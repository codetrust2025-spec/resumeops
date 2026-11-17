$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Path 'logs' -Force | Out-Null
$message = $null
try {
    $health = Get-Content 'inbox\.resume-bot-health.json' -Raw | ConvertFrom-Json
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $ollama = $true
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5 | Out-Null } catch { $ollama = $false }
    if (($now - [double]$health.last_scan) -gt 300) {
        $message = 'Resume Builder watcher heartbeat is stale (no scan for over 5 minutes).'
    } elseif (-not $ollama) {
        $message = 'Ollama is not answering on the RTX laptop; resume jobs will wait and retry.'
    } elseif ($health.overleaf -and $health.overleaf.ok -eq $false) {
        $message = 'Overleaf sign-in required: run Sign_In_Overleaf.bat on the RTX laptop, then retry the job.'
    } elseif ($health.mongodb -and $health.mongodb.ok -eq $false) {
        $message = 'MongoDB is unreachable; ATS records are queued locally and will be retried.'
    } elseif ($health.failures -gt 0) {
        $message = 'Resume Builder has candidate jobs awaiting recovery.'
    }
} catch {
    $message = 'Resume Builder watcher heartbeat is unavailable.'
}
$alert = 'logs\current-alert.txt'
if (-not $message) {
    Remove-Item $alert -ErrorAction SilentlyContinue
    exit 0
}
$send = -not (Test-Path $alert)
if (-not $send) {
    $send = ((Get-Date) - (Get-Item $alert).LastWriteTime).TotalMinutes -ge 30
}
if ($send) {
    ('{0:o} {1}' -f [DateTimeOffset]::Now, $message) | Set-Content $alert
    Add-Content 'logs\alerts.log' (Get-Content $alert)
    $webhook = $env:RESUME_BUILDER_ALERT_WEBHOOK
    if (-not $webhook) {
        $webhook = [Environment]::GetEnvironmentVariable('RESUME_BUILDER_ALERT_WEBHOOK', 'User')
    }
    if ($webhook) {
        $uri = [uri]$webhook
        if ($uri.Scheme -ne 'https') { throw 'Alert webhook must use HTTPS' }
        try {
            $body = @{ text = $message } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json' -TimeoutSec 10 -Body $body | Out-Null
        } catch {
            Add-Content 'logs\alerts.log' 'Webhook alert delivery failed.'
            throw 'Webhook alert delivery failed. Check the configured endpoint.'
        }
    }
}
exit 1
