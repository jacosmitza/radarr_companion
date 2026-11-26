@echo off
REM Author: Jaco Smit & Google Gemini
REM Date: 2026-11-23

title Radarr Companion — Multi-Drive Movie Organiser
cd /d "%~dp0"

echo [*] Starting Radarr Companion (will open in your default browser automatically)...
python radarr_companion.py

pause