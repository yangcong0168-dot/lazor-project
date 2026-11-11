@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 goto location_error

set "LAZOR_PYTHON="
set "LAZOR_GUI="
call :try_python "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if defined LAZOR_PYTHON goto launch
call :try_python "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if defined LAZOR_PYTHON goto launch
for /f "delims=" %%P in ('where python.exe 2^>nul') do call :try_python "%%P"
if defined LAZOR_PYTHON goto launch
for /f "delims=" %%P in ('where python3.exe 2^>nul') do call :try_python "%%P"
if defined LAZOR_PYTHON goto launch

echo A Python 3.10+ installation with tkinter could not be found.
echo Install Python with Tcl/Tk support, or run this from a suitable Python:
echo     python -m lazor.editor
echo See docs\editor_zh.md for details.
popd
pause
exit /b 1

:launch
start "Lazor Editor" "%LAZOR_GUI%" -m lazor.editor
if errorlevel 1 (
    echo The editor could not be started with "%LAZOR_GUI%".
    popd
    pause
    exit /b 1
)
popd
exit /b 0

:try_python
if defined LAZOR_PYTHON exit /b 0
if not exist "%~1" exit /b 0
rem Do not activate Microsoft Store application aliases.
if /i "%~dp1"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" exit /b 0
"%~1" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "LAZOR_PYTHON=%~1"
set "LAZOR_GUI=%~1"
if exist "%~dp1pythonw.exe" set "LAZOR_GUI=%~dp1pythonw.exe"
exit /b 0

:location_error
echo The project folder could not be opened.
pause
exit /b 1
