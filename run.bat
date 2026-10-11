@echo off
rem MiniReader launcher.
rem
rem   double-click                  open the reader
rem   drag a TXT file onto this     open that book
rem   run.bat --check               environment self-report
rem   run.bat --smoke 3             show the window for 3 seconds and exit
rem
rem This file is deliberately ASCII-only. cmd.exe parses batch files with the OEM code
rem page, and full-width Chinese punctuation inside the script desynchronises the
rem parser (a mangled "rem" line was being executed as a command). All Chinese
rem messages are printed by Python instead, which handles its own encoding.

setlocal
cd /d "%~dp0"

set "PY="
py -3.12 -c "pass" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -c "pass" >nul 2>nul && set "PY=py"
if not defined PY python -c "pass" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "pass" >nul 2>nul && set "PY=python3"

if not defined PY (
    echo.
    echo   Python not found. MiniReader needs Python 3.8 or newer.
    echo   Download: https://www.python.org/downloads/
    echo.
    echo   ^(Python mei you zhao dao. Qing xian an zhuang Python 3.8+ ^)
    echo.
    pause
    exit /b 1
)

rem The program prints its own "install a Qt binding" hint if Qt is missing, so no
rem import probe is needed here. Stderr is left visible on purpose.
%PY% -m minireader %*
set "RC=%ERRORLEVEL%"

rem A normal exit returns 0 and the window is already gone, so only pause on failure.
if not "%RC%"=="0" pause
endlocal & exit /b %RC%
