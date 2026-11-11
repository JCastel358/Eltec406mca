@echo off
setlocal
if defined ELTEC_PYTHON (
    "%ELTEC_PYTHON%" "%~dp0power_noise_compare.py" %*
) else (
    python "%~dp0power_noise_compare.py" %*
)
if errorlevel 1 (
    echo Unable to run the noise comparison. Python, numpy, and pyserial are required.
    pause
)
