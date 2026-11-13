"""HERRAMIENTA PRIVADA. Crea el ZIP limpio que se le entrega al cliente.

    python empaquetar_cliente.py            ->  NeyBot_cliente.zip

Excluye todo lo tuyo: .env (tus claves de DS Nube), data/, output/, logs/, .venv, pruebas,
generar_licencia.py, licencias_emitidas/ y tu lista de empresas (va una plantilla vacía).
Incluye src/clave_publica.pem (necesaria para validar la licencia).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
SALIDA = RAIZ / "NeyBot_cliente.zip"
EXCLUIR_CARPETAS = {".venv", "venv", "__pycache__", ".git", ".idea", ".vscode", "data", "output",
                    "logs", "licencias_emitidas", "tests", ".pytest_cache", "prueba"}
EXCLUIR_ARCHIVOS = {".env", "generar_licencia.py", "empaquetar_cliente.py", "empresas.txt",
                    "NeyBot_cliente.zip", "neybot.zip", "clave_privada.pem", "licencia.lic"}
EXCLUIR_SUFIJOS = {".pyc", ".log", ".lic", ".dat"}
PLANTILLA_EMPRESAS = "# Una empresa por línea. Se agregan también desde la pantalla de NeyBot.\n"
REQUERIDOS = ["main.py", "webui.py", "iniciar_web.bat", "instalar.bat", "configurar.py", "crear_acceso.py", "neybot.ico",
              "src/licencia.py", "src/clave_publica.pem", "web/index.html"]


def incluir(ruta: Path) -> bool:
    rel = ruta.relative_to(RAIZ)
    if any(p in EXCLUIR_CARPETAS for p in rel.parts[:-1]):
        return False
    return ruta.name not in EXCLUIR_ARCHIVOS and ruta.suffix not in EXCLUIR_SUFIJOS


def main() -> None:
    faltan = [r for r in REQUERIDOS if not (RAIZ / r).exists()]
    if faltan:
        sys.exit("Faltan archivos para el paquete: " + ", ".join(faltan))
    archivos = sorted(p for p in RAIZ.rglob("*") if p.is_file() and incluir(p))
    with zipfile.ZipFile(SALIDA, "w", zipfile.ZIP_DEFLATED) as z:
        for p in archivos:
            z.write(p, Path("NeyBot") / p.relative_to(RAIZ))
        z.writestr("NeyBot/empresas.txt", PLANTILLA_EMPRESAS)
    with zipfile.ZipFile(SALIDA) as z:
        nombres = z.namelist()
    peligro = [n for n in nombres if Path(n).name in {".env", "generar_licencia.py", "clave_privada.pem"}]
    if peligro:
        sys.exit(f"ERROR: el paquete incluye archivos privados: {peligro}")
    print(f"Listo: {SALIDA}  ({len(nombres)} archivos)")


if __name__ == "__main__":
    main()
