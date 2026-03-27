@echo off
REM One-command entry for Windows cmd.exe — same as .\ordo-ai-stack.ps1
set "SCRIPT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%ordo-ai-stack.ps1" %*
