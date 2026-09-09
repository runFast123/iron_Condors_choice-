<#
.SYNOPSIS
    Register the engine and tunnel as Windows Scheduled Tasks so they survive
    logout, reboot and idle.

.DESCRIPTION
    run-engine.ps1 already restarts uvicorn when it crashes, but nothing
    restarts run-engine.ps1 itself. Started from a terminal it lives exactly as
    long as that terminal, so a reboot -- or closing the window -- silently
    stops every forward run with positions still open.

    Task Scheduler's defaults are wrong for this, and wrong in ways that look
    like flakiness rather than configuration:

      * tasks are killed after 3 days (ExecutionTimeLimit)
      * they refuse to start on battery, and stop when the machine unplugs
      * they stop when the machine leaves idle
      * a failure is not retried

    All four are corrected here, which is why this registers from XML rather
    than a one-line schtasks call.

.PARAMETER AtStartup
    Start at boot instead of at logon. Needs an elevated shell -- a task that
    runs before anyone logs in is a machine-level change. Without it the tasks
    start when you log in, which needs no admin rights.

.PARAMETER NoTunnel
    Register only the engine. Use this if you run a named Cloudflare tunnel as
    its own service, or expose the engine some other way.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$AtStartup,
    [switch]$NoTunnel,
    [switch]$Uninstall,
    [string]$Prefix = "IronCondor"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$engineTask = "$Prefix-Engine"
$tunnelTask = "$Prefix-Tunnel"

function Remove-TaskIfPresent([string]$name) {
    # No stderr redirection, and errors non-terminating for this block.
    # Windows PowerShell 5.1 turns a native command's stderr into ErrorRecords
    # when redirected, so `schtasks /query` on a task that simply does not
    # exist -- the normal case here -- became a terminating error under
    # $ErrorActionPreference = "Stop". The same trap already broke the tunnel
    # script once.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Discard both streams: "task not found" is the expected case here and
        # schtasks reports it on stderr, which would otherwise print a scary
        # red block during a perfectly normal install.
        $null = & schtasks /query /tn $name 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            & schtasks /delete /tn $name /f | Out-Null
            Write-Output "  removed $name"
        }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

if ($Uninstall) {
    Write-Output "Removing scheduled tasks"
    Remove-TaskIfPresent $engineTask
    Remove-TaskIfPresent $tunnelTask
    Write-Output "Done. Anything already running keeps running until you stop it."
    return
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($AtStartup -and -not $isAdmin) {
    throw "-AtStartup needs an elevated PowerShell. Re-run as administrator, or drop the flag to start at logon instead."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$psExe = (Get-Command powershell.exe).Source

function New-TaskXml([string]$script, [string]$arguments, [string]$description) {
    $trigger = if ($AtStartup) {
        "<BootTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></BootTrigger>"
    } else {
        "<LogonTrigger><Enabled>true</Enabled><UserId>$user</UserId></LogonTrigger>"
    }
    $runLevel = if ($AtStartup) { "HighestAvailable" } else { "LeastPrivilege" }
    $logonType = if ($AtStartup) { "S4U" } else { "InteractiveToken" }

    $full = "-ExecutionPolicy Bypass -WindowStyle Hidden -NoProfile -File `"$root\$script`""
    if ($arguments) { $full += " $arguments" }

@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$description</Description>
  </RegistrationInfo>
  <Triggers>$trigger</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>$logonType</LogonType>
      <RunLevel>$runLevel</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <!-- The engine holds live forward runs. None of the usual reasons to stop
         a background task apply to it. -->
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <!-- PT0S is unlimited. The default kills a task after three days, which
         would look exactly like a random overnight failure. -->
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$psExe</Command>
      <Arguments>$full</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function Register-Task([string]$name, [string]$script, [string]$arguments, [string]$description) {
    Remove-TaskIfPresent $name
    $xml = New-TaskXml $script $arguments $description
    $path = Join-Path $env:TEMP "$name.xml"
    # Task Scheduler insists on UTF-16 for the XML it imports.
    [System.IO.File]::WriteAllText($path, $xml, [System.Text.Encoding]::Unicode)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & schtasks /create /tn $name /xml $path /f | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "schtasks returned $LASTEXITCODE for $name" }
        Write-Output "  registered $name"
    }
    finally {
        $ErrorActionPreference = $previous
        Remove-Item $path -ErrorAction SilentlyContinue
    }
}

Write-Output "Registering scheduled tasks ($(if ($AtStartup) { 'at boot' } else { 'at logon' }))"

Register-Task $engineTask "scripts\run-engine.ps1" "" `
    "Iron Condor engine: holds live forward runs and serves the dashboard."

if (-not $NoTunnel) {
    # A vendored copy first. A PATH lookup previously found a binary sitting in
    # a temp directory, which Windows eventually cleans up -- and the task then
    # fails every minute forever, with nothing obviously wrong.
    $vendored = Join-Path $root "tools\cloudflared.exe"
    $cfPath = if (Test-Path $vendored) {
        $vendored
    } else {
        $onPath = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($onPath) { $onPath.Source } else { $null }
    }

    if ($cfPath) {
        Register-Task $tunnelTask "scripts\run-tunnel.ps1" "-UpdateVercel -Cloudflared `"$cfPath`"" `
            "Iron Condor tunnel: exposes the engine and keeps Vercel pointed at it."
        Write-Output "  tunnel uses $cfPath"
    } else {
        Write-Warning "No cloudflared found, so the tunnel task was skipped."
        Write-Warning "Put it at tools\cloudflared.exe (or on PATH) and re-run."
    }
}

Write-Output ""
Write-Output "Start them now without waiting for a logon:"
Write-Output "  schtasks /run /tn $engineTask"
if (-not $NoTunnel) { Write-Output "  schtasks /run /tn $tunnelTask" }
Write-Output ""
Write-Output "Check on them:   schtasks /query /tn $engineTask /v /fo list"
Write-Output "Remove them:     powershell -File scripts\install-tasks.ps1 -Uninstall"
Write-Output ""
if (-not $AtStartup) {
    Write-Output "These start when you log in. To have them come up before login,"
    Write-Output "re-run in an elevated shell with -AtStartup."
}
