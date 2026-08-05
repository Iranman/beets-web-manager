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

if (Test-Path ".env") {
    Write-Host "==> .env already exists, leaving existing secrets untouched."
} else {
    Write-Host "==> Creating .env from .env.example..."
    Copy-Item ".env.example" ".env"
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

if ($healthy) {
    Write-Host ""
    Write-Host "SUCCESS: Beets Web Manager is running and healthy."
    Write-Host "Access the UI at: http://localhost:$port"
} else {
    Write-Host ""
    Write-Error "Beets Web Manager did not reach healthy state within 60 seconds. Check logs with: docker compose -f $composeFile logs beets-web-manager"
    exit 1
}
