<#
.SYNOPSIS
    Keeps the iron-condor engine running.

.DESCRIPTION
    The engine holds live forward runs, so a process that dies and stays dead
    silently stops the thing the user is watching. This restarts it, with a
    backoff so a crash loop does not spin the CPU, and it logs every restart so
    the reason is recoverable afterwards.

    It also loads .env.engine.local if present, which is where the shared
    secret belongs: an engine started without ENGINE_SHARED_SECRET accepts
    every caller, because an empty expected key disables the check entirely.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-engine.ps1

    To survive logout/reboot, register it with Task Scheduler:
      schtasks /create /tn "IronCondorEngine" /sc onstart /rl highest ^
        /tr "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\scripts\run-engine.ps1"
#>
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8020,
    [string]$LogDir = "engine/state/logs"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir "engine.log"

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

# Secrets live in a gitignored file, never in the repo and never on a command
# line where they would land in the process list.
$envFile = Join-Path $root ".env.engine.local"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"').Trim("'")
            Set-Item -Path "env:$name" -Value $value
        }
    }
    Write-Log "Loaded environment from .env.engine.local"
} else {
    Write-Log "WARNING: no .env.engine.local found. Without ENGINE_SHARED_SECRET the engine accepts any caller."
}

# A stable salt keeps user ids -- and therefore saved runs -- resolvable across
# restarts. The database mints one on first use, so this is only a fallback.
if (-not $env:ENGINE_DB) { $env:ENGINE_DB = "engine/state/engine.db" }

$delay = 2
$maxDelay = 60

while ($true) {
    Write-Log "Starting engine on ${BindHost}:${Port}"
    $start = Get-Date

    & python -m uvicorn engine.api:app --host $BindHost --port $Port --log-level info
    $code = $LASTEXITCODE

    $ranFor = (Get-Date) - $start
    Write-Log "Engine exited with code $code after $([int]$ranFor.TotalSeconds)s"

    # A process that survived a while was healthy; reset the backoff so a
    # single crash after hours of uptime restarts immediately.
    if ($ranFor.TotalSeconds -gt 60) { $delay = 2 } else { $delay = [Math]::Min($delay * 2, $maxDelay) }

    Write-Log "Restarting in ${delay}s"
    Start-Sleep -Seconds $delay
}
