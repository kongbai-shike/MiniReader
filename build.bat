@echo off
rem ===========================================================================
rem  MiniReader build script
rem
rem    build.bat              folder build (default, fastest start)
rem    build.bat onefile      onefile build (single exe, unpacks on every start)
rem    build.bat console      folder build with a console window (debugging)
rem    build.bat clean        remove build\ dist\ dist-onefile\
rem
rem  Output:
rem    dist\MiniReader\MiniReader.exe          folder build
rem    dist-onefile\MiniReader.exe             onefile build
rem
rem  Always call PyInstaller as 'py -3.12 -m PyInstaller': pip installs
rem  pyinstaller.exe into ...\Python312\Scripts\, which is not on PATH.
rem
rem  ---- Why every byte in this file is pure ASCII (do not undo it) --------
rem  Oct 2026 fix. If the console code page is 936 (or chcp fails, or there is
rem  no real console because an IDE / launcher captured the output), cmd
rem  decodes this file as GBK. A GBK trail byte may be any byte in 0x40-0x7E,
rem  so it eats ] \ [ | ^ and Chinese fragments turn into 'commands':
rem      '<mojibake>' is not recognized as an internal or external command
rem  A Chinese rem line is not safe either: one bad byte pair can swallow CR
rem  and merge the next line into the current one. A pure ASCII file decodes
rem  identically at cp936, cp65001, and when chcp silently fails.
rem  So: comments and messages here are English on purpose. The Chinese
rem  notes for this feature live in the delivery report and docs/.
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem --- code page and Python output encoding ---------------------------------
rem chcp stays because tools\make_icon.py prints check marks: with the console
rem at UTF-8 they render correctly, and PYTHONIOENCODING keeps Python from
rem raising UnicodeEncodeError. If chcp fails nothing here breaks, because no
rem command below depends on the code page any more.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

set PY=py -3.12
set PYI=%PY% -m PyInstaller

rem --- preflight: interpreter present, and nobody holding the output folder ---
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python was not found as 'py -3.12'. PyInstaller is installed for 3.12,
    echo   so the build needs exactly that interpreter.
    echo   Install: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem A running MiniReader.exe locks every DLL under dist\NAME\_internal, so
rem PyInstaller cannot clean that folder and dies with a PermissionError.
rem Refuse early with a readable message instead of that traceback.
rem findstr decides the match, not tasklist: some Windows builds return 0 even
rem when no task matches, which would block every build.
tasklist /fi "imagename eq MiniReader.exe" /nh 2>nul | findstr /i /c:"MiniReader.exe" >nul
if errorlevel 1 goto :no_running_app
echo.
echo   MiniReader.exe is still running. Close it, then run this again:
echo   Windows keeps the DLLs under dist\...\_internal locked while it runs.
echo.
pause
exit /b 1
:no_running_app

if /i "%~1"=="clean" goto :clean

rem --- 1/3 generate icon and version info (build products, never committed) -
echo [1/3] build icon ...
%PY% tools\make_icon.py || goto :fail

echo [2/3] build version info ...
%PY% tools\make_version_info.py || goto :fail

rem --- 2/3 pack --------------------------------------------------------------
if /i "%~1"=="onefile" (
    echo [3/3] package: onefile mode, single exe ...
    set MR_ONEFILE=1
    set DIST=dist-onefile
) else if /i "%~1"=="console" (
    echo [3/3] package: folder mode, with console window ...
    set MR_CONSOLE=1
    set DIST=dist
) else (
    echo [3/3] package: folder mode ...
    set DIST=dist
)

%PYI% MiniReader.spec --noconfirm --clean --distpath %DIST% || goto :fail

echo.
echo Done. Output folder: %DIST%\
if exist "%DIST%\MiniReader\MiniReader.exe" echo   folder : %DIST%\MiniReader\MiniReader.exe
if exist "%DIST%\MiniReader.exe" echo   onefile: %DIST%\MiniReader.exe
echo.
rem The exe lives one level deeper in folder mode; print the path that exists.
echo Self check:
if exist "%DIST%\MiniReader\MiniReader.exe" echo   %DIST%\MiniReader\MiniReader.exe --check
if exist "%DIST%\MiniReader\MiniReader.exe" echo   %DIST%\MiniReader\MiniReader.exe --smoke 4
if exist "%DIST%\MiniReader.exe" echo   %DIST%\MiniReader.exe --check
if exist "%DIST%\MiniReader.exe" echo   %DIST%\MiniReader.exe --smoke 4
goto :eof

:clean
echo Cleaning build\ dist\ dist-onefile\ ...
if exist build         rmdir /s /q build
if exist dist          rmdir /s /q dist
if exist dist-onefile  rmdir /s /q dist-onefile
echo Cleaned. assets\minireader.ico is rebuilt by every build.
goto :eof

:fail
set "RC=%ERRORLEVEL%"
echo.
echo Build failed, exit code %RC%.
rem Keeps the window open for double-click users; with redirected stdin (CI)
rem pause returns immediately, so automation is unaffected.
pause
exit /b %RC%
