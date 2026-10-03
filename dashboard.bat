@echo off
rem LeoAgent dashboard: double-click opens the SSH tunnel to the server and the app window.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard.ps1"
