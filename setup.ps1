# Setup script for Beets Web Manager Docker Compose installation (Windows).
param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Checking Docker..."
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is not installed. Install Docker Desktop first: https://docs.docker.com/get-docker/"
    exit 1
}
try { docker compose version | Out-Null } catch {
    Write-Error "Docker Compose v2 ('docker compose') is required."
    exit 1
}
try { docker info | Out-Null } catch {
    Write-Error "Docker daemon is not running. Start Docker Desktop and re-run this script."
    exit 1
}

Write-Host "==> Creating persistent data directories..."
New-Item -ItemType Directory -Force -Path "web-manager-data" | Out-Null
if ($Dev) {
    New-Item -ItemType Directory -Force -Path "config", "data\music", "data\downloads" | Out-Null
}

# Set/replace KEY=VALUE in .env without -replace's regex-replacement-text
# semantics: a value containing '$&', '$0', or '$$' (all reachable from
# common password characters) is reinterpreted by .NET's Regex.Replace as a
# backreference/whole-match token instead of being written literally.
# Deleting the old line and appending the new one sidesteps that class of
# bug -- Add-Content never reinterprets its -Value argument.
function Set-EnvValue {
    param([string]$Key, [string]$Value)
    if (Test-Path ".env") {
        (Get-Content ".env") | Where-Object { $_ -notmatch "^$([regex]::Escape($Key))=" } | Set-Content ".env"
    }
    Add-Content -Path ".env" -Value "$Key=$Value"
}

$isInteractive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

if (Test-Path ".env") {
    Write-Host "==> .env already exists, leaving existing secrets untouched."
    $freshEnv = $false
} else {
    Write-Host "==> Creating .env from .env.example..."
    Copy-Item ".env.example" ".env"
    $freshEnv = $true
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $apiBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($apiBytes)
    $apiToken = -join ($apiBytes | ForEach-Object { $_.ToString("x2") })
    (Get-Content ".env") -replace '^BEETS_WEB_AUTH_TOKEN=.*', "BEETS_WEB_AUTH_TOKEN=$token" | Set-Content ".env"
    (Get-Content ".env") -replace '^BEETS_API_TOKEN=.*', "BEETS_API_TOKEN=$apiToken" | Set-Content ".env"
    Write-Host "    Generated random BEETS_WEB_AUTH_TOKEN and BEETS_API_TOKEN in .env."
}

# Interactive Web Access prompt -- only on a genuinely fresh .env, so
# re-running setup.ps1 on an existing install never silently changes an
# already-configured bind address. .env.example ships BEETS_WEB_BIND_ADDRESS
# with a non-empty default (127.0.0.1), so "is it empty" can't gate this the
# way it gates the password prompt below.
if ($freshEnv -and $isInteractive) {
    Write-Host ""
    Write-Host "=== Web Access ==="
    Write-Host "1. This computer only (127.0.0.1)"
    Write-Host "2. Other devices on my local network (0.0.0.0)"
    Write-Host ""
    $bindChoice = Read-Host "Choose [2 for most NAS/server installs]"
    if ($bindChoice -eq "1") {
        $bindAddr = "127.0.0.1"
    } else {
        $bindAddr = "0.0.0.0"
    }
    Set-EnvValue -Key "BEETS_WEB_BIND_ADDRESS" -Value $bindAddr
    if ($bindAddr -eq "0.0.0.0") {
        Write-Host "    Set BEETS_WEB_BIND_ADDRESS=0.0.0.0 in .env."
        Write-Host "    This is the LISTENING address, not a browser URL -- from another device on your network, browse to:"
        Write-Host "      http://<this-machine's-LAN-IP>:8337"
        Write-Host "    Find this machine's LAN IP with 'ipconfig', or your NAS's network settings page."
    } else {
        Write-Host "    Set BEETS_WEB_BIND_ADDRESS=127.0.0.1 in .env -- only reachable from this computer, at http://localhost:8337"
    }
}

