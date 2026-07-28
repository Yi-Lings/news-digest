@echo off
cd /d "%~dp0"

rem Pass every PowerShell-style deployment parameter through unchanged.
rem Default is automation-safe; use deploy.bat -Interactive ... to allow prompts.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\deploy-all.ps1" %*
set "deploy_exit=%ERRORLEVEL%"
if /i "%~1"=="-Interactive" pause
exit /b %deploy_exit%
