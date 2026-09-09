<#
.SYNOPSIS
    Exposes the local engine to Vercel, and keeps ENGINE_URL pointing at it.

.DESCRIPTION
    A quick tunnel (trycloudflare.com) mints a NEW hostname every time it
    starts. Vercel stores ENGINE_URL as a fixed string, so every restart of the
    tunnel silently breaks the deployed dashboard until someone notices and
    re-pastes the URL. That is the failure this script removes: it starts the
    tunnel, reads the hostname out of cloudflared's own output, and pushes it to
    Vercel before handing back control.

    A quick tunnel is still the wrong long-term answer -- it has no
    authentication, no stable name, and Cloudflare may retire the URL at any
    time. Use -Named with a real Cloudflare tunnel once you have a domain:

      cloudflared tunnel login
      cloudflared tunnel create iron-condor
      cloudflared tunnel route dns iron-condor engine.yourdomain.com
      .\scripts\run-tunnel.ps1 -Named iron-condor -Hostname engine.yourdomain.com

    A named tunnel keeps one hostname forever, so ENGINE_URL is set once and
    never touched again.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-tunnel.ps1 -UpdateVercel
#>
[CmdletBinding()]
param(
    [int]$Port = 8020,
    [string]$Cloudflared = "cloudflared",
    [string]$Named = "",
    [string]$TunnelHostname = "",
    [switch]$UpdateVercel,
    [string]$LogDir = "engine/state/logs",
    # How often to ask the public URL whether it still reaches the engine, and
    # how many consecutive failures before restarting the tunnel. Two minutes
    # of silence is a real outage; one failed probe is usually a blip.
    [int]$CheckSeconds = 60,
    [int]$FailuresBeforeRestart = 2
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir "tunnel.log"

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

function Set-VercelEngineUrl([string]$url) {
    # Vercel has no "update"; the value has to be removed and re-added.
    #
    # Windows PowerShell 5.1 turns a native command's stderr into ErrorRecords
    # when you redirect it, so `2>&1` on npx made a *successful* vercel call
    # look like a failure -- and with $ErrorActionPreference = "Stop" that
    # killed the tunnel this function exists to publish. So: no stderr
    # redirection here, and errors are non-terminating for this block only.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        Write-Log "Pointing Vercel ENGINE_URL at $url"
        & npx vercel env rm ENGINE_URL production --yes | Out-Null
        $url | & npx vercel env add ENGINE_URL production | Out-Null
        Write-Log "Redeploying so the new value is picked up"
        & npx vercel --prod --yes | Select-Object -Last 3 | ForEach-Object { Write-Log $_ }
        Write-Log "Vercel now points at $url"
    }
    catch {
        # A failed publish must never take the tunnel down with it: the tunnel
        # is still useful, and the URL can be pasted into Vercel by hand.
        Write-Log "WARNING: could not update Vercel ($($_.Exception.Message))."
        Write-Log "         Set ENGINE_URL to $url manually, then redeploy."
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

if ($Named) {
    if (-not $TunnelHostname) { throw "-Named also needs -TunnelHostname (the DNS route you created)." }
    Write-Log "Starting named tunnel '$Named' -> https://$TunnelHostname"
    if ($UpdateVercel) { Set-VercelEngineUrl "https://$TunnelHostname" }
    # A named tunnel keeps its hostname across restarts, so this can just run.
    while ($true) {
        & $Cloudflared tunnel run --url "http://127.0.0.1:$Port" $Named
        Write-Log "Named tunnel exited; restarting in 5s"
        Start-Sleep -Seconds 5
    }
}

# ---- quick tunnel: capture whatever hostname Cloudflare hands out this time
while ($true) {
    Write-Log "Starting quick tunnel to http://127.0.0.1:$Port"
    $out = Join-Path $LogDir "cloudflared.out"
    Remove-Item $out -ErrorAction SilentlyContinue

    $proc = Start-Process -FilePath $Cloudflared `
        -ArgumentList "tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate" `
        -RedirectStandardError $out -RedirectStandardOutput "$out.stdout" `
        -NoNewWindow -PassThru

    # cloudflared prints the URL to stderr a second or two after launch.
    $url = $null
    for ($i = 0; $i -lt 60 -and -not $url; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $out) {
            $match = Select-String -Path $out -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" |
                     Select-Object -First 1
            if ($match) { $url = $match.Matches[0].Value }
        }
        if ($proc.HasExited) { break }
    }

    if ($url) {
        Write-Log "Tunnel is up at $url"
        # No BOM. PowerShell 5.1's `-Encoding utf8` writes one, and anything
        # that reads this file then gets "﻿https://..." -- a URL that
        # looks perfectly correct on screen and fails every request.
        [System.IO.File]::WriteAllText(
            (Join-Path $LogDir "tunnel-url.txt"), $url,
            (New-Object System.Text.UTF8Encoding $false)
        )
        if ($UpdateVercel) { Set-VercelEngineUrl $url }
    } else {
        Write-Log "WARNING: could not read a tunnel URL from cloudflared output"
    }

    # Watch the tunnel, not just the process.
    #
    # Waiting on exit only catches cloudflared *dying*. A quick tunnel can stop
    # routing while the process sits there perfectly healthy -- Cloudflare
    # retires the hostname, or the edge connection drops and does not recover.
    # From the outside that is indistinguishable from the engine being down,
    # and it presents as "Engine unreachable" on the login page with nothing in
    # any log to explain it. So: ask the public URL whether it still reaches
    # the engine, and if it stops answering, restart and republish.
    $failures = 0
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds $CheckSeconds

        if (-not $url) { continue }
        $alive = $false
        try {
            $probe = Invoke-WebRequest -Uri "$url/health" -TimeoutSec 20 -UseBasicParsing
            $alive = ($probe.StatusCode -eq 200)
        }
        catch {
            $alive = $false
        }

        if ($alive) {
            if ($failures -gt 0) { Write-Log "Tunnel answering again after $failures failed check(s)" }
            $failures = 0
            continue
        }

        $failures++
        Write-Log "Tunnel health check failed ($failures/$FailuresBeforeRestart) for $url"
        if ($failures -ge $FailuresBeforeRestart) {
            # The engine itself may simply be down -- restarting the tunnel
            # would not help and would needlessly remint the URL, so check
            # locally before blaming the tunnel.
            $engineUp = $false
            try {
                $local = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 -UseBasicParsing
                $engineUp = ($local.StatusCode -eq 200)
            }
            catch { $engineUp = $false }

            if (-not $engineUp) {
                Write-Log "  engine is down too, so this is not the tunnel's fault; waiting"
                $failures = 0
                continue
            }

            Write-Log "  engine is up but unreachable through the tunnel; restarting it"
            try { $proc.Kill() } catch { }
            break
        }
    }

    if (-not $proc.HasExited) { try { $proc.WaitForExit(10000) } catch { } }
    Write-Log "Tunnel exited with code $($proc.ExitCode); restarting in 5s"
    Start-Sleep -Seconds 5
}
