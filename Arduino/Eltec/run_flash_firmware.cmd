@echo off
rem Double-click this to flash Arduino\Eltec\Eltec.ino onto the bench ESP32.
rem Windows counterpart of run_flash_firmware.sh. Any arguments are passed
rem through, e.g.:  run_flash_firmware.cmd --sketch versions\Eltec_v2_2
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "PYTHON_BIN="
if defined ELTEC_PYTHON set "PYTHON_BIN=%ELTEC_PYTHON%"
if not defined PYTHON_BIN for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PYTHON_BIN=%%~$PATH:P"
if not defined PYTHON_BIN for %%P in (py.exe) do if not "%%~$PATH:P"=="" set "PYTHON_BIN=%%~$PATH:P"
if not defined PYTHON_BIN (
    echo Python 3 was not found on PATH. Install it from python.org, or set
    echo ELTEC_PYTHON to the interpreter you want to use.
    pause
    exit /b 1
)

"%PYTHON_BIN%" "%SCRIPT_DIR%\flash_firmware.py" --pause %*
exit /b %ERRORLEVEL%
