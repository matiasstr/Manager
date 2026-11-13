@echo off
rem Manager: prepara el entorno la primera vez y abre la interfaz.
rem El entorno vive en %USERPROFILE%\.manager para no chocar con el limite de rutas largas de Windows.
setlocal
cd /d "%~dp0"
set "VENV=%USERPROFILE%\.manager\venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo Preparando el entorno por primera vez. Tarda un par de minutos...
  python -m venv "%VENV%" || goto :error
)
"%VENV%\Scripts\python.exe" -c "import importlib.util as u, sys; sys.exit(any(u.find_spec(m) is None for m in ('fastapi','uvicorn','langgraph','fastembed','openai','mcp','numpy')))" 2>nul
if errorlevel 1 (
  echo Instalando dependencias...
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)

"%VENV%\Scripts\python.exe" -m manager %*
goto :eof

:error
echo.
echo No se pudo preparar el entorno. Revisa que Python 3.11 o superior este instalado y en el PATH.
pause
exit /b 1
