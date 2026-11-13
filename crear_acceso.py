"""Crea el ícono de NeyBot en el escritorio y en el menú Inicio (se abre con un clic, sin consola)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PYTHONW = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
ICONO = RAIZ / "neybot.ico"

SCRIPT = r"""
$ws = New-Object -ComObject WScript.Shell
$destinos = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)
foreach ($d in $destinos) {
  if (-not (Test-Path $d)) { continue }
  $l = $ws.CreateShortcut((Join-Path $d 'NeyBot.lnk'))
  $l.TargetPath = $env:NB_PYTHONW
  $l.Arguments = 'webui.py'
  $l.WorkingDirectory = $env:NB_RAIZ
  $l.IconLocation = $env:NB_ICONO
  $l.Description = 'NeyBot Automatización'
  $l.Save()
}
"""


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Este acceso directo solo se crea en Windows.")
    if not PYTHONW.exists():
        sys.exit("No se encontró .venv\\Scripts\\pythonw.exe. Ejecuta primero instalar.bat.")
    env = {**__import__("os").environ, "NB_PYTHONW": str(PYTHONW), "NB_RAIZ": str(RAIZ), "NB_ICONO": str(ICONO)}
    r = subprocess.run(["powershell", "-NoProfile", "-Command", SCRIPT], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("No se pudo crear el acceso directo:\n" + r.stderr)
    print("Listo: ya tienes el ícono NeyBot en el escritorio y en el menú Inicio.")


if __name__ == "__main__":
    main()
