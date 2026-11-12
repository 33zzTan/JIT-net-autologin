# ============================================================
#  JIT campus auto-login - register / remove / inspect the logon task
#
#  NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1
#  reads .ps1 files with the ANSI code page, so non-ASCII text here would
#  be garbled. Read README.md (UTF-8) for the Chinese documentation.
#
#  Usage (normal user, no admin required):
#     powershell -ExecutionPolicy Bypass -File install_task.ps1
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -DelaySeconds 60
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -NoWatchdog
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -Status
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -Diagnose
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -RunNow
#     powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
#
#  Registers TWO tasks by default:
#     JIT-Campus-AutoLogin  - at logon + N seconds, log in once, then exit
#     JIT-Campus-KeepAlive  - at logon + N+60 seconds, run forever in the
#                             background: keep-alive heartbeat + auto reconnect
# ============================================================

[CmdletBinding()]
param(
    [string]$TaskName = 'JIT-Campus-AutoLogin',
    [string]$KeepAliveTaskName = 'JIT-Campus-KeepAlive',
    [int]$DelaySeconds = 45,
    [switch]$Remove,
    [switch]$Status,
    [switch]$Diagnose,
    [switch]$RunNow,
    [switch]$UseBatch,
    [switch]$NoWatchdog
)

$ErrorActionPreference = 'Stop'
$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Batch    = Join-Path $AppDir 'run_silent.bat'
$Config   = Join-Path $AppDir 'config.json'
$LogFile  = Join-Path $AppDir 'logs\autologin.log'
$KeepLog  = Join-Path $AppDir 'logs\keepalive.log'
$KeepState= Join-Path $AppDir 'logs\keepalive.state.json'
$Profile  = Join-Path $AppDir 'browser-profile'

function Write-Head($text) {
    Write-Host ''
    Write-Host ('=' * 58) -ForegroundColor DarkGray
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 58) -ForegroundColor DarkGray
}

