<#
  Run the AWS Error Feed dashboard in the background on this computer (the tower), starting
  automatically when you log in, and share it privately over Tailscale.

  Usage (PowerShell, from the dashboard folder):
      powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1            # install + start
      powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Remove    # uninstall

  What it does:
    1. Creates a Windows scheduled task "AWS Error Feed" that runs the dashboard with pythonw
       (no console window) at log-on, restarting it if it stops. Output goes to dashboard.log.
    2. Starts it now.
    3. If Tailscale is installed: `tailscale serve` publishes it as https://<this-pc>.<tailnet>.ts.net
       - reachable ONLY from your own Tailscale devices (laptop, phone), never the public internet.
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "AWS Error Feed"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Here "aws_error_feed.py"
$Log = Join-Path $Here "dashboard.log"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" |
        Where-Object { $_.CommandLine -match 'aws_error_feed' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    if (Get-Command tailscale -ErrorAction SilentlyContinue) { tailscale serve reset 2>$null }
    Write-Host "Removed the scheduled task, stopped the dashboard and the Tailscale share."
    exit 0
}

# --- find pythonw next to the python that has boto3 installed
$py = (Get-Command python -ErrorAction Stop).Source
if ($py -match "WindowsApps") {
    $py = (& py -c "import sys; print(sys.executable)") 2>$null
}
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to $py" }
& $py -c "import boto3" 2>$null
if ($LASTEXITCODE -ne 0) { throw "boto3 isn't installed for $py - run: `"$py`" -m pip install boto3 mcp tzdata" }

# --- stop any copy that's already running (the port must be free)
Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" |
    Where-Object { $_.CommandLine -match 'aws_error_feed' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

# --- scheduled task: at log-on, hidden, restart on failure, no time limit
$action   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Script`" --no-browser --log `"$Log`"" -WorkingDirectory $Here
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "AWS Error Feed dashboard (http://127.0.0.1:8766)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Dashboard installed as a scheduled task and started. Log: $Log"

# --- private share over Tailscale
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    tailscale serve --bg 8766 | Out-Null
    $dns = ((tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".")
    Write-Host ""
    Write-Host "Shared privately on your tailnet:  https://$dns"
    Write-Host "Open that on your laptop or phone (with the Tailscale app signed in to the same account)."
    Write-Host "For the laptop's Claude connector use:  AWS_ERROR_FEED_URL = https://$dns,http://127.0.0.1:8766"
} else {
    Write-Host "Tailscale not found - install it to reach the dashboard from your laptop / phone."
}
Write-Host ""
Write-Host "Tip: set Windows power options so this PC doesn't sleep, or monitoring pauses while it sleeps."
