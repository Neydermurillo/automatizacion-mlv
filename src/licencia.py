"""Licencia por computador: un código firmado (Ed25519) con fecha de vencimiento.

- La licencia se genera con `generar_licencia.py` (solo lo tiene el propietario, con su clave privada).
- Este programa únicamente VERIFICA la firma con la clave pública (src/clave_publica.pem).
- La licencia queda amarrada al ID del computador: copiada a otro equipo no sirve.
- Tras el vencimiento hay DIAS_GRACIA días de gracia; después se bloquea solo la CARGA REAL
  (la simulación y la descarga de resultados siguen funcionando).
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.config import BASE_DIR

DIAS_GRACIA = 2
DIAS_AVISO = 30
TOLERANCIA_RELOJ = timedelta(days=1)

ARCHIVO_LICENCIA = BASE_DIR / "data" / "licencia.lic"
ARCHIVO_USO = BASE_DIR / "data" / "uso.dat"
ARCHIVO_CLAVE_PUBLICA = BASE_DIR / "src" / "clave_publica.pem"
_SAL = "neybot-licencia-v1"


class LicenciaInvalida(Exception):
    """El código de licencia no es válido (firma, formato o equipo)."""


class LibreriaFaltante(Exception):
    """Falta instalar la librería 'cryptography' en el Python que ejecuta el programa."""


class LicenciaError(Exception):
    """La licencia no permite la operación pedida."""


# ----------------------------------------------------------------------------- equipo
def _identificador_crudo() -> str:
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as clave:
                return str(winreg.QueryValueEx(clave, "MachineGuid")[0])
        except OSError:
            pass
    for ruta in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            texto = Path(ruta).read_text().strip()
            if texto:
                return texto
        except OSError:
            continue
    return f"mac-{uuid.getnode():012x}"


def normalizar_equipo(texto: str) -> str:
    return "".join(c for c in str(texto).upper() if c.isalnum())


def id_equipo() -> str:
    """ID corto y estable de este computador, p. ej. 7F3A-91C2-0B5D-E4A8."""
    resumen = hashlib.sha256(f"{_SAL}|{_identificador_crudo()}".encode()).hexdigest()[:16].upper()
    return "-".join(resumen[i : i + 4] for i in range(0, 16, 4))


# ----------------------------------------------------------------------------- firma
def _b64d(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def cargar_clave_publica(ruta: Path | None = None):
    """Clave pública del propietario, o None si no está instalada."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise LibreriaFaltante from None

    ruta = ruta or ARCHIVO_CLAVE_PUBLICA
    try:
        return serialization.load_pem_public_key(ruta.read_bytes())
    except (OSError, ValueError):
        return None


def decodificar(codigo: str, clave) -> dict:
    """Verifica la firma y devuelve los datos de la licencia."""
    try:
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise LibreriaFaltante from None

    limpio = "".join(str(codigo).split())
    if limpio.count(".") != 1:
        raise LicenciaInvalida("El código de licencia no tiene el formato correcto.")
    cuerpo, firma = limpio.split(".")
    try:
        datos_brutos, firma_bruta = _b64d(cuerpo), _b64d(firma)
        clave.verify(firma_bruta, datos_brutos)
        datos = json.loads(datos_brutos)
    except (InvalidSignature, ValueError, TypeError):
        raise LicenciaInvalida("La licencia no es auténtica o está dañada.") from None
    if not isinstance(datos, dict) or not {"equipo", "emitida", "vence"} <= datos.keys():
        raise LicenciaInvalida("La licencia está incompleta.")
    return datos


# ----------------------------------------------------------------------------- reloj
def _sello(ultima: str, equipo: str) -> str:
    return hashlib.sha256(f"{ultima}|{normalizar_equipo(equipo)}|{_SAL}".encode()).hexdigest()


