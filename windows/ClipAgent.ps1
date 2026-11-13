#Requires -Version 5.1
<#
  ClipAgent launcher (what the "ClipAgent" shortcut runs).

  - Already running?  Just open the browser.
  - Otherwise: quietly update the app (git pull --ff-only; on the assistant's own branch
    sterling/self, merge the published branch in and abort on conflicts), pip install when
    requirements.txt changed, start "main.py schedule" (scheduler + web page on 127.0.0.1) in a minimized window,
    wait until it answers, then open the browser.
  Everything is logged to %LOCALAPPDATA%\ClipAgent\data\logs\. Closing the minimized
  "ClipAgent" window (or Settings > Stop) stops ClipAgent.

  Keep this file plain ASCII (Windows PowerShell 5.1 reads BOM-less scripts as ANSI).
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoUpdate,
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$App = Split-Path -Parent $PSScriptRoot
$Root = Split-Path -Parent $App
$Data = Join-Path -Path $Root -ChildPath 'data'
$Logs = Join-Path -Path $Data -ChildPath 'logs'
$VenvPython = Join-Path -Path $App -ChildPath '.venv\Scripts\python.exe'
$Url = "http://127.0.0.1:$Port"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$LauncherLog = Join-Path -Path $Logs -ChildPath ('launcher-{0:yyyy-MM-dd}.log' -f (Get-Date))
$AppLog = Join-Path -Path $Logs -ChildPath ('clipagent-{0:yyyy-MM-dd}.log' -f (Get-Date))

function Write-Log([string]$Text) {
    Add-Content -LiteralPath $LauncherLog -Value ('{0:HH:mm:ss} {1}' -f (Get-Date), $Text) -Encoding UTF8
}

function Show-Problem([string]$Text) {
    Write-Log "PROBLEM: $Text"
    try {
        Add-Type -AssemblyName PresentationFramework
        $null = [System.Windows.MessageBox]::Show($Text, 'ClipAgent', 'OK', 'Warning')
    }
    catch {
        Write-Host $Text -ForegroundColor Red
    }
}

function Test-Running {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "$Url/healthz" -TimeoutSec 3
        return ($r.StatusCode -eq 200 -and $r.Content -match '"status"')
    }
    catch {
        return $false
    }
}

function Invoke-Logged {
    # Run a program with its output appended to the launcher log; return the exit code.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())
    $ErrorActionPreference = 'Continue'
    & $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" } | Out-File -LiteralPath $LauncherLog -Append -Encoding utf8
    return $LASTEXITCODE
}

function Get-FileHashOrEmpty([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    return ''
}

function Update-App {
    $git = Get-Command -Name 'git' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $git) { Write-Log 'git not found; skipping the update check'; return }
    $gitExe = $git.Source
    if ((Invoke-Logged -FilePath $gitExe -ArgumentList @('-C', $App, 'fetch', '--quiet', 'origin')) -ne 0) {
        Write-Log 'update check failed (offline?); starting the current version'
        return
    }
    $ErrorActionPreference = 'Continue'
    $local = (& $gitExe -C $App rev-parse HEAD 2>$null | Out-String).Trim()
    $branch = (& $gitExe -C $App rev-parse --abbrev-ref HEAD 2>$null | Out-String).Trim()
    $upstream = (& $gitExe -C $App config --get clipagent.upstream 2>$null | Out-String).Trim()
    if ($upstream -and $branch -ne $upstream -and $upstream -match '^[A-Za-z0-9._/-]+$') {
        $remote = (& $gitExe -C $App rev-parse "origin/$upstream" 2>$null | Out-String).Trim()
        if (-not $remote) { Write-Log 'up to date'; return }
        & $gitExe -C $App merge-base --is-ancestor $remote HEAD 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Log 'up to date'; return }
    }
    else {
        $upstream = ''
        $remote = (& $gitExe -C $App rev-parse '@{u}' 2>$null | Out-String).Trim()
    }
    if (-not $remote -or $local -eq $remote) { Write-Log 'up to date'; return }
    $reqFile = Join-Path -Path $App -ChildPath 'requirements.txt'
    $before = Get-FileHashOrEmpty $reqFile
    if ($upstream) {
        # The assistant's own branch (sterling/self): merge the published branch in, keep its commits.
        $code = Invoke-Logged -FilePath $gitExe -ArgumentList @('-C', $App, '-c', 'user.name=Sterling', '-c', 'user.email=sterling@clipagent.local', 'pull', '--no-rebase', '--no-edit', 'origin', $upstream)
        if ($code -ne 0) {
            $null = Invoke-Logged -FilePath $gitExe -ArgumentList @('-C', $App, 'merge', '--abort')
            Write-Log "merging origin/$upstream conflicted with the assistant's changes; kept the local version"
            return
        }
    }
    elseif ((Invoke-Logged -FilePath $gitExe -ArgumentList @('-C', $App, 'pull', '--ff-only')) -ne 0) {
        Write-Log 'git pull --ff-only failed; starting the current version'
        return
    }
    Write-Log "updated $local -> $remote"
    if ((Get-FileHashOrEmpty $reqFile) -ne $before) {
        $code = Invoke-Logged -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $reqFile)
        Write-Log "pip install exit code $code"
    }
}

