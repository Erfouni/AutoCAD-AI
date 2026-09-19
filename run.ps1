# Starts the AutoCAD MCP server.
#
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
#
# AutoCAD must already be running with a drawing open.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Process -Name acad -ErrorAction SilentlyContinue)) {
    Write-Warning "AutoCAD does not appear to be running."
    Write-Warning "Start AutoCAD and open a drawing, or the first tool call will try to launch it (slow)."
    Write-Host ""
}

# The port (8770 unless ACAD_MCP_PORT says otherwise) is decided in config.py,
# so `python server.py` and this script always agree.
python server.py
