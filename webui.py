"""Ventana gráfica moderna: una página web local que se abre como aplicación de Edge.

Solo acepta conexiones de este mismo computador (127.0.0.1). No necesita instalar nada nuevo.
Uso:  python webui.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

if sys.stdout is None:  # se abre con pythonw (sin consola): evita fallos al imprimir
    sys.stdout = sys.stderr = open(os.devnull, "w")

from src.config import BASE_DIR

os.chdir(BASE_DIR)  # empresas.txt, output/ y logs/ siempre relativos al proyecto

import main as robot  # noqa: E402
from src import licencia  # noqa: E402
from src.config import cargar_config  # noqa: E402
from src.excel_io import leer_registros  # noqa: E402
from src.logger import crear_logger  # noqa: E402
from src.validacion import TipoDocumento  # noqa: E402

HOST = "127.0.0.1"
PUERTO = int(os.getenv("MLV_PUERTO", "8765"))
CARPETA_WEB = BASE_DIR / "web"
CARPETA_EXCEL = BASE_DIR / "data" / "cargas"
MAX_EXCEL_BYTES = 50 * 1024 * 1024
EXTENSIONES = {".xlsx", ".xlsm"}
SIN_LIMITE = 10_000_000  # carga real: procesa todos los registros del archivo
TOKEN = secrets.token_urlsafe(24)  # evita que otra página abierta en el navegador dispare corridas

TIPOS = {
    "egreso": TipoDocumento.COMPROBANTE_EGRESO,
    "recibo": TipoDocumento.RECIBO_CAJA,
}


# ----------------------------------------------------------------------------- registro en vivo
class RegistroEnVivo(logging.Handler):
    """Guarda las líneas del log para que la página las lea."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lineas: list[dict] = []
        self._cerrojo = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            texto = record.getMessage()
            if record.exc_info:
                texto += " (ver detalle en la carpeta logs)"
            with self._cerrojo:
                self.lineas.append(
                    {
                        "h": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                        "n": record.levelname,
                        "t": texto,
                    }
                )
        except Exception:
            pass

    def desde(self, indice: int) -> list[dict]:
        with self._cerrojo:
            return self.lineas[indice:]

    def total(self) -> int:
        with self._cerrojo:
            return len(self.lineas)


class Estado:
    """Estado de la corrida actual (solo se permite una a la vez)."""

    def __init__(self) -> None:
        self.cerrojo = threading.Lock()
        self.corriendo = False
        self.detener = threading.Event()
        self.registro = RegistroEnVivo()
        self.resumen: dict | None = None
        self.error: str | None = None
        self.descripcion = ""


ESTADO = Estado()


# ----------------------------------------------------------------------------- utilidades
def _nombre_seguro(nombre: str) -> str:
    base = Path(nombre).stem
    limpio = re.sub(r"[^\w\- ().]", "", base, flags=re.UNICODE).strip() or "excel"
    return limpio[:60]


def _formato_fecha(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def listar_historial() -> list[dict]:
    carpeta = BASE_DIR / "output"
    if not carpeta.exists():
        return []
    archivos = sorted(carpeta.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "nombre": a.name,
            "fecha": _formato_fecha(a.stat().st_mtime),
            "kb": round(a.stat().st_size / 1024),
        }
        for a in archivos[:200]
        if not a.name.startswith("~$")
    ]


def vista_previa_excel(ruta: Path) -> dict:
    """Lee el Excel con la misma lógica del robot y cuenta lo que encontró."""
    config = replace(cargar_config(), archivo_excel=ruta)
    registros, invalidas = leer_registros(config)
    con_datos = [i for i in invalidas if not robot._es_fila_sin_datos(i)]
    return {"registros": len(registros), "invalidas": len(con_datos)}


def estado_inicial() -> dict:
    hoy = date.today()
    empresa_env = os.getenv("DS_EMPRESA", "").strip()
    empresas = robot.cargar_empresas()
    if empresa_env and empresa_env not in empresas:
        empresas.insert(0, empresa_env)
    return {
        "token": TOKEN,
        "hoy": hoy.strftime("%d/%m/%Y"),
        "anios": robot.anios_disponibles(),
        "anio_actual": hoy.year,
        "empresas": empresas,
        "empresa_defecto": empresa_env,
        "formas_pago": list(robot.OPCIONES_PAGO.values()),
        "forma_recibo": robot.FORMA_PAGO_RECIBO,
    }


