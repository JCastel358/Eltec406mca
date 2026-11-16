@echo off
rem Open the M25/HUB live noise and settling viewer. Acquisition starts only in the page.
call "%~dp0m40623\run_eltec_40623_array_tester.cmd" --live %*
