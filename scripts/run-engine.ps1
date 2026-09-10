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
#
# Re-read before every start, not once at launch. Loading it only at startup
# meant rotating the shared secret had no effect until somebody remembered to
# restart this supervisor too -- so the engine kept running wide open while
# both the rotation script and Vercel reported success.
$envFile = Join-Path $root ".env.engine.local"

function Import-EngineEnv {
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
}

Import-EngineEnv

# A stable salt keeps user ids -- and therefore saved runs -- resolvable across
# restarts. The database mints one on first use, so this is only a fallback.
if (-not $env:ENGINE_DB) { $env:ENGINE_DB = "engine/state/engine.db" }

# Only one supervisor, ever.
#
# Task Scheduler's MultipleInstancesPolicy only stops the *same task* running
# twice. It says nothing about a copy started by hand from a terminal, and two
# supervisors both restarting uvicorn against one port is not a harmless
# duplicate: whichever loses the bind exits 3, backs off, retries, and keeps
# losing -- so the log fills with restarts while the engine looks fine, and a
# stop of the winner hands the port to a process nobody knows about.
#
# Global\ so it spans sessions: the point is to catch the terminal-versus-task
# case, which Local\ would miss.
$createdNew = $false
$mutex = $null
try {
    $mutex = New-Object System.Threading.Mutex($true, "Global\IronCondorEngineSupervisor", [ref]$createdNew)
}
catch [System.UnauthorizedAccessException] {
    # A mutex we cannot open is one another session already holds.
    $createdNew = $false
}

if (-not $createdNew) {
    Write-Log "Another engine supervisor is already running; exiting rather than fighting it for port $Port."
    Write-Log "  Stop it first, or use that one:  Get-ScheduledTask IronCondor-Engine"
    return
}

$delay = 2
$maxDelay = 60

function Clear-OrphanedEngine {
    <#
        Kill a previous engine still holding the port.

        Stopping this supervisor does not stop its uvicorn child: PowerShell
        launches it synchronously, so a force-kill of the script orphans the
        child, which keeps the port and keeps serving. The next supervisor then
        cannot bind, exits 3, and loops on a 60s backoff forever -- while the
        orphan happily serves *old code* to every user.

        That is not hypothetical. It is how this project shipped a two-hour-old
        build for a whole session, and it happened again the moment the engine
        was restarted to pick up a change.

        Only processes that are clearly ours are killed: a uvicorn running
        engine.api. Anything else on the port is someone else's problem and
        gets reported rather than terminated.
    #>
    $owners = @()
    try {
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                  Select-Object -ExpandProperty OwningProcess -Unique
    }
    catch {
        return   # nothing listening
    }

    # Not $pid: that is a read-only automatic variable in PowerShell.
    foreach ($ownerPid in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($proc.CommandLine -match "uvicorn" -and $proc.CommandLine -match "engine\.api") {
            Write-Log "Port $Port held by an orphaned engine (pid $ownerPid); stopping it"
            try {
                Stop-Process -Id $ownerPid -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
            }
            catch {
                Write-Log "  could not stop pid ${ownerPid}: $($_.Exception.Message)"
            }
        }
        else {
            Write-Log "WARNING: port $Port is held by pid $ownerPid ($($proc.Name)), which is not ours."
            Write-Log "         The engine cannot start until that process releases it."
        }
    }
}

while ($true) {
    Import-EngineEnv
    Clear-OrphanedEngine
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
