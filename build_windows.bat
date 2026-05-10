@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

"%PYTHON_EXE%" -m PyInstaller ^
  --onefile ^
  --name SuralinkBoxSync ^
  --distpath dist ^
  --workpath build ^
  --specpath build ^
  --add-data "%CD%\app.py;." ^
  --add-data "%CD%\src;src" ^
  --add-data "%CD%\.streamlit;.streamlit" ^
  --add-data "%CD%\logo;logo" ^
  --collect-all streamlit ^
  --collect-all box_sdk_gen ^
  launcher.py

if errorlevel 1 (
  echo.
  echo Build failed. Make sure PyInstaller is installed:
  echo   uv pip install pyinstaller
  echo   "%PYTHON_EXE%" -m pip install pyinstaller
  exit /b 1
)

echo.
echo Built dist\SuralinkBoxSync.exe
echo Do not put .env or box_config.json inside the executable.
