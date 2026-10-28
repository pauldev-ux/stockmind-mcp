<#
.SYNOPSIS
    Levanta StockMind completo: el servidor MCP en HTTP y la API del chat. Ctrl+C detiene los dos.

.DESCRIPTION
    1. stockmind-mcp --http   (http://127.0.0.1:MCP_PORT/mcp, exige MCP_TOKEN)
    2. stockmind-api          (http://API_HOST:API_PORT/docs, exige API_TOKEN)
    Los dos leen el mismo .env. Si uno termina, se detiene el otro.

.EXAMPLE
    .\scripts\iniciar.ps1
#>
[CmdletBinding()]
param(
    [int]$PuertoMcp = 8765,
    [int]$EsperaSegundos = 20
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$raiz = Split-Path -Parent $PSScriptRoot
$python = Join-Path $raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No se encontró $python. Crea el entorno: py -3.14 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
}

function Esperar-Puerto([int]$puerto, [int]$segundos, $proceso) {
    $limite = (Get-Date).AddSeconds($segundos)
    while ((Get-Date) -lt $limite) {
        if ($proceso.HasExited) { return $false }
        $tcp = New-Object System.Net.Sockets.TcpClient
        try { $tcp.Connect("127.0.0.1", $puerto); return $true } catch { Start-Sleep -Milliseconds 300 } finally { $tcp.Dispose() }
    }
    return $false
}

$mcp = $null
$api = $null
try {
    Write-Host "Iniciando el servidor MCP (HTTP, puerto $PuertoMcp)..." -ForegroundColor Cyan
    $mcp = Start-Process -FilePath $python -ArgumentList @("-m", "stockmind.mcp_server", "--http", "--port", "$PuertoMcp") `
        -WorkingDirectory $raiz -NoNewWindow -PassThru
    if (-not (Esperar-Puerto $PuertoMcp $EsperaSegundos $mcp)) {
        throw "El servidor MCP no arrancó (¿falta MCP_TOKEN en .env? Mira los mensajes de arriba)."
    }

    Write-Host "Iniciando la API del chat..." -ForegroundColor Cyan
    $api = Start-Process -FilePath $python -ArgumentList @("-m", "stockmind.api") -WorkingDirectory $raiz `
        -NoNewWindow -PassThru
    Write-Host "Listo. Documentación de la API en http://<API_HOST>:<API_PORT>/docs (por defecto http://127.0.0.1:8000/docs). Ctrl+C detiene todo." -ForegroundColor Green

    while (-not $mcp.HasExited -and -not $api.HasExited) { Start-Sleep -Milliseconds 500 }
    if ($mcp.HasExited) { Write-Host "El servidor MCP terminó (código $($mcp.ExitCode))." -ForegroundColor Yellow }
    if ($api.HasExited) { Write-Host "La API terminó (código $($api.ExitCode))." -ForegroundColor Yellow }
}
finally {
    foreach ($p in @($api, $mcp)) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "StockMind detenido." -ForegroundColor Cyan
}