function Get-Existing {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Get-ExistingKeepAlive {
    Get-ScheduledTask -TaskName $KeepAliveTaskName -ErrorAction SilentlyContinue
}

function Show-KeepAliveState {
    if (-not (Test-Path -LiteralPath $KeepState)) {
        Write-Host '  keepalive : no state file yet (the watchdog has never run)' -ForegroundColor Yellow
        return
    }
    try {
        $s = Get-Content -LiteralPath $KeepState -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host ("  keepalive : {0}  (pid {1}, up {2:N0} min)" -f $s.status_text, $s.pid, ([double]$s.uptime_seconds / 60))
        Write-Host ("              last check {0} | last online {1} | last relogin {2}" -f $s.last_check, $s.last_online, $s.last_relogin)
        Write-Host ("              relogin attempts {0} / ok {1} (last hour {2}) | checks {3}" -f `
            $s.relogins_total, $s.relogin_success_total, $s.relogins_last_hour, $s.checks_total)
        if ($s.probe)    { Write-Host ("              probe  : {0}" -f $s.probe) }
        if ($s.portal)   { Write-Host ("              portal : {0}" -f $s.portal) }
        if ($s.last_error) { Write-Host ("              last error: {0}" -f $s.last_error) -ForegroundColor Yellow }
        if ($s.paused)   { Write-Host ("              paused : {0} (until {1})" -f $s.paused_reason, $s.paused_until) -ForegroundColor Yellow }
        Write-Host ("              updated {0}" -f $s.updated_at)
    } catch {
        Write-Host '  keepalive : state file exists but could not be parsed' -ForegroundColor Yellow
    }
}

function Show-LogFile($path, $lines) {
    if (Test-Path -LiteralPath $path) {
        Write-Host ''
        Write-Host "  --- last $lines lines of $(Split-Path -Leaf $path) ---" -ForegroundColor DarkGray
        Get-Content -LiteralPath $path -Tail $lines -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    }
}

function Find-Python {
    param([switch]$Windowless)
    $names = @()
    if ($Windowless) { $names += 'pythonw.exe' } else { $names += 'python.exe' }
    $names += 'python.exe'
    foreach ($name in $names) {
        foreach ($path in @((Join-Path $AppDir $name), (Join-Path $AppDir ".venv\Scripts\$name"))) {
            if (Test-Path -LiteralPath $path) { return $path }
        }
    }
    foreach ($name in $names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Show-ResultCode($code) {
    $value = 0
    try { $value = [int]$code } catch { $value = -1 }
    switch ($value) {
        0      { return 'OK (0)' }
        1      { return 'FAILED (1) - login was not completed, see logs\autologin.log' }
        267009 { return '0x41301 - still running (or stuck). Run: Stop-ScheduledTask' }
        267011 { return '0x41303 - has not run yet' }
        267014 { return '0x41302 - terminated (task was stopped / time limit hit)' }
        default { return ('code {0} (0x{1:X})' -f $value, $value) }
    }
}

function Show-LogTail($lines) {
    if (Test-Path -LiteralPath $LogFile) {
        Write-Host ''
        Write-Host "  --- last $lines lines of logs\autologin.log ---" -ForegroundColor DarkGray
        Get-Content -LiteralPath $LogFile -Tail $lines -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host '  (no logs\autologin.log yet - the program has never run)' -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- status
if ($Status) {
    Write-Head "Logon task: $TaskName"
    $task = Get-Existing
    if (-not $task) {
        Write-Host '  NOT registered - run this script without arguments to register.' -ForegroundColor Yellow
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("  State        : {0}" -f $task.State)
        Write-Host ("  LastRunTime  : {0}" -f $info.LastRunTime)
        Write-Host ("  LastResult   : {0}" -f (Show-ResultCode $info.LastTaskResult))
        Write-Host ("  NextRunTime  : {0}" -f $info.NextRunTime)
        Write-Host '  Action       :'
        $task.Actions | ForEach-Object {
            Write-Host ("    {0} {1}" -f $_.Execute, $_.Arguments)
            Write-Host ("    (working dir: {0})" -f $_.WorkingDirectory)
        }
    }

    Write-Head "Keep-alive task: $KeepAliveTaskName"
    $keepTask = Get-ExistingKeepAlive
    if (-not $keepTask) {
        Write-Host '  NOT registered (run this script, or -NoWatchdog was used)' -ForegroundColor Yellow
    } else {
        $keepInfo = Get-ScheduledTaskInfo -TaskName $KeepAliveTaskName
        Write-Host ("  State        : {0}" -f $keepTask.State)
        Write-Host ("  LastRunTime  : {0}" -f $keepInfo.LastRunTime)
        if ($keepTask.State -eq 'Running') {
            Write-Host '  LastResult   : (running - normal for a resident task)'
        } else {
            Write-Host ("  LastResult   : {0}" -f (Show-ResultCode $keepInfo.LastTaskResult))
        }
        Write-Host '  Action       :'
        $keepTask.Actions | ForEach-Object {
            Write-Host ("    {0} {1}" -f $_.Execute, $_.Arguments)
        }
    }
    Write-Host ''
    Show-KeepAliveState
    Show-LogFile $LogFile 12
    Show-LogFile $KeepLog 12
    exit 0
}

# ---------------------------------------------------------------- diagnose
if ($Diagnose) {
    Write-Head 'Environment check'
    $py  = Find-Python
    $pyw = Find-Python -Windowless
    Write-Host ("  AppDir        : {0}" -f $AppDir)
    Write-Host ("  python.exe    : {0}" -f ($(if ($py)  { $py }  else { 'NOT FOUND' })))
    Write-Host ("  pythonw.exe   : {0}" -f ($(if ($pyw) { $pyw } else { 'not found (will use python.exe)' })))
    Write-Host ("  run_silent.bat: {0}" -f (Test-Path -LiteralPath $Batch))
    Write-Host ("  config.json   : {0}" -f (Test-Path -LiteralPath $Config))
    Write-Host ("  browser-profile exists: {0}" -f (Test-Path -LiteralPath $Profile))
    foreach ($lock in @('SingletonLock', 'lockfile')) {
        $p = Join-Path $Profile $lock
        if (Test-Path -LiteralPath $p) { Write-Host ("  WARNING: {0} exists - close the auto-login browser window before testing" -f $lock) -ForegroundColor Yellow }
    }
    foreach ($exe in @(
        'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files\Microsoft\Edge\Application\msedge.exe')) {
        if (Test-Path -LiteralPath $exe) { Write-Host ("  Edge          : {0}" -f $exe) -ForegroundColor Green }
    }
    if ($py) {
        $probe = & $py -c "import sys, os; sys.path.insert(0, os.path.join(r'$AppDir', 'vendor')); import selenium; print('selenium ' + selenium.__version__)" 2>&1
        Write-Host ("  vendor selenium: {0}" -f ($probe -join ' '))
        $compile = & $py -c "import py_compile, sys, os; root = r'$AppDir'; [py_compile.compile(os.path.join(root, 'jitauth', f), doraise=True) for f in os.listdir(os.path.join(root, 'jitauth')) if f.endswith('.py')]; print('jitauth/*.py syntax OK')" 2>&1
        Write-Host ("  syntax check  : {0}" -f ($compile -join ' '))
    }
    $task = Get-Existing
    Write-Host ("  task          : {0}" -f ($(if ($task) { $task.State } else { 'NOT registered' })))
    $keepTask = Get-ExistingKeepAlive
    Write-Host ("  keepalive task: {0}" -f ($(if ($keepTask) { $keepTask.State } else { 'NOT registered' })))
    Show-KeepAliveState
    if ($py) {
        $ka = & $py -c "import sys, os; sys.path.insert(0, r'$AppDir'); from jitauth import keepalive as k, config as c; s = k.Settings.from_cfg(c.load()); print('interval=%g threshold=%d relogin=%s probes=%d' % (s.check_interval, s.fail_threshold, s.relogin_enabled, len(k.build_endpoints(c.load()))))" 2>&1
        Write-Host ("  keepalive cfg : {0}" -f ($ka -join ' '))
    }
    Show-LogTail 10
    exit 0
}

# ---------------------------------------------------------------- remove
if ($Remove) {
    Write-Head 'Removing tasks'
    foreach ($name in @($TaskName, $KeepAliveTaskName)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $existing) {
            Write-Host ("  not registered: {0}" -f $name) -ForegroundColor Yellow
            continue
        }
        try { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue } catch { }
        Start-Sleep -Milliseconds 400
        try {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host ("  removed: {0}" -f $name) -ForegroundColor Green
        } catch {
            Write-Host ("  could not remove {0}: {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
            Write-Host ("  try: Stop-ScheduledTask -TaskName {0}" -f $name) -ForegroundColor Yellow
        }
    }
    exit 0
}

# ---------------------------------------------------------------- install
Write-Head 'Register logon auto-start task'

$py  = Find-Python
$pyw = Find-Python -Windowless
if (-not $py -and -not $pyw) {
    Write-Host '  Python not found. Install Python, or put python.exe / pythonw.exe in this folder.' -ForegroundColor Red
    exit 1
}

if ($UseBatch) {
    if (-not (Test-Path -LiteralPath $Batch)) {
        Write-Host "  cannot find $Batch" -ForegroundColor Red
        exit 1
    }
    $action = New-ScheduledTaskAction -Execute $Batch -WorkingDirectory $AppDir
    Write-Host "  runner : $Batch"
} else {
    $exe = if ($pyw) { $pyw } else { $py }
    $action = New-ScheduledTaskAction -Execute $exe -Argument '-m jitauth --silent' -WorkingDirectory $AppDir
    Write-Host "  runner : $exe -m jitauth --silent"
}

# dependency / config sanity checks (informational)
if ($py) {
    $probe = & $py -c "import sys, os; sys.path.insert(0, os.path.join(r'$AppDir', 'vendor')); import selenium; print('ok')" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($probe -join ' ') -notmatch 'ok') {
        Write-Host '  WARNING: vendor selenium is not importable - the browser path may be unavailable.' -ForegroundColor Yellow
        Write-Host "           try: $py tools\fetch_vendor.py" -ForegroundColor Yellow
    } else {
        Write-Host '  vendor selenium: ready' -ForegroundColor Green
    }
}
if (Test-Path -LiteralPath $Config) {
    try {
        $cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host ("  wifi   : {0}" -f $cfg.wifi.ssid)
        Write-Host ("  portal : {0}" -f $cfg.portal.url)
        Write-Host ("  account: {0} / {1}" -f $cfg.account.username, $cfg.account.isp)
        Write-Host ("  browser: engine={0} profile={1} close_after_login={2}" -f `
            $cfg.browser.engine, $cfg.browser.profile, $cfg.browser.close_after_login)
    } catch {
        Write-Host '  WARNING: config.json could not be parsed - check its JSON syntax.' -ForegroundColor Yellow
    }
}

# replace an old task (also un-stick it: a hung run would block a new one)
if (Get-Existing) {
    Write-Host '  removing the old task ...'
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Start-Sleep -Milliseconds 500
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    } catch {
        Write-Host ("  could not remove the old task: {0}" -f $_.Exception.Message) -ForegroundColor Red
        Write-Host ("  if it says it is still running, run: Stop-ScheduledTask -TaskName {0}" -f $TaskName) -ForegroundColor Yellow
        exit 1
    }
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelaySeconds}S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 2)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'JIT-Stu campus auto-login: open our own browser window, fill the portal form and log in.' | Out-Null

