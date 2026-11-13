@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==============================================
echo   NeyBot - instalacion (tarda unos minutos)
echo ==============================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [X] No se encontro Python. Instalalo desde https://www.python.org/downloads/
  echo     y marca la casilla "Add Python to PATH". Luego ejecuta este archivo de nuevo.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creando entorno de Python...
  python -m venv .venv
  if errorlevel 1 goto error
)
echo [2/5] Instalando librerias...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if exist requirements.txt (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -m pip install playwright python-dotenv openpyxl cryptography --quiet
if errorlevel 1 goto error
echo [3/5] Verificando Microsoft Edge...
if not exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" if not exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
  echo [X] No se encontro Microsoft Edge. Instalalo y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)
echo [4/5] Configuracion de DS Nube (tus datos quedan solo en este computador)
if not exist ".env" (
  ".venv\Scripts\python.exe" configurar.py
  if errorlevel 1 goto error
) else (
  echo     Ya existe .env; no se cambia. Para cambiarlo ejecuta configurar.bat
)
echo [5/5] Creando el icono de NeyBot en el escritorio...
".venv\Scripts\python.exe" crear_acceso.py
echo.
echo Listo. Abre NeyBot con el icono NeyBot del escritorio.
pause
exit /b 0
:error
echo.
echo [X] Algo fallo. Toma una foto de esta pantalla y enviala a soporte.
pause
exit /b 1
