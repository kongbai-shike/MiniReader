@echo off
rem ===========================================================================
rem  MiniReader 打包脚本
rem
rem    build.bat              目录模式（默认，推荐：启动快）
rem    build.bat onefile      单文件模式（好分发，但每次启动都要解压）
rem    build.bat console      带控制台窗口（排查打包问题时用）
rem    build.bat clean        删掉 build/ dist/ dist-onefile/
rem
rem  产物：
rem    dist\MiniReader\MiniReader.exe          目录模式
rem    dist-onefile\MiniReader.exe             单文件模式
rem
rem  注意：必须用 py -3.12 -m PyInstaller 调用。pip 会把 pyinstaller.exe 装到
rem  ...\Python312\Scripts\，而那个目录默认不在 PATH 上。
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem --- 代码页与 Python 输出编码 --------------------------------------------
rem 这个脚本是 UTF-8（含中文），tools\make_icon.py 的自查会 print ✓ / ✗。
rem cmd.exe 默认代码页 936，两者都会变成乱码；更糟的是那两个字符编不出来，
rem 会让 make_icon.py 直接 UnicodeEncodeError，打包卡在第一步。
rem chcp 65001 让 cmd 按 UTF-8 解读本文件与子进程输出；PYTHONIOENCODING
rem 是给 Python 的兜底（工具脚本自己也会 reconfigure stdout，见
rem tools\make_icon.py::force_utf8_console，两道都留着更保险）。
rem >nul 是因为 chcp 自己会打印一行「Active code page: 65001」，没必要看。
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

set PY=py -3.12
set PYI=%PY% -m PyInstaller

if /i "%~1"=="clean" goto :clean

rem --- 1. 生成图标与版本信息（都是生成物，不提交进仓库）--------------------
echo [1/3] 生成图标...
%PY% tools\make_icon.py || goto :fail

echo [2/3] 生成版本信息...
%PY% tools\make_version_info.py || goto :fail

rem --- 2. 打包 -------------------------------------------------------------
if /i "%~1"=="onefile" (
    echo [3/3] 打包（单文件模式）...
    set MR_ONEFILE=1
    set DIST=dist-onefile
) else if /i "%~1"=="console" (
    echo [3/3] 打包（目录模式 + 控制台）...
    set MR_CONSOLE=1
    set DIST=dist
) else (
    echo [3/3] 打包（目录模式）...
    set DIST=dist
)

%PYI% MiniReader.spec --noconfirm --clean --distpath %DIST% || goto :fail

echo.
echo 完成。产物在 %DIST%\
if exist "%DIST%\MiniReader\MiniReader.exe" echo   目录模式：%DIST%\MiniReader\MiniReader.exe
if exist "%DIST%\MiniReader.exe"                 echo   单文件：  %DIST%\MiniReader.exe
echo.
echo 自查：
echo   %DIST%\MiniReader.exe --check
echo   %DIST%\MiniReader.exe --smoke 4
goto :eof

:clean
echo 清理 build\ dist\ dist-onefile\ ...
if exist build         rmdir /s /q build
if exist dist          rmdir /s /q dist
if exist dist-onefile  rmdir /s /q dist-onefile
echo 已清理。（assets\minireader.ico 保留——它是生成物，但每次打包都会重建）
goto :eof

:fail
echo.
echo 打包失败，退出码 %ERRORLEVEL%。
exit /b %ERRORLEVEL%
