@echo off
cd /d "%~dp0"
set PY=
where py >nul 2>&1
if not errorlevel 1 set PY=py
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set PY=python
)
if not defined PY (
  echo No encuentro Python. Instalalo desde https://www.python.org/downloads/ y marca "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Instalando dependencias...
%PY% -m pip install -r requirements.txt -r requirements-dev.txt
if errorlevel 1 (
  echo Fallo al instalar las dependencias.
  pause
  exit /b 1
)
echo Creando el .exe...
%PY% -m PyInstaller --onefile --noconsole --clean --add-data "assets\star.png;assets" --icon assets\icon.ico --name LuxkorAccountManager roblox_manager.py
if errorlevel 1 (
  echo Fallo al crear el .exe.
  pause
  exit /b 1
)
copy /y "dist\LuxkorAccountManager.exe" "LuxkorAccountManager.exe" >nul
rmdir /s /q build dist
del /q LuxkorAccountManager.spec
echo.
echo Listo: LuxkorAccountManager.exe esta en esta carpeta.
pause
