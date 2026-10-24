@echo off
setlocal
cd /d "%~dp0"
set "ESTOQUE_DADOS=%~dp0dados"
set "URL=http://127.0.0.1:8765"
if not exist "%ESTOQUE_DADOS%" mkdir "%ESTOQUE_DADOS%"

curl.exe -s -o nul "%URL%/api/gerados" && goto abrir
start "" "%~dp0app\python\pythonw.exe" "%~dp0app\painel.py" --sem-navegador
for /l %%i in (1,1,30) do (
  curl.exe -s -o nul "%URL%/api/gerados" && goto abrir
  ping -n 2 127.0.0.1 >nul
)
echo Nao foi possivel iniciar o Estoque SPED.
pause
exit /b 1

:abrir
rem Janela de programa no Edge ou no Chrome; sem nenhum dos dois, o navegador padrao
for %%n in (
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) do if exist %%n (
  start "" %%n --app=%URL% --window-size=1400,900
  exit /b 0
)
start "" %URL%
