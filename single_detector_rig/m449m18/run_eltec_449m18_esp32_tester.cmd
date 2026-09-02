@echo off
rem Launch the Windows/ESP32 449 M18 (5/18 Hz) tester from any working directory.
rem Windows counterpart of run_eltec_449m18_esp32_tester.sh - same app, same
rem results folder (%USERPROFILE%\Documents\Eltec_449M18_Test_Results).
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "APP_PATH=%SCRIPT_DIR%\eltec_449m18_esp32_tester.py"

rem --- log file (mirrors the Xubuntu launcher's ~/.local/state location) ----
set "LOG_DIR=%LOCALAPPDATA%\eltec-449m18-esp32"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
if not exist "%LOG_DIR%" set "LOG_DIR=%TEMP%\eltec-449m18-esp32"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
if not exist "%LOG_DIR%" (
    echo Eltec 449 M18 ESP32 Tester: could not create a log directory. 1>&2
    exit /b 1
)
set "LOG_FILE=%LOG_DIR%\launcher.log"

rem Rotate past 5 MB so a long-running rig PC does not grow an unbounded log.
if exist "%LOG_FILE%" (
    for %%F in ("%LOG_FILE%") do set "LOG_SIZE=%%~zF"
    if !LOG_SIZE! GTR 5242880 move /y "%LOG_FILE%" "%LOG_FILE%.previous" >nul 2>&1
)

if not exist "%APP_PATH%" (
    call :fail "Application file not found: %APP_PATH%"
    exit /b 1
)

rem --- pick an interpreter -------------------------------------------------
rem ELTEC_PYTHON wins (same override as the Xubuntu launcher). Otherwise prefer
rem pythonw.exe so the GUI starts without a console window behind it.
set "PYTHON_BIN="
if defined ELTEC_PYTHON set "PYTHON_BIN=%ELTEC_PYTHON%"
if not defined PYTHON_BIN (
    for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYTHON_BIN=%%~$PATH:P"
)
if not defined PYTHON_BIN (
    for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PYTHON_BIN=%%~$PATH:P"
)
if not defined PYTHON_BIN (
    for %%P in (py.exe) do if not "%%~$PATH:P"=="" set "PYTHON_BIN=%%~$PATH:P"
)
if not defined PYTHON_BIN (
    call :fail "Python 3 was not found. Install Python 3 and the dependencies listed in README.md."
    exit /b 1
)
if not exist "%PYTHON_BIN%" (
    call :fail "Python is not executable: %PYTHON_BIN%"
    exit /b 1
)

rem --- run -----------------------------------------------------------------
set "PYTHONUNBUFFERED=1"
>>"%LOG_FILE%" 2>&1 (
    echo.
    echo [%DATE% %TIME%] Starting Eltec 449 M18 ESP32 Tester
    echo Application: %APP_PATH%
    echo Python: %PYTHON_BIN%
    pushd "%SCRIPT_DIR%"
    "%PYTHON_BIN%" "%APP_PATH%" %*
    popd
)
set "STATUS=%ERRORLEVEL%"

if not "%STATUS%"=="0" (
    call :fail "The application exited with status %STATUS%."
)
exit /b %STATUS%

rem -------------------------------------------------------------------------
:fail
set "MESSAGE=%~1"
>>"%LOG_FILE%" echo %DATE% %TIME% ERROR: %MESSAGE%
echo Eltec 449 M18 ESP32 Tester: %MESSAGE% 1>&2
echo Details: %LOG_FILE% 1>&2
rem Popup equivalent of the Xubuntu notify-send/zenity path, so a double-clicked
rem shortcut does not fail silently when there is no console to read. The
rem message box blocks until dismissed, so tests and headless runs set
rem ELTEC_LAUNCHER_NO_DIALOG=1 and read the log instead.
if defined ELTEC_LAUNCHER_NO_DIALOG goto :eof
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
    "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($env:MESSAGE + [Environment]::NewLine + [Environment]::NewLine + 'Details: ' + $env:LOG_FILE, 'Eltec 449 M18 tester could not start', 'OK', 'Error')" >nul 2>&1
goto :eof
