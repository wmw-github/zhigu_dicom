@echo off
rem ===================================================================
rem  Register / unregister the zhigu:// protocol for "ZhiGu Imaging".
rem  Keep this file pure ASCII: it is read by cmd.exe in the OEM code
rem  page, and the install path may contain Chinese characters.
rem ===================================================================
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "APP=%HERE%\..\CT3D260414_release"
if not exist "%APP%\web_bridge_setup.py" set "APP=%HERE%"

if not exist "%APP%\pyenv\python.exe" (
    echo [ERROR] python.exe not found:
    echo         %APP%\pyenv\python.exe
    echo.
    echo Put this web_bridge folder next to CT3D260414_release, then retry.
    pause
    exit /b 1
)

"%APP%\pyenv\python.exe" "%APP%\web_bridge_setup.py" %*
exit /b %ERRORLEVEL%
