@echo off
title Instalar Estoque SPED
cd /d "%~dp0"
if not exist "%~dp0app\painel.py" (
  echo Extraia o arquivo .zip para uma pasta antes de instalar.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\instalar.ps1"
if errorlevel 1 (
  echo.
  echo A instalacao falhou.
  pause
  exit /b 1
)
echo.
echo Pronto. Abra pelo atalho "Estoque SPED" na Area de Trabalho ou no Menu Iniciar.
start "" "%LOCALAPPDATA%\Programs\Estoque SPED\Estoque SPED.cmd"
pause
