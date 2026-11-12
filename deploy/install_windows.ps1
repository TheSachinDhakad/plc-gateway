<#
.SYNOPSIS
  Install industrial-plc-gateway on a Windows 10/11 industrial PC.

.DESCRIPTION
  - Creates .venv with Python 3.11/3.12 (Python launcher "py")
  - Installs runtime + Windows dependencies (pywin32)
  - Creates data\, logs\, certs\ and .env (from .env.example), restricts ACLs
  - Validates the configuration (no PLC connections)
  - Optionally installs the Windows service with restart-on-failure recovery

  Run from an elevated PowerShell in the project directory:
    Set-ExecutionPolicy -Scope Process Bypass
    .\deploy\install_windows.ps1 -InstallService
    .\deploy\install_windows.ps1 -InstallService -ServiceAccount ".\svc_plcgw"   # recommended

.NOTES
  This script has been written for Windows PowerShell 5.1 / PowerShell 7 but must be
  validated on the target image before production rollout.
#>
[CmdletBinding()]
param(
  [switch]$InstallService,
  [string]$ServiceAccount = "",
  [string]$PythonVersion = "3.11"
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell (Administrator)."
  }
}

Write-Host "== industrial-plc-gateway installer ==" -ForegroundColor Cyan
if ($InstallService) { Assert-Admin }

# --- Python
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { throw "Python launcher 'py' not found. Install Python $PythonVersion (64-bit) from python.org." }
& py "-$PythonVersion" --version
if ($LASTEXITCODE -ne 0) { throw "Python $PythonVersion is not installed." }

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  & py "-$PythonVersion" -m venv .venv
}
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

# --- directories and secrets file
foreach ($d in "data", "logs", "certs") { New-Item -ItemType Directory -Force -Path $d | Out-Null }
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example - edit it with real credentials." -ForegroundColor Yellow
}

# Restrict .env, certs, data, logs to Administrators, SYSTEM and (optionally) the service account
$principals = @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM")
if ($ServiceAccount) { $principals += $ServiceAccount }
foreach ($target in ".env", "certs", "data", "logs") {
  & icacls $target /inheritance:r | Out-Null
  foreach ($p in $principals) {
    $perm = if ($target -eq ".env" -or $target -eq "certs") { "(R)" } else { "(M)" }
    if ((Get-Item $target).PSIsContainer) { $perm = "(OI)(CI)$perm" }
    & icacls $target /grant:r "${p}:$perm" | Out-Null
  }
}

# --- validate configuration
& $VenvPy scripts\validate_config.py --config-dir config
if ($LASTEXITCODE -ne 0) { Write-Warning "Configuration is not valid yet - fix config\*.yaml before starting." }

if ($InstallService) {
  # pywin32 post-install registers pythoncom/pywintypes DLLs needed by pythonservice.exe
  $post = Join-Path $Root ".venv\Scripts\pywin32_postinstall.py"
  if (Test-Path $post) { & $VenvPy $post -install -silent | Out-Null }

  & $VenvPy deploy\gateway_service.py --startup auto install
  if ($LASTEXITCODE -ne 0) { throw "service installation failed" }
  # restart on failure: 5 s, 30 s, 60 s; reset failure count after 1 day
  & sc.exe failure PLCGateway reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
  & sc.exe failureflag PLCGateway 1 | Out-Null
  if ($ServiceAccount) {
    $cred = Get-Credential -UserName $ServiceAccount -Message "Password for service account $ServiceAccount"
    & sc.exe config PLCGateway obj= $ServiceAccount password= $cred.GetNetworkCredential().Password | Out-Null
    Write-Host "Grant '$ServiceAccount' the 'Log on as a service' right (secpol.msc) if not already granted."
  }
  Write-Host "Service 'PLCGateway' installed. Start with:  Start-Service PLCGateway" -ForegroundColor Green
}

Write-Host @"

Next steps
  1. Edit .env (MySQL credentials) and config\plants.yaml / tags.yaml
  2. Create schema:  .venv\Scripts\python.exe scripts\init_database.py
  3. Foreground run: deploy\run_gateway.bat
  4. Service:        Start-Service PLCGateway / Stop-Service PLCGateway
  Logs: $Root\logs\gateway.log   Status: $Root\data\status.json
"@