try {
    Write-Log "launcher started (port $Port)"
    if (Test-Running) {
        Write-Log 'already running'
        if (-not $NoBrowser) { Start-Process -FilePath "$Url/" }
        exit 0
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Show-Problem 'ClipAgent is not installed completely. Double-click ClipAgent-Setup.bat again.'
        exit 1
    }

    # PATH as it is now in the registry (winget may have added FFmpeg since this login).
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ';'

    if (-not $NoUpdate) { Update-App }

    $strategy = Join-Path -Path $Data -ChildPath 'strategy.json'
    if (-not (Test-Path -LiteralPath $strategy)) {
        Copy-Item -LiteralPath (Join-Path -Path $App -ChildPath 'strategy.json') -Destination $strategy
    }
    # Settings for the app process (inherited by the window started below).
    $env:CLIPAGENT_DATA_DIR = $Data
    $env:CLIPAGENT_STRATEGY_PATH = $strategy
    $env:DASHBOARD_TRUST_LOOPBACK = 'true'   # no password needed on this laptop itself
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    $ffmpeg = Get-Command -Name 'ffmpeg' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ffmpeg) { $env:FFMPEG_BIN = $ffmpeg.Source }

    # cmd.exe gives the minimized window a title and appends all output to the day's log.
    $cmdLine = "/d /s /c `"title ClipAgent - close this window to stop ClipAgent & `"$VenvPython`" main.py schedule --host 127.0.0.1 --port $Port >> `"$AppLog`" 2>&1`""
    Write-Log "starting: $VenvPython main.py schedule --port $Port"
    $proc = Start-Process -FilePath (Join-Path -Path $env:SystemRoot -ChildPath 'System32\cmd.exe') -ArgumentList $cmdLine -WorkingDirectory $App -WindowStyle Minimized -PassThru

    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-Running) {
            Write-Log 'ClipAgent is up'
            if (-not $NoBrowser) { Start-Process -FilePath "$Url/" }
            exit 0
        }
        if ($proc.HasExited) { break }
    }
    # A change the assistant made to its own code may stop the app from starting: put the version
    # before that change back (recorded in data\assistant\selfedit_pending.txt) and try once more.
    $marker = Join-Path -Path $Data -ChildPath 'assistant\selfedit_pending.txt'
    $git = Get-Command -Name 'git' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ((Test-Path -LiteralPath $marker) -and $git) {
        $prev = (Get-Content -LiteralPath $marker -TotalCount 1).Trim()
        Remove-Item -LiteralPath $marker -Force
        if ($prev -match '^[0-9a-f]{40}$') {
            Write-Log "start failed after a self-edit; resetting to $prev"
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
            $null = Invoke-Logged -FilePath $git.Source -ArgumentList @('-C', $App, 'reset', '--hard', $prev)
            $proc = Start-Process -FilePath (Join-Path -Path $env:SystemRoot -ChildPath 'System32\cmd.exe') -ArgumentList $cmdLine -WorkingDirectory $App -WindowStyle Minimized -PassThru
            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 2
                if (Test-Running) {
                    Write-Log 'ClipAgent is up (previous version restored)'
                    if (-not $NoBrowser) { Start-Process -FilePath "$Url/" }
                    exit 0
                }
                if ($proc.HasExited) { break }
            }
        }
    }
    Show-Problem ("ClipAgent did not start. Another program may be using port $Port, or something " +
        "went wrong while starting. The details are in:`n$AppLog")
    exit 1
}
catch {
    Show-Problem "ClipAgent could not start: $($_.Exception.Message)"
    exit 1
}
