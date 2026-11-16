# Run once from PowerShell in the extracted project directory.
# Prepares Python dependencies, Ollama model and optionally authorized browser login.
# Must not bypass Overleaf login/OTP/CAPTCHA or silently install system software.
param(
  [switch]$ConnectOverleaf,
  [switch]$CreateLogonTask,
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/-]*$')]
  [string]$Model = 'qwen2.5:7b'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:Path = $env:Path + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
function Need($program, $hint) {
  if (-not (Get-Command $program -ErrorAction SilentlyContinue)) {
    throw "$program is missing. $hint"
  }
}
Need 'py' 'Install Python 3.10+ from python.org and enable the Python launcher.'
Need 'pdflatex' 'Install MiKTeX or TeX Live with lmodern, geometry, titlesec, enumitem and hyperref.'
Need 'pdfinfo' 'Install Poppler for Windows, then add its bin directory to PATH.'
Need 'pdftotext' 'Install Poppler for Windows, then add its bin directory to PATH.'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & py -m venv .venv
  if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
& $python -m pip install playwright==1.62.0
if ($LASTEXITCODE -ne 0) { throw 'Playwright Python installation failed' }
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Playwright browser installation failed' }
try {
  $models = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
} catch {
  throw 'Local Ollama server is not running. Start Ollama, then run setup_windows.ps1 again.'
}
$available = @($models.models | Where-Object { $_.name -eq $Model -or $_.model -eq $Model })
if ($available.Count -eq 0) {
  throw "Requested model $Model is missing. Run ollama list and select an installed model using -Model."
}
if ($ConnectOverleaf) {
  Write-Host 'Use the opened browser to authenticate with Overleaf as the account owner.'
  $browserProfile = Join-Path $env:LOCALAPPDATA 'ResumeBuilderChrome'
  & $python -m resume_bot overleaf-login --browser-profile $browserProfile
  if ($LASTEXITCODE -ne 0) { throw 'Overleaf sign-in was not completed' }
}
if ($CreateLogonTask) {
  $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
  $launch = Join-Path $PSScriptRoot 'start_windows.ps1'
  $watchArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $launch + '" -Model "' + $Model + '"'
  $watchAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $watchArguments -WorkingDirectory $PSScriptRoot
  $logon = New-ScheduledTaskTrigger -AtLogOn -User $account
  $watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName 'AI Resume Builder Watcher' -Action $watchAction -Trigger $logon -Principal $principal -Settings $watchSettings -Force | Out-Null
  $monitor = Join-Path $PSScriptRoot 'monitor_windows.ps1'
  $monitorArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $monitor + '"'
  $monitorAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $monitorArguments -WorkingDirectory $PSScriptRoot
  $monitorTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
  $monitorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName 'AI Resume Builder Monitor' -Action $monitorAction -Trigger $monitorTrigger -Principal $principal -Settings $monitorSettings -Force | Out-Null
  Start-ScheduledTask -TaskName 'AI Resume Builder Watcher'
}
& $python -m resume_bot doctor --model $Model
