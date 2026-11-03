@echo off
rem Zhigu DICOM - Native Messaging launcher
rem Chrome starts this file via CreateProcess and talks over stdin/stdout.
rem %~dp0 locates the install dir so no absolute path is hardcoded here
rem (Chinese paths inside .bat break under the GBK code page).
rem Do NOT echo anything: stdout is the protocol channel.
setlocal
"%~dp0pyenv\python.exe" "%~dp0web_bridge_native.py" %*
