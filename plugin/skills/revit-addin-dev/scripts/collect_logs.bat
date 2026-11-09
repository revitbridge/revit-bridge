@echo off
REM Double-click to collect the add-in's logs into a zip on the Desktop.
REM Developer: replace REPLACE-ME below with the add-in name before sending
REM this file together with collect_logs.ps1 (same folder) to the user.
REM Nothing on this machine is changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_logs.ps1" -Name "REPLACE-ME" %*
pause
