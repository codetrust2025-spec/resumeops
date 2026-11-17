param([ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/-]*$')][string]$Model = 'qwen2.5:7b')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:Path = (Join-Path $PSScriptRoot '.venv\Scripts') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
New-Item -ItemType Directory -Path 'logs' -Force | Out-Null
$log = Join-Path $PSScriptRoot 'logs\watcher.log'
$browserProfile = Join-Path $env:LOCALAPPDATA 'ResumeBuilderChrome'
$command = 'python -u -m resume_bot watch --inbox inbox --output output --model "' + $Model + '" --interval 30 --overleaf --browser-profile "' + $browserProfile + '" >> "' + $log + '" 2>&1'
function Test-Ollama {
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

# At boot nobody has signed in, so the Ollama tray app is not running. Start the Ollama server
# for this account if it is not already answering; the watcher retries jobs until it is.
function Start-OllamaIfNeeded {
    if (Test-Ollama) { return }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    $command = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    if ($command) { $candidates = @($command.Source) + $candidates }
    $exe = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $exe) {
        Add-Content -LiteralPath $log -Value ('{0:o} Ollama is not answering and ollama.exe was not found' -f [DateTimeOffset]::Now)
        return
    }
    Add-Content -LiteralPath $log -Value ('{0:o} Ollama is not answering; starting {1} serve' -f [DateTimeOffset]::Now, $exe)
    Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs\ollama-serve.log') `
        -RedirectStandardError (Join-Path $PSScriptRoot 'logs\ollama-serve.error.log')
    for ($i = 0; $i -lt 30 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 2 }
    $state = if (Test-Ollama) { 'Ollama is answering' } else { 'Ollama did not answer within 60 seconds' }
    Add-Content -LiteralPath $log -Value ('{0:o} {1}' -f [DateTimeOffset]::Now, $state)
}

while ($true) {
    Start-OllamaIfNeeded
    if ((Test-Path $log) -and (Get-Item $log).Length -ge 5MB) {
        Move-Item $log ($log + '.previous') -Force
    }
    Add-Content -LiteralPath $log -Value ('{0:o} Watcher starting' -f [DateTimeOffset]::Now)
    & cmd.exe /d /c $command
    Add-Content -LiteralPath $log -Value ('{0:o} Watcher exited with code {1}; restarting in 30 seconds' -f [DateTimeOffset]::Now, $LASTEXITCODE)
    Start-Sleep -Seconds 30
}