# ------------------------------------------------ keep-alive (watchdog) task
#  This is the part that keeps the network alive AFTER the first login:
#  every 60s it probes the internet, and if the portal says "not logged in"
#  it re-submits the form over plain HTTP (no browser window, no popup).
if (-not $NoWatchdog) {
    Write-Head "Register keep-alive task: $KeepAliveTaskName"
    $watchExe = if ($pyw) { $pyw } else { $py }
    $keepAction = New-ScheduledTaskAction -Execute $watchExe `
        -Argument '-m jitauth --watch' -WorkingDirectory $AppDir

    $keepTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $keepTrigger.Delay = "PT$($DelaySeconds + 60)S"

    #  No execution time limit (it is meant to run forever) + restart if it dies.
    $keepSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    $keepPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    if (Get-ExistingKeepAlive) {
        Write-Host '  removing the old keep-alive task ...'
        try { Stop-ScheduledTask -TaskName $KeepAliveTaskName -ErrorAction SilentlyContinue } catch { }
        Start-Sleep -Milliseconds 500
        try {
            Unregister-ScheduledTask -TaskName $KeepAliveTaskName -Confirm:$false
        } catch {
            Write-Host ("  could not remove the old keep-alive task: {0}" -f $_.Exception.Message) -ForegroundColor Red
            Write-Host ("  run: Stop-ScheduledTask -TaskName {0}" -f $KeepAliveTaskName) -ForegroundColor Yellow
            exit 1
        }
    }

    Register-ScheduledTask -TaskName $KeepAliveTaskName `
        -Action $keepAction -Trigger $keepTrigger -Settings $keepSettings -Principal $keepPrincipal `
        -Description 'JIT-Stu keep-alive watchdog: probe the network, silently re-login over HTTP when the session drops.' | Out-Null

    $keepTask = Get-ScheduledTask -TaskName $KeepAliveTaskName
    Write-Host ("  registered: {0} ({1})" -f $keepTask.TaskName, $keepTask.State) -ForegroundColor Green
    Write-Host ("  trigger   : at logon + {0} seconds" -f ($DelaySeconds + 60))
    Write-Host ("  action    : {0} -m jitauth --watch" -f $watchExe)
    Write-Host '  limits    : no time limit; restarts itself every minute if it exits' -ForegroundColor DarkGray
    Write-Host '  note      : a tray icon appears after logon (right-click for status / pause / quit)'
}

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host ''
Write-Host ("  registered: {0} ({1})" -f $task.TaskName, $task.State) -ForegroundColor Green
Write-Host ("  trigger   : at logon + {0} seconds" -f $DelaySeconds)
$task.Actions | ForEach-Object {
    Write-Host ("  action    : {0} {1}" -f $_.Execute, $_.Arguments)
}

if ($RunNow) {
    Write-Host ''
    Write-Host '  starting it once now ...'
    Start-ScheduledTask -TaskName $TaskName
    if (-not $NoWatchdog) { Start-ScheduledTask -TaskName $KeepAliveTaskName }
    Start-Sleep -Seconds 10
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("  LastRunTime : {0}" -f $info.LastRunTime)
    Write-Host ("  LastResult  : {0}" -f (Show-ResultCode $info.LastTaskResult))
    Show-LogFile $LogFile 12
    if (-not $NoWatchdog) {
        Write-Host ''
        Show-KeepAliveState
        Show-LogFile $KeepLog 12
    }
}

Write-Host ''
Write-Host '  Common commands:' -ForegroundColor Cyan
Write-Host "    status : powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Status"
Write-Host "    run now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "    stop   : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "    remove : powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Remove"