# Interactive Browser Login Prompt if BEETS_WEB_PASSWORD is not configured
$webPassLine = Select-String -Path ".env" -Pattern '^BEETS_WEB_PASSWORD=' | Select-Object -First 1
$webPassVal = if ($webPassLine) { ($webPassLine.Line -split '=', 2)[1].Trim() } else { "" }
if (-not $webPassVal -and $isInteractive) {
    Write-Host ""
    Write-Host "=== Browser Login Setup ==="
    Write-Host "This is the username and password you will use to open Beets Web Manager in your browser."
    Write-Host "Note: BEETS_WEB_AUTH_TOKEN (API bearer token) and BEETS_API_TOKEN (engine token) are separate internal tokens."
    Write-Host ""
    $inputUser = Read-Host "Browser username [admin]"
    $webUsername = if ([string]::IsNullOrWhiteSpace($inputUser)) { "admin" } else { $inputUser.Trim() }

    $webPassword = ""
    while ($true) {
        $secPass = Read-Host "Browser password (min 32 chars, upper, lower, number, special)" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secPass)
        $webPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        if ([string]::IsNullOrEmpty($webPassword)) {
            Write-Host "Password cannot be empty."
            continue
        }
        $errs = @()
        if ($webPassword.Length -lt 32) { $errs += "at least 32 characters" }
        if ($webPassword -notmatch '[A-Z]') { $errs += "an uppercase letter" }
        if ($webPassword -notmatch '[a-z]') { $errs += "a lowercase letter" }
        if ($webPassword -notmatch '[0-9]') { $errs += "a number" }
        if ($webPassword -notmatch '[^a-zA-Z0-9]') { $errs += "a special character" }

        if ($errs.Count -eq 0) {
            break
        } else {
            Write-Host "Password does not meet requirements: $($errs -join ', ')."
        }
    }

    Set-EnvValue -Key "BEETS_WEB_USERNAME" -Value $webUsername
    Set-EnvValue -Key "BEETS_WEB_PASSWORD" -Value $webPassword
    Write-Host "    Configured browser username ($webUsername) and password in .env."
}

# Validation
$apiTokenLine = Select-String -Path ".env" -Pattern '^BEETS_API_TOKEN=' | Select-Object -First 1
$apiTokenVal = if ($apiTokenLine) { ($apiTokenLine.Line -split '=', 2)[1].Trim() } else { "" }
if (-not $apiTokenVal -or $apiTokenVal -eq "changeme") {
    Write-Warning "BEETS_API_TOKEN is unconfigured or set to 'changeme' placeholder."
    Write-Warning "Please set BEETS_API_TOKEN in .env to match your Beets control agent."
}

$apiUrlLine = Select-String -Path ".env" -Pattern '^BEETS_API_URL=' | Select-Object -First 1
$apiUrlVal = if ($apiUrlLine) { ($apiUrlLine.Line -split '=', 2)[1].Trim() } else { "" }
if (-not $apiUrlVal) {
    if ($Dev) {
        Write-Warning "BEETS_API_URL is empty in .env. Defaulting to http://beets:8338 (docker-compose.dev.yml)."
    } else {
        Write-Warning "BEETS_API_URL is empty in .env. docker-compose.yml requires BEETS_API_URL to be set -- the container will fail to start without it."
    }
}

$composeFile = "docker-compose.yml"
if ($Dev) {
    Write-Host "==> Starting Beets Web Manager in DEVELOPMENT mode (source build)..."
    $composeFile = "docker-compose.dev.yml"
    docker compose -f docker-compose.dev.yml up -d --build
} else {
    Write-Host "==> Pulling published image from GitHub Container Registry..."
    docker compose pull beets-web-manager
    Write-Host "==> Starting Beets Web Manager..."
    docker compose up -d beets-web-manager
}

Write-Host "==> Waiting for Beets Web Manager to become healthy..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    $status = docker compose -f $composeFile ps --format '{{.Health}}' 2>$null
    if ($status -match "healthy") {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds 2
}

$portLine = Select-String -Path ".env" -Pattern '^WEBCONTROL_PORT=' | Select-Object -First 1
$port = if ($portLine) { ($portLine.Line -split '=')[1].Trim() } else { "8337" }
$bindAddrLine = Select-String -Path ".env" -Pattern '^BEETS_WEB_BIND_ADDRESS=' | Select-Object -First 1
$bindAddrFinal = if ($bindAddrLine) { ($bindAddrLine.Line -split '=', 2)[1].Trim() } else { "" }

if ($healthy) {
    Write-Host ""
    Write-Host "SUCCESS: Beets Web Manager is running and healthy."
    Write-Host "Access the UI at: http://localhost:$port"
    if ($bindAddrFinal -eq "0.0.0.0") {
        Write-Host "It is also reachable from other devices on your network at:"
        Write-Host "  http://<this-machine's-LAN-IP>:$port"
        Write-Host "(find this machine's LAN IP with 'ipconfig')"
    }
} else {
    Write-Host ""
    Write-Error "Beets Web Manager did not reach healthy state within 60 seconds. Check logs with: docker compose -f $composeFile logs beets-web-manager"
    exit 1
}
