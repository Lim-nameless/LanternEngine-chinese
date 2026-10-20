@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem  NOTE: keep this file pure ASCII.
rem  cmd.exe reads .bat files in byte chunks; UTF-8 Chinese can land on a chunk
rem  boundary and desync the parser (it then runs echo lines as commands).
rem  All Chinese text is printed by _i18nmsg.py instead.

call "%~dp0_findpython.bat"
if not defined PY goto nopy

%PY% _i18nmsg.py banner
echo.

%PY% bundle_i18n.py --apply --verify
set "RC=%ERRORLEVEL%"
echo.

if "%RC%"=="0" goto ok
if "%RC%"=="2" goto already
if "%RC%"=="3" goto degraded
goto fail

:ok
%PY% _i18nmsg.py ok
goto end

:already
%PY% _i18nmsg.py already
goto end

:degraded
%PY% _i18nmsg.py degraded
goto end

:fail
%PY% _i18nmsg.py fail
goto end

:end
echo.
pause
exit /b %RC%

:nopy
echo.
echo [ERROR] No usable Python found.
echo.
echo This localization kit needs Python 3.8 or newer.
echo Install it and tick "Add Python to PATH":
echo     https://www.python.org/downloads/
echo.
pause
exit /b 1
