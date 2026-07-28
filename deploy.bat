@echo off
cd /d "%~dp0"

rem Default is automation-safe: no PowerShell/SSH/PAT prompts and no trailing pause.
rem Human operator who wants hidden prompts: deploy.bat --interactive
if /i "%~1"=="--interactive" goto interactive

powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0deploy\deploy-all.ps1"
exit /b %ERRORLEVEL%

:interactive
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\deploy-all.ps1" -Interactive
set "deploy_exit=%ERRORLEVEL%"
pause
exit /b %deploy_exit%
