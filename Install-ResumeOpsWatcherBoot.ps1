#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Run the ResumeOps watcher and monitor at Windows startup, without anyone signing in.

.DESCRIPTION
  Registers "ResumeOps Watcher (boot)" and "ResumeOps Monitor (boot)" to run as YOUR account
  whether or not you are signed in. Windows asks for your password in its own dialog and keeps it
  in Task Scheduler; this script never stores, prints or sends it.

  A password logon is used on purpose: it lets the watcher read the Chrome profile that holds
  the Overleaf session (Windows protects it with your credentials). A password-less (S4U) task
  starts, but cannot use that session, so Overleaf would fail after every reboot.

  The old sign-in tasks are disabled, not deleted. Undo everything with
  Uninstall-ResumeOpsWatcherBoot.ps1. If both ever run, the second watcher stands by.
#>
param([string]$Model = 'qwen2.5:7b')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $account -Message 'ResumeOps: enter your Windows password so the watcher can run at startup without signing in.'
if (-not $credential) { throw 'No credential entered; nothing was changed.' }
$password = $credential.GetNetworkCredential().Password

$launch = Join-Path $PSScriptRoot 'start_windows.ps1'
$monitor = Join-Path $PSScriptRoot 'monitor_windows.ps1'
$watchAction = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $PSScriptRoot `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $launch + '" -Model "' + $Model + '"')
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = 'PT1M'   # let networking and the GPU driver come up first
$watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ResumeOps Watcher (boot)' -Action $watchAction -Trigger $boot `
    -Settings $watchSettings -User $account -Password $password -RunLevel Limited -Force | Out-Null

$monitorAction = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $PSScriptRoot `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $monitor + '"')
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
$monitorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ResumeOps Monitor (boot)' -Action $monitorAction -Trigger @($boot, $every5) `
    -Settings $monitorSettings -User $account -Password $password -RunLevel Limited -Force | Out-Null
$password = $null

foreach ($old in 'AI Resume Builder Watcher', 'AI Resume Builder Monitor') {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $old | Out-Null
        Write-Host "Disabled the sign-in task '$old' (kept for rollback)."
    }
}
Write-Host 'Registered. The running watcher keeps working; the boot task takes over at the next restart.'
Write-Host 'Test it: restart Windows and do NOT sign in. After ~3 minutes https://resumeops.tech should show "Watcher online".'
Get-ScheduledTask -TaskName 'ResumeOps Watcher (boot)', 'ResumeOps Monitor (boot)' | Format-Table TaskName, State
