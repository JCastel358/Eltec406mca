@echo off
cd /d "%~dp0"
python analyze_emitter_results.py
if errorlevel 1 pause
