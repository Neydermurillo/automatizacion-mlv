"""Pide el usuario y la clave de DS Nube y los guarda en .env (solo en este computador)."""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

RUTA = Path(__file__).resolve().parent / ".env"


def pedir(texto: str, secreto: bool = False) -> str:
    while True:
        valor = (getpass.getpass(texto) if secreto else input(texto)).strip()
        if valor:
            return valor
        print("  No puede quedar vacío.")


def main() -> None:
    if RUTA.exists():
        if input("Ya hay datos guardados. ¿Reemplazarlos? (s/N): ").strip().lower() != "s":
            sys.exit("No se cambió nada.")
    print("Datos de ingreso a DS Nube (la clave no se muestra mientras escribes).")
    usuario = pedir("Usuario: ")
    while True:
        clave = pedir("Clave: ", secreto=True)
        if clave == pedir("Repite la clave: ", secreto=True):
            break
        print("  Las claves no coinciden, intenta de nuevo.")
    if any(c in usuario + clave for c in "'\"\\\n"):
        sys.exit("El usuario o la clave tienen comillas o barra invertida; escríbelos por soporte.")
    # DS_EMPRESA queda con un valor de relleno: NeyBot siempre usa la empresa elegida en pantalla.
    # Valores entre comillas simples para que '#' o '$' dentro de la clave no se interpreten.
    cuerpo = (
        f"DS_USUARIO='{usuario}'\n"
        f"DS_CLAVE='{clave}'\n"
        "DS_EMPRESA=SIN_EMPRESA\n"
    )
    RUTA.write_text(cuerpo, encoding="utf-8")
    try:
        os.chmod(RUTA, 0o600)
    except OSError:
        pass
    print(f"\nGuardado en {RUTA}. No compartas ese archivo.")


if __name__ == "__main__":
    main()
