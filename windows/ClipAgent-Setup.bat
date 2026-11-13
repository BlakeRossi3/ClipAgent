@echo off
rem ClipAgent installer for Windows: double-click this file.
rem It runs setup.ps1 from the same folder if it is there, otherwise downloads it from GitHub.
rem Nothing here needs administrator rights; Windows may ask once while installing Python/Git/FFmpeg.
setlocal
set "CLIPAGENT_BRANCH=claude/clever-allen-zi43bi"
set "CLIPAGENT_SETUP_URL=https://raw.githubusercontent.com/BlakeRossi3/ClipAgent/refs/heads/%CLIPAGENT_BRANCH%/windows/setup.ps1"
title ClipAgent setup
echo.
echo  ClipAgent setup - this window shows the progress. It takes 5-15 minutes the first time.
echo.
if exist "%~dp0setup.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Branch "%CLIPAGENT_BRANCH%"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $p=Join-Path $env:TEMP 'clipagent-setup.ps1'; Invoke-WebRequest -UseBasicParsing -Uri $env:CLIPAGENT_SETUP_URL -OutFile $p; & $p -Branch $env:CLIPAGENT_BRANCH; exit $LASTEXITCODE"
)
if errorlevel 1 (
  echo.
  echo  Setup did not finish. Read the red message above, fix it, then double-click this file again.
  echo  Help: https://github.com/BlakeRossi3/ClipAgent/blob/%CLIPAGENT_BRANCH%/docs/WINDOWS.md
  pause
  exit /b 1
)
echo.
echo  Done. ClipAgent is open in your browser. You can close this window.
timeout /t 15
endlocal
