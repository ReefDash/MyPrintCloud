@echo off
rem MyPrintCloud bridge: keeps your printer connected to your server. Close this window to stop it.
title MyPrintCloud bridge
cd /d "%~dp0bridge"
:loop
python bridge.py
echo Bridge stopped; restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
