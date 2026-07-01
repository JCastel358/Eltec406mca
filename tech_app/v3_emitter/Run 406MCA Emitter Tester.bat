@echo off
cd /d "%~dp0"
REM Prefer pythonw so the technician sees only the app window (no console).
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw eltec_406mca_emitter_tester.py
) else (
    python eltec_406mca_emitter_tester.py
    if errorlevel 1 pause
)
