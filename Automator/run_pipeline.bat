@echo off
SET REPO=C:\Users\virat.arya\ETG\SoftsDatabase - Documents\Database\Hardmine\Non Fundamental\Seasonality
SET LOG=%REPO%\Automator\logs\automation_log.txt
SET PYTHON=C:\Users\virat.arya\AppData\Local\anaconda3\python.exe

:: Prevent Git Credential Manager from showing an interactive dialog in unattended runs.
:: If credentials are cached it pushes silently; if not, it fails immediately instead of hanging.
set GCM_INTERACTIVE=never
set GIT_TERMINAL_PROMPT=0
echo. >> "%LOG%"
echo ============================================ >> "%LOG%"
echo Run started: %DATE% %TIME% >> "%LOG%"

echo Running Seasonal Dashboard ingest... >> "%LOG%"
"%PYTHON%" "%REPO%\Code\ingest.py" >> "%LOG%" 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: ingest.py failed >> "%LOG%"
    goto :error
)

echo Git add, commit and push... >> "%LOG%"
cd /d "%REPO%"
git add Database\*.parquet >> "%LOG%" 2>&1
git commit --allow-empty -m "Seasonal Dashboard data update %DATE% %TIME%" >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Git push failed >> "%LOG%"
    goto :error
)

echo Run finished successfully: %DATE% %TIME% >> "%LOG%"
echo Sending success email... >> "%LOG%"
"%PYTHON%" "%REPO%\Automator\notify.py" >> "%LOG%" 2>&1
goto :end

:error
echo Sending failure email... >> "%LOG%"
"%PYTHON%" "%REPO%\Automator\notify.py" --fail >> "%LOG%" 2>&1

:end
