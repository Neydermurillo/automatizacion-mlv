import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Opciones de la ventana "Formas de pago" (si la plataforma agrega otra, agrégala aquí).
FORMAS_DE_PAGO = (
    "Consignaciones",
    "PAGO EN EFECTIVO",
    "Transferencia Caja social",
    "Transferencia Fondo Inversiones",
)


@dataclass(frozen=True)
class Config:
    usuario: str
    clave: str
    empresa: str
    url_login: str = "https://ds.dsnube.co/dslogin/"
    timeout_ms: int = 30_000
    headless: bool = False
    canal_navegador: str = "msedge"
    fecha_pago: date = date(2025, 12, 31)   # fecha del pago y del comprobante nuevo
    archivo_excel: Path = BASE_DIR / "data" / "pagos.xlsx"
    hoja_excel: str | None = None
    fila_inicio: int = 2            # la fila 1 es el encabezado; todo lo demás se lee
    col_nit: str = "C"
    col_nombre: str = "D"
    col_valor: str = "H"
    dir_logs: Path = BASE_DIR / "logs"
    dir_output: Path = BASE_DIR / "output"
    carga_real: bool = False        # True solo si CARGA_REAL=true en el .env
    max_cargas: int = 1             # comprobantes que se graban como máximo por corrida
    pantallazos: bool = False       # True solo si PANTALLAZOS=true en el .env
    forma_pago: str = "PAGO EN EFECTIVO"   # FORMA_PAGO en el .env


def cargar_config() -> Config:
    usuario = os.getenv("DS_USUARIO")
    clave = os.getenv("DS_CLAVE")
    empresa = os.getenv("DS_EMPRESA")
    if not usuario or not clave or not empresa:
        raise ValueError("Faltan DS_USUARIO, DS_CLAVE o DS_EMPRESA en el archivo .env")

    forma = os.getenv("FORMA_PAGO", "PAGO EN EFECTIVO").strip()
    validas = {f.lower(): f for f in FORMAS_DE_PAGO}
    if forma.lower() not in validas:
        raise ValueError(
            f"FORMA_PAGO '{forma}' no es válida. Opciones: {', '.join(FORMAS_DE_PAGO)}"
        )

    return Config(
        usuario=usuario,
        clave=clave,
        empresa=empresa,
        carga_real=os.getenv("CARGA_REAL", "false").strip().lower() == "true",
        max_cargas=int(os.getenv("MAX_CARGAS", "1")),
        pantallazos=os.getenv("PANTALLAZOS", "false").strip().lower() == "true",
        forma_pago=validas[forma.lower()],
    )