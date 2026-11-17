#Requires -RunAsAdministrator
# Roll back Install-ResumeOpsWatcherBoot.ps1: remove the boot tasks, re-enable the sign-in tasks.
$ErrorActionPreference = 'Stop'
foreach ($task in 'ResumeOps Watcher (boot)', 'ResumeOps Monitor (boot)') {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Write-Host "Removed '$task'."
    }
}
foreach ($old in 'AI Resume Builder Watcher', 'AI Resume Builder Monitor') {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Enable-ScheduledTask -TaskName $old | Out-Null
        Write-Host "Re-enabled '$old'."
    }
}
