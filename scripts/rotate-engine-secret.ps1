<#
.SYNOPSIS
    Generates a new ENGINE_SHARED_SECRET and sets it on both sides.

.DESCRIPTION
    The engine gates every route behind this secret -- but only when one is
    set. `check_engine_key` returns early on an empty expected value, so an
    engine started without it accepts every caller who can reach the tunnel.
    The tunnel URL is public and unauthenticated, so "no secret" means the
    whole API is open.

    Both sides have to agree, which is the awkward part: a new secret on the
    engine alone makes Vercel's calls 401, and a new secret on Vercel alone
    makes them 401 the other way. So this writes the value to
    .env.engine.local (gitignored, read by run-engine.ps1), pushes the *same*
    value to Vercel, redeploys, and then tells you to restart the engine.

    Run it whenever the secret may have been exposed, or when you do not know
    what the current one is -- there is no way to read it back from either
    side, by design.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\rotate-engine-secret.ps1
#>
[CmdletBinding()]
param(
    [string]$EnvFile = ".env.engine.local",
    [switch]$SkipVercel
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Step([string]$message) {
    Write-Output ("  {0}" -f $message)
}

# 32 bytes of CSPRNG output, URL-safe so it survives a header and an env var.
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

Write-Output "Rotating ENGINE_SHARED_SECRET"
Write-Step "generated $($secret.Length) characters"

# --- engine side -----------------------------------------------------------
$existing = @()
if (Test-Path $EnvFile) {
    $existing = Get-Content $EnvFile | Where-Object { $_ -notmatch '^\s*ENGINE_SHARED_SECRET\s*=' }
}
$lines = @($existing) + @("ENGINE_SHARED_SECRET=$secret")
Set-Content -Path $EnvFile -Value $lines -Encoding utf8
Write-Step "wrote $EnvFile (gitignored)"

& git check-ignore -q $EnvFile | Out-Null
$ignored = ($LASTEXITCODE -eq 0)
if (-not $ignored) {
    Write-Warning "$EnvFile is NOT gitignored. Do not commit it."
}

# --- Vercel side -----------------------------------------------------------
if ($SkipVercel) {
    Write-Step "skipped Vercel; set ENGINE_SHARED_SECRET there yourself"
}
else {
    # PowerShell 5.1 turns a native command's stderr into ErrorRecords when it
    # is redirected, so a successful npx call reads as a failure. No `2>&1`
    # here, and errors are non-terminating for this block.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & npx vercel env rm ENGINE_SHARED_SECRET production --yes | Out-Null
        $secret | & npx vercel env add ENGINE_SHARED_SECRET production | Out-Null
        Write-Step "set on Vercel (production)"
        & npx vercel --prod --yes | Select-Object -Last 1 | ForEach-Object { Write-Step $_ }
        Write-Step "redeployed"
    }
    catch {
        Write-Warning "Could not update Vercel: $($_.Exception.Message)"
        Write-Warning "The engine now expects a secret Vercel does not send, so the"
        Write-Warning "dashboard will get 401s until you set it there by hand."
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

Write-Output ""
Write-Output "Now restart the engine so it picks the value up:"
Write-Output "  powershell -ExecutionPolicy Bypass -File scripts\run-engine.ps1"
Write-Output ""
Write-Output "The value is in $EnvFile. It is not printed here and cannot be read"
Write-Output "back from Vercel -- rotate again rather than trying to recover it."
