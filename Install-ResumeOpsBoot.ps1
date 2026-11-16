param([switch]$Start)
$ErrorActionPreference = 'Stop'
$taskName = 'ResumeOps Dashboard'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open an elevated PowerShell as the Codetrust deployment owner and run this script.'
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$supervisor = Join-Path $PSScriptRoot 'dashboard_supervisor.py'
$config = Get-Content (Join-Path $PSScriptRoot 'dashboard-config.json') -Raw | ConvertFrom-Json
if ($identity.Name.Split('\')[-1] -ne (Split-Path (Split-Path $config.project_root -Parent) -Leaf)) {
    throw 'Run this script as the account that owns the resume project.'
}
$state = Get-Content (Join-Path $PSScriptRoot 'release-state.json') -Raw | ConvertFrom-Json
& $python $supervisor verify $state.active --port 18765
if ($LASTEXITCODE) { throw 'Release preflight failed; leaving the current dashboard running.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('-u "' + $supervisor + '" run') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$verifyTask = 'ResumeOps Dashboard Preflight'
if (Get-ScheduledTask -TaskName $verifyTask -ErrorAction SilentlyContinue) {
    throw 'A dashboard preflight already exists.'
}
$verifyAction = New-ScheduledTaskAction -Execute $python -Argument ('-u "' + $supervisor + '" verify ' + $state.active + ' --port 18765') -WorkingDirectory $PSScriptRoot
Register-ScheduledTask -TaskName $verifyTask -Action $verifyAction -Principal $taskPrincipal -Settings $settings | Out-Null
try {
    Start-ScheduledTask -TaskName $verifyTask
    $deadline = (Get-Date).AddSeconds(50)
    do {
        Start-Sleep -Seconds 2
        $info = Get-ScheduledTaskInfo -TaskName $verifyTask
        $testState = (Get-ScheduledTask -TaskName $verifyTask).State
    } while (($info.LastRunTime.Year -lt 2000 -or $testState -eq 'Running') -and (Get-Date) -lt $deadline)
    if ($info.LastRunTime.Year -lt 2000 -or $testState -eq 'Running' -or $info.LastTaskResult -ne 0) {
        throw "Preflight under the boot account failed (result $($info.LastTaskResult)). The current listener is unchanged."
    }
} finally {
    Stop-ScheduledTask -TaskName $verifyTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $verifyTask -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force | Out-Null
if (-not $Start) { return }
$ids = @(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
if ($ids.Count -gt 1) { throw 'Ambiguous dashboard origin process.' }
if ($ids.Count -eq 1) {
    $listenerId = $ids[0]
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerId"
    if ($process.Name -ne 'python.exe' -or $process.CommandLine -notmatch '(?i)resume_dashboard\.py') {
        throw 'Unexpected origin process. No process was stopped.'
    }
    Stop-Process -Id $listenerId
}
Start-ScheduledTask -TaskName $taskName
$deadline = (Get-Date).AddSeconds(70)
do {
    Start-Sleep -Seconds 3
    & $python $supervisor check
    if ($LASTEXITCODE -eq 0) { Get-ScheduledTask -TaskName $taskName; return }
} while ((Get-Date) -lt $deadline)
throw 'Scheduled origin did not pass health checks. See logs\supervisor.log and logs\dashboard.log.'
