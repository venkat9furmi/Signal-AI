<#
    Watch the watchers.

    Docker restarts a crashed container, but nothing restarts Docker. On
    2026-10-25 this machine rebooted at 01:38, Docker Desktop did not start
    (its own AutoStart setting is off), and monitoring was down for eleven
    hours before anyone noticed. That is the gap this fills.

    Every minute it asks the page whether it is alive. If it is not, it tries
    to heal - start Docker Desktop, then `docker compose up -d` - and if the
    page is still dead after two consecutive checks it says so in Slack, once,
    and again when it recovers. Slack is the right channel for this: it is
    where the commentators already are.

    No admin rights needed: it runs from the Startup folder as the logged-in
    user. It cannot help if nobody logs in after a reboot - a permanent
    always-on host is the real answer, and this is the cheap version.

    Usage: powershell -NoProfile -ExecutionPolicy Bypass -File uptime-guard.ps1
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root "uptime-guard.log"
$page = "http://127.0.0.1:8642/api/channels"
$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$CHECK_SECONDS = 60
#: Two failed checks in a row before shouting: a single miss is usually a
#: restart we caused ourselves.
$FAILS_BEFORE_ALERT = 2

function Write-Log([string]$text) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $text" | Out-File -Append -Encoding utf8 $log
    $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
    if ($lines.Count -gt 3000) { $lines[-1500..-1] | Set-Content -Encoding utf8 $log }
}

function Get-SlackCreds {
    $f = Join-Path $root ".slack.env"
    if (-not (Test-Path $f)) { return $null }
    $vals = @{}
    Get-Content $f | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $vals[$matches[1]] = $matches[2] }
    }
    if ($vals.SLACK_BOT_TOKEN -and $vals.SLACK_CHANNEL_ID) { return $vals }
    return $null
}

function Send-Slack([string]$text) {
    $c = Get-SlackCreds
    if (-not $c) { Write-Log "wanted to say '$text' but there are no Slack credentials"; return }
    try {
        $body = @{ channel = $c.SLACK_CHANNEL_ID; text = $text } | ConvertTo-Json -Compress
        $r = Invoke-RestMethod -Uri "https://slack.com/api/chat.postMessage" -Method Post `
            -Headers @{ Authorization = "Bearer $($c.SLACK_BOT_TOKEN)"; "Content-Type" = "application/json; charset=utf-8" } `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 15
        if (-not $r.ok) { Write-Log "slack refused: $($r.error)" }
    } catch { Write-Log "slack unreachable: $($_.Exception.Message)" }
}

function Test-Page {
    try { return (Invoke-WebRequest $page -UseBasicParsing -TimeoutSec 8).StatusCode -eq 200 }
    catch { return $false }
}

function Repair {
    if (-not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
        if (Test-Path $dockerExe) {
            Write-Log "Docker Desktop is not running - starting it"
            Start-Process $dockerExe
            # The engine takes a while; compose will be tried on later passes.
            Start-Sleep -Seconds 45
        } else {
            Write-Log "Docker Desktop not found at $dockerExe"
            return
        }
    }
    & docker compose up -d 2>&1 | ForEach-Object { Write-Log "compose: $_" }
}

Set-Location $root
Write-Log "uptime guard started (pid $PID)"
$fails = 0
$alerted = $false

while ($true) {
    if (Test-Page) {
        if ($alerted) {
            Send-Slack ":white_check_mark: Commentary QC is back - monitoring resumed."
            Write-Log "recovered, told Slack"
        } elseif ($fails -gt 0) {
            Write-Log "recovered after $fails failed check(s)"
        }
        $fails = 0
        $alerted = $false
    } else {
        $fails++
        Write-Log "page not answering (failure $fails)"
        Repair
        if ($fails -ge $FAILS_BEFORE_ALERT -and -not $alerted) {
            Send-Slack ":rotating_light: Commentary QC is down - nothing is being monitored. Trying to restart it on $env:COMPUTERNAME."
            $alerted = $true
            Write-Log "told Slack we are down"
        }
    }
    Start-Sleep -Seconds $CHECK_SECONDS
}
