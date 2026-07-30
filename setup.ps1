# One-command setup for the Docker Compose installation (Windows).
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

Write-Host "==> Creating local directories..."
New-Item -ItemType Directory -Force -Path "config", "data\music", "data\downloads", "backups", "web-manager-data" | Out-Null

if (Test-Path ".env") {
    Write-Host "==> .env already exists, leaving it untouched."
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
    # BEETS_API_TOKEN ships as the placeholder "changeme" in .env.example, which
    # the Beets control agent's beets_api_token_is_usable() rejects at startup --
    # leaving it unset here means the beets engine container fails to start on
    # first run.
    (Get-Content ".env") -replace '^BEETS_API_TOKEN=.*', "BEETS_API_TOKEN=$apiToken" | Set-Content ".env"
    Write-Host "    Generated random BEETS_WEB_AUTH_TOKEN and BEETS_API_TOKEN values in .env (not printed here)."
    Write-Host "    Edit .env now to add AI/Plex/AcoustID/Lidarr credentials, or configure them later in the app."
}

if (Test-Path "config.yaml") {
    Write-Host "==> config.yaml already exists, leaving it untouched."
} else {
    Write-Host "==> Creating config.yaml from config.yaml.example..."
    Copy-Item "config.yaml.example" "config.yaml"
}

Write-Host "==> Building and starting the stack..."
docker compose up -d --build

Write-Host "==> Waiting for the app to become healthy..."
for ($i = 0; $i -lt 30; $i++) {
    $health = docker compose ps --format '{{.Health}}' 2>$null
    if ($health -match "healthy") { break }
    Start-Sleep -Seconds 2
}

$portLine = Select-String -Path ".env" -Pattern '^WEBCONTROL_PORT=' | Select-Object -First 1
$port = if ($portLine) { ($portLine.Line -split '=')[1] } else { "8337" }

Write-Host ""
Write-Host "Done. Open http://localhost:$port in your browser."
Write-Host "If this is a fresh install, complete the guided setup at http://localhost:$port/setup."
