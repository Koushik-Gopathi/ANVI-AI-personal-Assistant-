@echo off
rem Builds dist\Karen\Karen.exe (keeps using the .env in this folder)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --windowed --name Karen --icon web\anvi.ico ^
  --add-data "web;web" ^
  --collect-submodules uvicorn ^
  desktop.py
if errorlevel 1 exit /b 1
echo.
echo Built: %~dp0dist\Karen\Karen.exe
