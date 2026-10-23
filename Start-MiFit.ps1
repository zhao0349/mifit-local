$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$pythonw = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
$app = Join-Path $taskRoot 'launch.py'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Python environment is missing. Please restore the deployment first.'
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Start-Process -FilePath $pythonw -ArgumentList @("`"$app`"") -WorkingDirectory $taskRoot -WindowStyle Hidden