# ----------------------------------------------------------------------------- la corrida
def _hilo_corrida(corrida, config, ruta_excel: Path) -> None:
    logger = crear_logger(config.dir_logs)
    logger.addHandler(ESTADO.registro)
    try:
        config = replace(config, archivo_excel=ruta_excel)
        resumen = robot.ejecutar_corrida(corrida, config, logger, parar=ESTADO.detener.is_set)
        ESTADO.resumen = {
            "ruta": Path(resumen["ruta"]).name if resumen.get("ruta") else None,
            "conteo": dict(resumen["conteo"]),
            "total": resumen["total"],
            "detenida": resumen["detenida"],
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("La corrida terminó con error")
        ESTADO.error = str(e)
    finally:
        logger.removeHandler(ESTADO.registro)
        ESTADO.corriendo = False


def iniciar_corrida(datos: dict) -> tuple[int, dict]:
    with ESTADO.cerrojo:
        if ESTADO.corriendo:
            return 409, {"error": "Ya hay una corrida en curso."}

        empresa = str(datos.get("empresa", "")).strip()
        tipo = TIPOS.get(datos.get("documento"))
        try:
            anio = int(datos.get("anio"))
        except (TypeError, ValueError):
            return 400, {"error": "Año no válido."}
        archivo = str(datos.get("archivo", ""))
        carga_real = bool(datos.get("carga_real"))

        if not empresa or tipo is None:
            return 400, {"error": "Falta elegir la empresa o el documento."}
        ruta = (CARPETA_EXCEL / Path(archivo).name)
        if not archivo or not ruta.is_file():
            return 400, {"error": "Primero sube el archivo de Excel."}
        if carga_real:
            lic = licencia.evaluar()
            if not lic.permite_real:
                return 403, {"error": lic.mensaje}
            if str(datos.get("confirmacion", "")).strip() != "GRABAR":
                return 400, {"error": "Para grabar en producción escribe GRABAR."}

        es_recibo = tipo == TipoDocumento.RECIBO_CAJA
        forma = robot.FORMA_PAGO_RECIBO if es_recibo else str(datos.get("forma_pago", ""))
        if not es_recibo and forma not in robot.OPCIONES_PAGO.values():
            return 400, {"error": "Elige la forma de pago."}

        try:
            config = cargar_config()
            corrida = robot.Corrida(
                empresa=empresa,
                tipo=tipo,
                anio=anio,
                forma_pago=forma,
                carga_real=carga_real,
                max_cargas=SIN_LIMITE if carga_real else 1,
            )
        except Exception as e:  # noqa: BLE001
            return 400, {"error": str(e)}

        ESTADO.registro = RegistroEnVivo()
        ESTADO.resumen = None
        ESTADO.error = None
        ESTADO.detener.clear()
        ESTADO.descripcion = robot.describir_corrida(corrida)
        ESTADO.corriendo = True
        threading.Thread(target=_hilo_corrida, args=(corrida, config, ruta), daemon=True).start()
        return 200, {"ok": True}


# ----------------------------------------------------------------------------- servidor HTTP
class Manejador(BaseHTTPRequestHandler):
    server_version = "MLV"

    def log_message(self, *args) -> None:  # silencioso
        pass

    # --- respuestas
    def _json(self, codigo: int, datos: dict | list) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _archivo(self, ruta: Path, tipo: str, descarga: str | None = None) -> None:
        datos = ruta.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        if descarga:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(descarga)}")
        self.end_headers()
        self.wfile.write(datos)

    def _autorizado(self) -> bool:
        if self.headers.get("X-Token") != TOKEN:
            self._json(403, {"error": "No autorizado."})
            return False
        return True

    def _leer_json(self) -> dict:
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(largo) or b"{}")
        except json.JSONDecodeError:
            return {}

    # --- GET
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        ruta = url.path
        consulta = parse_qs(url.query)

        if ruta in ("/", "/index.html"):
            return self._archivo(CARPETA_WEB / "index.html", "text/html; charset=utf-8")
        if ruta.startswith("/img/"):
            nombre = Path(ruta).name
            archivo = CARPETA_WEB / "img" / nombre
            if archivo.suffix.lower() == ".png" and archivo.is_file():
                return self._archivo(archivo, "image/png")
            return self._json(404, {"error": "No encontrado."})
        if ruta == "/api/inicio":
            return self._json(200, estado_inicial())
        if ruta == "/api/estado":
            desde = int((consulta.get("desde") or ["0"])[0])
            return self._json(
                200,
                {
                    "corriendo": ESTADO.corriendo,
                    "descripcion": ESTADO.descripcion,
                    "lineas": ESTADO.registro.desde(desde),
                    "total": ESTADO.registro.total(),
                    "resumen": ESTADO.resumen,
                    "error": ESTADO.error,
                },
            )
        if ruta == "/api/licencia":
            return self._json(200, licencia.evaluar().a_dict())
        if ruta == "/api/historial":
            return self._json(200, listar_historial())
        if ruta == "/api/descargar":
            nombre = Path((consulta.get("nombre") or [""])[0]).name
            archivo = BASE_DIR / "output" / nombre
            if not nombre.endswith(".xlsx") or not archivo.is_file():
                return self._json(404, {"error": "No existe ese archivo."})
            return self._archivo(
                archivo,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                descarga=nombre,
            )
        self._json(404, {"error": "No encontrado."})

    # --- POST
    def do_POST(self) -> None:  # noqa: N802
        if not self._autorizado():
            return
        ruta = urlparse(self.path).path

        if ruta == "/api/subir":
            nombre = unquote(self.headers.get("X-Nombre", "excel.xlsx"))
            if Path(nombre).suffix.lower() not in EXTENSIONES:
                return self._json(400, {"error": "El archivo debe ser Excel (.xlsx)."})
            largo = int(self.headers.get("Content-Length") or 0)
            if not 0 < largo <= MAX_EXCEL_BYTES:
                return self._json(400, {"error": "El archivo está vacío o pesa más de 50 MB."})
            CARPETA_EXCEL.mkdir(parents=True, exist_ok=True)
            destino = CARPETA_EXCEL / (
                f"{datetime.now():%Y%m%d_%H%M%S}_{_nombre_seguro(nombre)}{Path(nombre).suffix.lower()}"
            )
            restante = largo
            with destino.open("wb") as f:
                while restante > 0:
                    trozo = self.rfile.read(min(restante, 1 << 20))
                    if not trozo:
                        break
                    f.write(trozo)
                    restante -= len(trozo)
            try:
                previa = vista_previa_excel(destino)
            except Exception as e:  # noqa: BLE001
                destino.unlink(missing_ok=True)
                return self._json(
                    400, {"error": f"No pude leer ese Excel: {' '.join(str(e).split())[:200]}"}
                )
            return self._json(200, {"archivo": destino.name, "nombre": nombre, **previa})

        if ruta == "/api/licencia/activar":
            try:
                est = licencia.activar(str(self._leer_json().get("codigo", "")))
            except licencia.LicenciaInvalida as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": f"No se pudo activar: {' '.join(str(e).split())[:160]}"})
            return self._json(200, est.a_dict())

        if ruta == "/api/iniciar":
            codigo, respuesta = iniciar_corrida(self._leer_json())
            return self._json(codigo, respuesta)

        if ruta == "/api/detener":
            if ESTADO.corriendo:
                ESTADO.detener.set()
                ESTADO.registro.emit(
                    logging.LogRecord("ui", logging.WARNING, "", 0,
                                      "Deteniendo: termina el registro en curso y para.", None, None)
                )
            return self._json(200, {"ok": True})

        if ruta == "/api/empresas/agregar":
            nombre = " ".join(str(self._leer_json().get("nombre", "")).split())
            if not nombre:
                return self._json(400, {"error": "Escribe el nombre de la empresa."})
            if nombre not in robot.cargar_empresas():
                with robot.ARCHIVO_EMPRESAS.open("a", encoding="utf-8") as f:
                    f.write(f"\n{nombre}\n")
            return self._json(200, {"empresas": robot.cargar_empresas()})

        if ruta == "/api/empresas/quitar":
            nombre = str(self._leer_json().get("nombre", "")).strip()
            archivo = robot.ARCHIVO_EMPRESAS
            if archivo.exists():
                lineas = archivo.read_text(encoding="utf-8-sig").splitlines()
                resto = [ln for ln in lineas if ln.strip() != nombre]
                archivo.write_text("\n".join(resto) + "\n", encoding="utf-8")
            return self._json(200, {"empresas": robot.cargar_empresas()})

        if ruta == "/api/abrir-carpeta":
            carpeta = BASE_DIR / "output"
            carpeta.mkdir(exist_ok=True)
            try:
                if sys.platform.startswith("win"):
                    os.startfile(carpeta)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(carpeta)])
                else:
                    subprocess.Popen(["xdg-open", str(carpeta)])
            except Exception:  # noqa: BLE001
                pass
            return self._json(200, {"ok": True})

        self._json(404, {"error": "No encontrado."})


# ----------------------------------------------------------------------------- arranque
def _abrir_ventana(url: str) -> None:
    """Abre la página como aplicación de Edge (sin barra de direcciones); si no, el navegador."""
    candidatos = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidatos:
        if exe and Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1360,940"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def main() -> None:
    CARPETA_EXCEL.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PUERTO}/"
    try:
        servidor = ThreadingHTTPServer((HOST, PUERTO), Manejador)
    except OSError:
        print("El programa ya está abierto; se muestra la ventana existente.")
        _abrir_ventana(url)
        return
    print(f"MLV Automatización en {url}  (cierra esta consola para terminar)")
    if os.getenv("MLV_SIN_VENTANA") != "1":
        threading.Timer(0.6, _abrir_ventana, args=(url,)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ESTADO.detener.set()
        servidor.server_close()
        time.sleep(0.1)


if __name__ == "__main__":
    main()
