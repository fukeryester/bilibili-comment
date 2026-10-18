@echo off
setlocal
cd /d "%~dp0"
rem Seconds to wait after each run. Change this number to adjust the interval.
set INTERVAL_SECONDS=10
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:loop
python "%~dp0random_bili_video.py"
if errorlevel 1 goto failed
python "%~dp0ai_comment_run.py"
if errorlevel 1 goto failed
python "%~dp0bili_comment.py"
if errorlevel 1 goto failed
echo next run in %INTERVAL_SECONDS% seconds. Press Ctrl+C to stop.
timeout /t %INTERVAL_SECONDS% /nobreak
goto loop

:failed
echo failed with code %ERRORLEVEL%
echo next run in %INTERVAL_SECONDS% seconds. Press Ctrl+C to stop.
timeout /t %INTERVAL_SECONDS% /nobreak
goto loop
