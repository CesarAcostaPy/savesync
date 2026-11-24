# Instala o actualiza SaveSync en Windows.
#   irm https://github.com/__REPO__/releases/latest/download/install-windows.ps1 | iex
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = if ($env:SAVESYNC_REPO) { $env:SAVESYNC_REPO } else { "__REPO__" }
$Base = "https://github.com/$Repo/releases/latest/download"
$Dir  = Join-Path $env:LOCALAPPDATA "SaveSync"
$Tmp  = Join-Path $env:TEMP ("savesync-" + [guid]::NewGuid())

Write-Host "==> Descargando SaveSync..."
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
Invoke-WebRequest "$Base/SaveSync-windows-x64.zip" -OutFile "$Tmp\app.zip"
Invoke-WebRequest "$Base/savesync-cli-windows-x64.exe" -OutFile "$Tmp\savesync-cli.exe"

Write-Host "==> Instalando en $Dir"
Get-Process SaveSync -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path "$Dir\app") { Remove-Item "$Dir\app" -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$Dir\app" | Out-Null
Expand-Archive "$Tmp\app.zip" -DestinationPath "$Dir\app" -Force
Copy-Item "$Tmp\savesync-cli.exe" "$Dir\savesync-cli.exe" -Force
Remove-Item $Tmp -Recurse -Force

$Exe = Get-ChildItem "$Dir\app" -Filter "SaveSync.exe" -Recurse | Select-Object -First 1
if (-not $Exe) { $Exe = Get-ChildItem "$Dir\app" -Filter "*.exe" -Recurse | Select-Object -First 1 }

$Shell = New-Object -ComObject WScript.Shell
foreach ($Folder in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  $Lnk = $Shell.CreateShortcut((Join-Path $Folder "SaveSync.lnk"))
  $Lnk.TargetPath = $Exe.FullName
  $Lnk.WorkingDirectory = $Exe.DirectoryName
  $Lnk.Description = "Sincroniza tus partidas guardadas con GitHub"
  $Lnk.Save()
}

Write-Host ""
Write-Host "==> Listo. Abre SaveSync desde el menú Inicio o el escritorio."
Write-Host "   Sincronizar al jugar (Steam > juego > Propiedades > Opciones de lanzamiento):"
Write-Host "     `"$Dir\savesync-cli.exe`" run elden-ring -- %command%"
Write-Host "   Para actualizar, vuelve a ejecutar este mismo comando."
Start-Process $Exe.FullName
