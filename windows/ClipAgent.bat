@echo off
rem Starts ClipAgent (same as the ClipAgent shortcut). Pass -NoBrowser / -NoUpdate / -Port N if needed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0ClipAgent.ps1" %*
