param([ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/-]*$')][string]$Model = 'qwen2.5:7b')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:Path = (Join-Path $PSScriptRoot '.venv\Scripts') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
New-Item -ItemType Directory -Path 'logs' -Force | Out-Null
$log = Join-Path $PSScriptRoot 'logs\watcher.log'
$browserProfile = Join-Path $env:LOCALAPPDATA 'ResumeBuilderChrome'
$command = 'python -u -m resume_bot watch --inbox inbox --output output --model "' + $Model + '" --interval 30 --overleaf --browser-profile "' + $browserProfile + '" >> "' + $log + '" 2>&1'
while ($true) {
    if ((Test-Path $log) -and (Get-Item $log).Length -ge 5MB) {
        Move-Item $log ($log + '.previous') -Force
    }
    Add-Content -LiteralPath $log -Value ('{0:o} Watcher starting' -f [DateTimeOffset]::Now)
    & cmd.exe /d /c $command
    Add-Content -LiteralPath $log -Value ('{0:o} Watcher exited with code {1}; restarting in 30 seconds' -f [DateTimeOffset]::Now, $LASTEXITCODE)
    Start-Sleep -Seconds 30
}
