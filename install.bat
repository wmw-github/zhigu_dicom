@echo off
rem ===================================================================
rem  Zhigu DICOM - desktop shortcut installer
rem  ASCII-only on purpose: batch files with non-ASCII text can break
rem  under different codepages (e.g. Win7 cp936 vs UTF-8).
rem
rem  Creates a desktop shortcut that launches run_env.bat.
rem ===================================================================
setlocal

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "APP_DIR=%ROOT_DIR%"
set "LAUNCH_BAT=%APP_DIR%\run_env.bat"
set "ICON_PATH=%APP_DIR%\logo.ico"
set "VBS_SCRIPT=%ROOT_DIR%\CreateShortcut.vbs"

echo ============================================================
echo  Zhigu DICOM - install (create desktop shortcut)
echo ============================================================
echo.
echo App dir: %APP_DIR%
echo.

if not exist "%LAUNCH_BAT%" (
    echo [ERROR] run_env.bat not found: "%LAUNCH_BAT%"
    pause
    exit /b 1
)
if not exist "%ICON_PATH%" (
    echo [WARN] logo.ico not found, shortcut will use the default icon.
    set "ICON_PATH=%LAUNCH_BAT%"
)

set "DESKTOP="
for /f "tokens=2*" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul') do set "DESKTOP=%%b"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%PUBLIC%\Desktop"
if not exist "%DESKTOP%" (
    echo [ERROR] Desktop folder not found.
    pause
    exit /b 1
)

set "SHORTCUT_PATH=%DESKTOP%\ZhiguDICOM.lnk"
set "SHORTCUT_NAME=Zhigu DICOM"

echo Creating shortcut: %SHORTCUT_PATH%
echo.
if exist "%SHORTCUT_PATH%" del /f /q "%SHORTCUT_PATH%" >nul 2>&1

set "MADE_IT="
if exist "%VBS_SCRIPT%" (
    cscript //nologo "%VBS_SCRIPT%" "%SHORTCUT_PATH%" "%LAUNCH_BAT%" "" "%APP_DIR%" "%SHORTCUT_NAME%" "%ICON_PATH%" >nul 2>&1
    if exist "%SHORTCUT_PATH%" set "MADE_IT=1"
)

if not defined MADE_IT (
    echo VBS path failed, trying PowerShell...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT_PATH%');" ^
        "$s.TargetPath='%LAUNCH_BAT%';$s.WorkingDirectory='%APP_DIR%';" ^
        "$s.Description='%SHORTCUT_NAME%';$s.IconLocation='%ICON_PATH%';$s.Save()" >nul 2>&1
    if exist "%SHORTCUT_PATH%" set "MADE_IT=1"
)

if not defined MADE_IT (
    echo [ERROR] Failed to create the shortcut. Check write permission.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done. Double-click the "Zhigu DICOM" desktop shortcut.
echo  Keep the app directory in place after installing.
echo ============================================================
echo.
pause
exit /b 0
