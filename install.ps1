# One-command installer for maxbot (Windows PowerShell).
# Usage: .\install.ps1 [-Name my-bot]
param([string]$Name = "my-bot")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python not found on PATH. Install Python 3.10+ first." }
& $py "$here\install.py" --name $Name
