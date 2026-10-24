param(
  [string]$Destino = (Join-Path $env:LOCALAPPDATA 'Programs\Estoque SPED'),
  [string]$PastaAtalhos = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$origem = Split-Path -Parent $PSScriptRoot
$app = Join-Path $Destino 'app'
$novo = Join-Path $Destino 'app.novo'
$velho = Join-Path $Destino 'app.velho'

function Porta-Livre {
  -not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
}

# Fecha a versao aberta: primeiro pedindo ao proprio sistema, depois pelo executavel
try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 3 'http://127.0.0.1:8765/api/encerrar' | Out-Null } catch {}
for ($i = 0; $i -lt 20 -and -not (Porta-Livre); $i++) { Start-Sleep -Milliseconds 250 }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.ExecutablePath -like '*\app\python\pythonw.exe' -and $_.CommandLine -like '*painel.py*' } |
  ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
Start-Sleep -Milliseconds 500

# Copia a versao nova ao lado e so depois troca: se algo falhar, a antiga continua inteira
New-Item -ItemType Directory -Force (Join-Path $Destino 'dados') | Out-Null
foreach ($p in $novo, $velho) { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
Copy-Item -Recurse (Join-Path $origem 'app') $novo
if (Test-Path $app) {
  try { Rename-Item $app 'app.velho' }
  catch { Remove-Item -Recurse -Force $novo; throw 'O Estoque SPED ainda esta aberto. Feche a janela dele e rode o instalador de novo.' }
}
Rename-Item $novo 'app'
if (Test-Path $velho) { Remove-Item -Recurse -Force $velho -ErrorAction SilentlyContinue }
Copy-Item (Join-Path $origem 'Estoque SPED.cmd'), (Join-Path $origem 'LEIA-ME.txt') $Destino -Force

$atalhos = if ($PastaAtalhos) { @(Join-Path $PastaAtalhos 'Estoque SPED.lnk') } else { @(
  (Join-Path ([Environment]::GetFolderPath('Programs')) 'Estoque SPED.lnk'),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Estoque SPED.lnk')
) }
# O .ico tem o codigo do desenho no nome: o Windows nao reaproveita o icone antigo do cache
$icone = (Get-ChildItem $app -Filter 'icone-*.ico' | Select-Object -First 1).FullName
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in $atalhos) {
  $a = $shell.CreateShortcut($lnk)
  $a.TargetPath = Join-Path $Destino 'Estoque SPED.cmd'
  $a.WorkingDirectory = $Destino
  $a.IconLocation = $icone + ',0'
  $a.WindowStyle = 7
  $a.Description = 'Converte o inventario do cliente em SPED Fiscal (bloco H)'
  $a.Save()
}
try { Start-Process -FilePath "$env:SystemRoot\System32\ie4uinit.exe" -ArgumentList '-show' -Wait -WindowStyle Hidden } catch {}
Write-Host "Instalado em $Destino"
