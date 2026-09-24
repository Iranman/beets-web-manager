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
New-Item -ItemType Directory -Force -Path "beets", "music", "downloads", "web-manager" | Out-Null

if (-not (Test-Path "beets\config.yaml") -and (Test-Path "config.yaml.example")) {
    Copy-Item "config.yaml.example" "beets\config.yaml"
    Write-Host "    Initialized default beets\config.yaml from template."
}

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
    Set-EnvValue -Key "BEETS_WEB_AUTH_TOKEN" -Value $token
    Write-Host "    Generated random BEETS_WEB_AUTH_TOKEN in .env."
}

# Interactive Web Access prompt
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

$composeFile = "docker-compose.yml"
if ($Dev) {
    Write-Host "==> Starting Beets stack in DEVELOPMENT mode (source build)..."
    $composeFile = "docker-compose.dev.yml"
    docker compose -f docker-compose.dev.yml up -d --build
} else {
    Write-Host "==> Pulling published images from GitHub Container Registry..."
    docker compose pull
    Write-Host "==> Starting Beets stack (beets engine + beets-web-manager)..."
    docker compose up -d
}

Write-Host "==> Waiting for services to become healthy..."
$healthy = $false
for ($i = 0; $i -lt 45; $i++) {
    $status = docker compose -f $composeFile ps --format '{{.Health}}' 2>$null
    if (-not $status) {
        $status = docker compose -f $composeFile ps 2>$null
    }
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

$lanIp = try {
    (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
     Where-Object { $_.InterfaceAlias -notmatch 'vEthernet|Loopback|WSL' -and $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.254\.' } |
     Select-Object -First 1).IPAddress
} catch { "<LAN-IP>" }
if (-not $lanIp) { $lanIp = "<LAN-IP>" }

if ($healthy) {
    Write-Host ""
    Write-Host "======================================================================"
    Write-Host "SUCCESS: Beets and Beets Web Manager are running and healthy!"
    Write-Host ""
    Write-Host "Open the Web UI in your browser:"
    Write-Host "  Local:   http://localhost:$port"
    if ($bindAddrFinal -eq "0.0.0.0") {
        Write-Host "  Network: http://${lanIp}:$port"
    }
    Write-Host ""
    Write-Host "Complete initial setup and configure your admin login in the browser."
    Write-Host ""
    Write-Host "Management commands:"
    Write-Host "  View logs:   docker compose -f $composeFile logs -f"
    Write-Host "  Stop stack:  docker compose -f $composeFile down"
    Write-Host "  Restart:     docker compose -f $composeFile restart"
    Write-Host "======================================================================"
} else {
    Write-Host ""
    Write-Error "Services did not reach healthy state within 90 seconds. Check logs with: docker compose -f $composeFile logs"
    exit 1
}
