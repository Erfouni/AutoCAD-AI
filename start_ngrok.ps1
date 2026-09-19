# Opens a public HTTPS tunnel to the AutoCAD MCP server and prints the URL to
# paste into ChatGPT or Manus.
#
#   powershell -ExecutionPolicy Bypass -File .\start_ngrok.ps1
#
# IMPORTANT: connect the VPN first. ngrok refuses agent connections from
# Iranian IP addresses (ERR_NGROK_9040) and the tunnel dies with the VPN.
#
# Note: a tunnel is probably already running against port 8765 for the Codex
# AutoCAD host. The free ngrok plan allows only one agent session at a time, so
# starting this will conflict unless that one is stopped first.

param(
    [int]$Port = 8770,
    # Defaults to %USERPROFILE%\mcp_setup\ngrok.exe, then to ngrok on PATH.
    [string]$NgrokExe = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not $NgrokExe) {
    $NgrokExe = Join-Path $env:USERPROFILE "mcp_setup\ngrok.exe"
    if (-not (Test-Path $NgrokExe)) {
        $onPath = Get-Command ngrok -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($onPath) { $NgrokExe = $onPath.Source }
    }
}

$secretFile = Join-Path $PSScriptRoot ".secret"
if (-not (Test-Path $secretFile)) {
    Write-Error "No .secret file yet. Start the server once (run.ps1) so it can generate one."
    exit 1
}
$secret = (Get-Content $secretFile -Raw).Trim()

if (-not (Test-Path $NgrokExe)) {
    Write-Error "ngrok not found at $NgrokExe. Pass -NgrokExe <path>."
    exit 1
}

# Fail early with a clear message rather than a confusing tunnel error.
try {
    $null = Invoke-RestMethod -Uri "https://api.ipify.org?format=json" -TimeoutSec 10
} catch {
    Write-Warning "No internet reachable. Is the VPN connected?"
}

Write-Host "Starting ngrok against 127.0.0.1:$Port ..."
Start-Process -FilePath $NgrokExe -ArgumentList "http", "$Port" -WindowStyle Minimized

# Wait for the agent's local API to report the public URL.
$publicUrl = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 3
        $match = $tunnels.tunnels | Where-Object { $_.config.addr -match ":$Port$" } | Select-Object -First 1
        if ($match) { $publicUrl = $match.public_url; break }
    } catch { }
}

if (-not $publicUrl) {
    Write-Error @"
ngrok did not report a tunnel for port $Port.
Most likely causes:
  1. The VPN is down -- ngrok blocks this IP range (ERR_NGROK_9040).
  2. Another ngrok agent is already running (free plan allows one session).
Check http://127.0.0.1:4040 and the ngrok window for the real error.
"@
    exit 1
}

$connector = "$publicUrl/$secret/mcp"

Write-Host ""
Write-Host ("=" * 70)
Write-Host "  Tunnel is up."
Write-Host ("=" * 70)
Write-Host "  Paste this into ChatGPT / Manus as the MCP server URL:"
Write-Host ""
Write-Host "    $connector"
Write-Host ""
Write-Host "  Health check: $publicUrl/$secret/health"
Write-Host ""
Write-Host "  Treat this URL as a password -- it is the only thing standing"
Write-Host "  between the internet and AutoCAD on this machine."
Write-Host ("=" * 70)
