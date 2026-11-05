@echo off
rem Starts the Lumi TV server. Run by the "lumitv" scheduled task (see windows.md).
cd /d "%~dp0.."
if not exist logs mkdir logs
rem Keep one previous log: over 5 MB at start, service.log replaces service.1.log.
if exist logs\service.log for %%F in (logs\service.log) do if %%~zF GTR 5242880 move /y logs\service.log logs\service.1.log >nul
"%USERPROFILE%\.local\bin\uv.exe" run lumitv serve >> logs\service.log 2>&1
