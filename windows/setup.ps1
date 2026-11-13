#Requires -Version 5.1
<#
  ClipAgent installer for Windows 10/11 (run by ClipAgent-Setup.bat; safe to run again).

  1. Installs what is missing with winget: Python 3.13, Git, FFmpeg (Gyan full build).
  2. Clones (or updates) the app into %LOCALAPPDATA%\ClipAgent\app on the chosen branch.
  3. Creates the Python environment and installs the requirements.
  4. Keeps your data in %LOCALAPPDATA%\ClipAgent\data (settings, database, memory vault, videos),
     outside the app folder, so updates never touch it.
  5. Creates "ClipAgent" shortcuts (Start menu + Desktop), starts ClipAgent and opens the
     setup page in your browser.

  Keep this file plain ASCII: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.
#>
[CmdletBinding()]
param(
    [string]$Branch = 'claude/clever-allen-zi43bi',
    [string]$RepoUrl = 'https://github.com/BlakeRossi3/ClipAgent.git',
    [string]$InstallRoot = (Join-Path -Path $env:LOCALAPPDATA -ChildPath 'ClipAgent'),
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$App = Join-Path -Path $InstallRoot -ChildPath 'app'
$Data = Join-Path -Path $InstallRoot -ChildPath 'data'
$Logs = Join-Path -Path $Data -ChildPath 'logs'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Done([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }

function Update-SessionPath {
    # winget installs change PATH in the registry; pick that up without opening a new window.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Test-CommandExists([string]$Name) {
    return [bool](Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    # Run a program, showing its output; return its exit code. Native programs write progress to
    # stderr, which Windows PowerShell 5.1 would turn into terminating errors under 'Stop'.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())
    $ErrorActionPreference = 'Continue'
    & $FilePath @ArgumentList | Out-Host
    return $LASTEXITCODE
}

function Get-NativeOutput {
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())
    $ErrorActionPreference = 'Continue'
    $out = & $FilePath @ArgumentList 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return (($out | Out-String).Trim())
}

function Install-WithWinget([string]$Id, [string]$Label, [string[]]$Extra = @()) {
    if (-not (Test-CommandExists 'winget')) {
        throw ("$Label is needed but 'winget' (App Installer) is missing. Open the Microsoft Store, " +
            "install or update 'App Installer', then double-click ClipAgent-Setup.bat again.")
    }
    Write-Step "Installing $Label (a few minutes; Windows may ask for permission)"
    $wingetArgs = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements',
        '--accept-source-agreements') + $Extra
    $code = Invoke-Native -FilePath 'winget' -ArgumentList $wingetArgs
    # -1978335189 (0x8A15002B): already installed / no newer version
    if ($code -ne 0 -and $code -ne -1978335189) {
        throw "winget could not install $Label (exit code $code)."
    }
    Update-SessionPath
    Write-Done "$Label installed."
}

function Find-Python {
    $candidates = @(
        (Join-Path -Path $env:LOCALAPPDATA -ChildPath 'Programs\Python\Python313\python.exe'),
        (Join-Path -Path $env:LOCALAPPDATA -ChildPath 'Programs\Python\Python312\python.exe'),
        (Join-Path -Path $env:ProgramFiles -ChildPath 'Python313\python.exe'),
        (Join-Path -Path $env:ProgramFiles -ChildPath 'Python312\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    if (Test-CommandExists 'py') {
        foreach ($v in @('-3.13', '-3.12')) {
            $exe = Get-NativeOutput -FilePath 'py' -ArgumentList @($v, '-c', 'import sys; print(sys.executable)')
            if ($exe) { return $exe }
        }
    }
    return $null  # never the Microsoft Store "python" alias, which only opens the Store
}

function Find-Git {
    if (Test-CommandExists 'git') { return (Get-Command -Name 'git' -CommandType Application | Select-Object -First 1).Source }
    $c = Join-Path -Path $env:ProgramFiles -ChildPath 'Git\cmd\git.exe'
    if (Test-Path -LiteralPath $c) { return $c }
    return $null
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $WorkDir
    $lnk.Description = $Description
    $lnk.WindowStyle = 7  # minimized
    $lnk.Save()
}

try {
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    $transcript = Join-Path -Path $Logs -ChildPath ('setup-{0:yyyyMMdd-HHmmss}.log' -f (Get-Date))
    Start-Transcript -LiteralPath $transcript | Out-Null

    Write-Host ''
    Write-Host 'ClipAgent setup' -ForegroundColor White
    Write-Host "App folder:  $App"
    Write-Host "Your data:   $Data"

    # ---------------------------------------------------------------- prerequisites
    Write-Step 'Checking Python, Git and FFmpeg'
    Update-SessionPath
    $python = Find-Python
    if (-not $python) {
        Install-WithWinget -Id 'Python.Python.3.13' -Label 'Python 3.13' -Extra @('--scope', 'user')
        $python = Find-Python
        if (-not $python) { throw 'Python was installed but cannot be found. Restart the computer and run setup again.' }
    }
    Write-Done "Python: $python"

    $git = Find-Git
    if (-not $git) {
        Install-WithWinget -Id 'Git.Git' -Label 'Git'
        $git = Find-Git
        if (-not $git) { throw 'Git was installed but cannot be found. Restart the computer and run setup again.' }
    }
    Write-Done "Git: $git"

    if (-not (Test-CommandExists 'ffmpeg')) {
        # Gyan.FFmpeg = the "full" build (libass captions, libx264, fontconfig/freetype).
        Install-WithWinget -Id 'Gyan.FFmpeg' -Label 'FFmpeg'
        if (-not (Test-CommandExists 'ffmpeg')) {
            Write-Host '    FFmpeg will be found after the next restart of ClipAgent.' -ForegroundColor Yellow
        }
    }
    else {
        Write-Done 'FFmpeg: already installed.'
    }

    # ---------------------------------------------------------------- the app
    if (Test-Path -LiteralPath (Join-Path -Path $App -ChildPath '.git')) {
        Write-Step "Updating ClipAgent ($Branch)"
        $null = Invoke-Native -FilePath $git -ArgumentList @('-C', $App, 'fetch', 'origin', $Branch)
        $null = Invoke-Native -FilePath $git -ArgumentList @('-C', $App, 'checkout', $Branch)
        $code = Invoke-Native -FilePath $git -ArgumentList @('-C', $App, 'pull', '--ff-only', 'origin', $Branch)
        if ($code -ne 0) { Write-Host '    Could not update (local changes?). Continuing with the current version.' -ForegroundColor Yellow }
    }
    else {
        Write-Step "Downloading ClipAgent ($Branch)"
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
        $code = Invoke-Native -FilePath $git -ArgumentList @('clone', '--branch', $Branch, $RepoUrl, $App)
        if ($code -ne 0) { throw "git clone failed (exit code $code). Check the internet connection and try again." }
    }

    Write-Step 'Installing the Python packages (first time: 5-10 minutes)'
    $venvPython = Join-Path -Path $App -ChildPath '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $code = Invoke-Native -FilePath $python -ArgumentList @('-m', 'venv', (Join-Path -Path $App -ChildPath '.venv'))
        if ($code -ne 0) { throw "Could not create the Python environment (exit code $code)." }
    }
    $null = Invoke-Native -FilePath $venvPython -ArgumentList @('-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', 'pip')
    $code = Invoke-Native -FilePath $venvPython -ArgumentList @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path -Path $App -ChildPath 'requirements.txt'))
    if ($code -ne 0) { throw "Installing the Python packages failed (exit code $code). See the log: $transcript" }
    Write-Done 'Packages installed.'

    # ---------------------------------------------------------------- data folder
    Write-Step 'Preparing your data folder'
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    $strategy = Join-Path -Path $Data -ChildPath 'strategy.json'
    if (-not (Test-Path -LiteralPath $strategy)) {
        Copy-Item -LiteralPath (Join-Path -Path $App -ChildPath 'strategy.json') -Destination $strategy
    }
    $env:CLIPAGENT_DATA_DIR = $Data
    $env:CLIPAGENT_STRATEGY_PATH = $strategy
    $env:PYTHONUTF8 = '1'
    Push-Location -LiteralPath $App
    try {
        $code = Invoke-Native -FilePath $venvPython -ArgumentList @('main.py', 'init')
    }
    finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "ClipAgent could not create its database (exit code $code)." }
    Write-Done "Data folder ready. Your memory vault is $Data\vault (you can open it in Obsidian)."

    # ---------------------------------------------------------------- shortcuts
    Write-Step 'Creating the ClipAgent shortcuts'
    $ps = Join-Path -Path $env:SystemRoot -ChildPath 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $launcher = Join-Path -Path $App -ChildPath 'windows\ClipAgent.ps1'
    $launchArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $programs = [Environment]::GetFolderPath('Programs')
    $desktop = [Environment]::GetFolderPath('Desktop')
    New-Shortcut -Path (Join-Path -Path $programs -ChildPath 'ClipAgent.lnk') -Target $ps -Arguments $launchArgs -WorkDir $App -Description 'Start ClipAgent and open it in the browser'
    New-Shortcut -Path (Join-Path -Path $desktop -ChildPath 'ClipAgent.lnk') -Target $ps -Arguments $launchArgs -WorkDir $App -Description 'Start ClipAgent and open it in the browser'
    # Used by Settings > "Start ClipAgent when Windows starts" (copied into the Startup folder).
    New-Shortcut -Path (Join-Path -Path $InstallRoot -ChildPath 'ClipAgent (background).lnk') -Target $ps -Arguments "$launchArgs -NoBrowser" -WorkDir $App -Description 'Start ClipAgent quietly'
    Write-Done 'Start menu and Desktop shortcuts created.'

    # ---------------------------------------------------------------- start
    Write-Step 'Starting ClipAgent - your browser opens the setup page in a moment'
    $null = Invoke-Native -FilePath $ps -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launcher, '-NoUpdate', '-Port', "$Port")
    Write-Host ''
    Write-Host 'All done. From now on, open ClipAgent with the shortcut on your Desktop.' -ForegroundColor Green
    Write-Host 'You can close this window.'
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host ''
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "A log is in $Logs" -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
