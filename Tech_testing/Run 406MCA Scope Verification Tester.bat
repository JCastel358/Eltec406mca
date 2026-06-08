@echo off
cd /d "%~dp0"
python eltec_406mca_scope_verification_tester.py
if errorlevel 1 pause
