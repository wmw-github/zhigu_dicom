@echo off
rem =============================================================
rem  Zhigu DICOM - launcher
rem  Prefer a local embedded runtime (pyenv\) if present;
rem  otherwise fall back to the system Python 3.8+ on PATH.
rem =============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set "PY_DIR=%~dp0pyenv"
set "PYTHON_EXE=%PY_DIR%\pythonw.exe"

if exist "%PYTHON_EXE%" (
  set "PATH=%PY_DIR%;%PY_DIR%\Library\bin;%PY_DIR%\DLLs;%PY_DIR%\Lib\site-packages\PyQt5\Qt5\bin;%PATH%"
  set "QT_PLUGIN_PATH=%PY_DIR%\Lib\site-packages\PyQt5\Qt5\plugins"
) else (
  where pythonw >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] No local pyenv runtime found and no pythonw.exe on PATH.
    echo         Install Python 3.8+ and the deps in requirements.txt first.
    pause
    exit /b 1
  )
  set "PYTHON_EXE=pythonw"
)

set LOG_OUT=%~dp0run_env.out.log
set LOG_ERR=%~dp0run_env.err.log
del /f /q "%LOG_OUT%" >nul 2>&1
del /f /q "%LOG_ERR%" >nul 2>&1

"%PYTHON_EXE%" "%~dp0main.py" 1>"%LOG_OUT%" 2>"%LOG_ERR%"
if errorlevel 1 (
  if exist "%LOG_ERR%" type "%LOG_ERR%"
  pause
)