def _ultimo_uso(ruta: Path, equipo: str) -> datetime | None:
    try:
        datos = json.loads(ruta.read_text())
        if datos.get("sello") != _sello(datos["ultima"], equipo):
            return None
        return datetime.fromisoformat(datos["ultima"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _registrar_uso(ruta: Path, equipo: str, ahora: datetime) -> None:
    anterior = _ultimo_uso(ruta, equipo)
    ultima = max(ahora, anterior) if anterior else ahora
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        texto = ultima.isoformat()
        ruta.write_text(json.dumps({"ultima": texto, "sello": _sello(texto, equipo)}))
    except OSError:
        pass


# ----------------------------------------------------------------------------- estado
@dataclass(frozen=True)
class Estado:
    codigo: str  # sin_licencia, sin_clave, sin_libreria, invalida, otro_equipo, reloj, activa, por_vencer, gracia, vencida
    mensaje: str
    nivel: str  # ok, info, warn, bad
    permite_real: bool
    equipo: str
    cliente: str = ""
    vence: date | None = None
    dias_restantes: int | None = None

    def a_dict(self) -> dict:
        return {
            "codigo": self.codigo,
            "mensaje": self.mensaje,
            "nivel": self.nivel,
            "permite_real": self.permite_real,
            "equipo": self.equipo,
            "cliente": self.cliente,
            "vence": self.vence.strftime("%d/%m/%Y") if self.vence else None,
            "dias_restantes": self.dias_restantes,
            "dias_gracia": DIAS_GRACIA,
        }


def _fecha(texto: str) -> date:
    return date.fromisoformat(str(texto)[:10])


def evaluar(
    *,
    hoy: date | None = None,
    ahora: datetime | None = None,
    ruta_licencia: Path | None = None,
    ruta_uso: Path | None = None,
    clave=None,
    equipo: str | None = None,
    registrar_uso: bool = True,
) -> Estado:
    ahora = ahora or datetime.now(timezone.utc)
    hoy = hoy or datetime.now().date()
    ruta_licencia = ruta_licencia or ARCHIVO_LICENCIA
    ruta_uso = ruta_uso or ARCHIVO_USO
    equipo = equipo or id_equipo()
    def estado(codigo, mensaje, nivel, permite=False, **extra) -> Estado:
        return Estado(codigo, mensaje, nivel, permite, equipo, **extra)

    try:
        clave = clave if clave is not None else cargar_clave_publica()
    except LibreriaFaltante:
        return estado(
            "sin_libreria",
            "Falta instalar la librería 'cryptography' en el Python de este programa "
            "(python -m pip install cryptography).",
            "bad",
        )
    if clave is None:
        return estado(
            "sin_clave",
            "Falta el archivo src/clave_publica.pem; no se puede verificar la licencia.",
            "bad",
        )
    if not ruta_licencia.exists():
        return estado(
            "sin_licencia",
            "Sin licencia: puedes simular, pero no grabar. Actívala en la sección Licencia.",
            "warn",
        )
    try:
        datos = decodificar(ruta_licencia.read_text(encoding="utf-8"), clave)
        vence, emitida = _fecha(datos["vence"]), _fecha(datos["emitida"])
    except (OSError, ValueError, LicenciaInvalida) as e:
        return estado("invalida", f"{e} Pide una licencia nueva.", "bad")

    cliente = str(datos.get("cliente", ""))
    if normalizar_equipo(datos["equipo"]) != normalizar_equipo(equipo):
        return estado(
            "otro_equipo",
            "Esta licencia pertenece a otro computador. Pide una para este equipo.",
            "bad",
            cliente=cliente,
        )

    # el reloj no puede ir antes de la emisión de la licencia ni antes del último uso
    hoy_utc = ahora.astimezone(timezone.utc)
    ultimo = _ultimo_uso(ruta_uso, equipo)
    emision_utc = datetime(emitida.year, emitida.month, emitida.day, tzinfo=timezone.utc)
    if hoy_utc < emision_utc - TOLERANCIA_RELOJ or (ultimo and hoy_utc < ultimo - TOLERANCIA_RELOJ):
        return estado(
            "reloj",
            "La fecha del computador parece estar atrasada. Corrígela para poder grabar.",
            "bad",
            cliente=cliente,
            vence=vence,
        )
    if registrar_uso:
        _registrar_uso(ruta_uso, equipo, hoy_utc)

    dias = (vence - hoy).days
    comunes = {"cliente": cliente, "vence": vence, "dias_restantes": dias}
    fecha = vence.strftime("%d/%m/%Y")
    if dias >= 0:
        if dias <= DIAS_AVISO:
            nivel = "bad" if dias <= 7 else "warn" if dias <= 15 else "info"
            cuando = "vence hoy" if dias == 0 else f"vence el {fecha}, faltan {dias} día(s)"
            return estado("por_vencer", f"Tu licencia {cuando}. Renuévala para no interrumpir el trabajo.", nivel, True, **comunes)
        return estado("activa", f"Licencia activa hasta el {fecha}.", "ok", True, **comunes)
    pasados = -dias
    if pasados <= DIAS_GRACIA:
        restan = DIAS_GRACIA - pasados
        return estado(
            "gracia",
            f"Tu licencia venció el {fecha}. Estás en período de gracia: quedan {restan} día(s) para renovar.",
            "bad",
            True,
            **comunes,
        )
    return estado(
        "vencida",
        f"Tu licencia venció el {fecha}. Puedes simular, pero no grabar hasta renovarla.",
        "bad",
        **comunes,
    )


def activar(codigo: str, *, clave=None, ruta_licencia: Path | None = None, **kw) -> Estado:
    """Valida el código pegado por la persona y, si sirve, lo guarda como licencia."""
    try:
        clave = clave if clave is not None else cargar_clave_publica()
    except LibreriaFaltante:
        raise LicenciaInvalida(
            "Falta instalar la librería 'cryptography' (python -m pip install cryptography)."
        ) from None
    if clave is None:
        raise LicenciaInvalida("Falta el archivo src/clave_publica.pem.")
    equipo = kw.get("equipo") or id_equipo()
    datos = decodificar(codigo, clave)
    if normalizar_equipo(datos["equipo"]) != normalizar_equipo(equipo):
        raise LicenciaInvalida("Esa licencia es de otro computador. Pide una para este equipo.")
    hoy = kw.get("hoy") or datetime.now().date()
    if (hoy - _fecha(datos["vence"])).days > DIAS_GRACIA:
        raise LicenciaInvalida("Esa licencia ya está vencida. Pide una vigente.")
    ruta = ruta_licencia or ARCHIVO_LICENCIA
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("".join(str(codigo).split()), encoding="utf-8")
    return evaluar(clave=clave, ruta_licencia=ruta, **kw)


def exigir_carga_real(**kw) -> Estado:
    """Lanza LicenciaError si la licencia no permite grabar en producción."""
    est = evaluar(**kw)
    if not est.permite_real:
        raise LicenciaError(est.mensaje)
    return est
