@echo off
rem Run every Eltec test-rig suite and print a summary (see run_all_tests.py).
rem ELTEC_PYTHON overrides the interpreter, as for the app launchers.
setlocal
set "PYTHON_BIN=python"
if defined ELTEC_PYTHON set "PYTHON_BIN=%ELTEC_PYTHON%"
"%PYTHON_BIN%" "%~dp0run_all_tests.py" %*
exit /b %ERRORLEVEL%
