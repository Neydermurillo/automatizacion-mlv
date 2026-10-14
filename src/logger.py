import logging
from pathlib import Path


def crear_logger(dir_logs: Path) -> logging.Logger:
    dir_logs.mkdir(exist_ok=True)
    logger = logging.getLogger("dsnube")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formato = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    consola = logging.StreamHandler()
    consola.setFormatter(formato)
    archivo = logging.FileHandler(dir_logs / "proceso.log", encoding="utf-8")
    archivo.setFormatter(formato)

    logger.addHandler(consola)
    logger.addHandler(archivo)
    return logger