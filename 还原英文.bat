@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem  NOTE: keep this file pure ASCII (see 一键汉化.bat for the reason).
call "%~dp0_findpython.bat"
if not defined PY goto nopy

%PY% _i18nmsg.py restore_banner
echo.
%PY% bundle_i18n.py --restore
echo.
pause
exit /b 0

:nopy
echo.
echo [ERROR] No usable Python found.
echo Check that Python 3.8+ is installed and on PATH.
echo.
pause
exit /b 1
