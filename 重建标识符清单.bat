@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ===========================================================================
rem  OPTIONAL: rebuild unsafe-values.json (the "identifier" blacklist).
rem  Needs the .NET SDK dotnet command. Not required for normal use.
rem  NOTE: keep this file pure ASCII (see 一键汉化.bat for the reason).
rem ===========================================================================

call "%~dp0_findpython.bat"
if not defined PY goto nopy

where dotnet >nul 2>nul
if errorlevel 1 goto nodotnet

%PY% _i18nmsg.py rebuild_banner
echo.

if not exist "_work" mkdir "_work"

echo [1/4] extracting the app assembly ...
%PY% bundle_i18n.py --extract-asm "_work\launcher.dll"
if errorlevel 1 goto fail
echo.

echo [2/4] analysing string-literal usage context with .NET ...
dotnet run "_tools\analyze.cs" -- "_work\launcher.dll" > "_work\strings_ctx.tsv" 2> "_work\strings_ctx.err"
if errorlevel 1 goto dotnetfail
echo.

echo [3/4] building the blacklist ...
%PY% build_unsafe.py "_work\strings_ctx.tsv" "unsafe-values.json"
if errorlevel 1 goto fail
echo.

echo [4/4] done. You can run 一键汉化.bat again now.
echo.
pause
exit /b 0

:nodotnet
%PY% _i18nmsg.py rebuild_nodotnet
echo.
pause
exit /b 0

:dotnetfail
echo.
echo [ERROR] dotnet failed. Output:
type "_work\strings_ctx.err"
echo.
echo A recent .NET SDK (10+) is required. Skipping this tool does not affect
echo the normal one-click localisation.
echo.
pause
exit /b 1

:fail
echo.
echo [ERROR] see the messages above.
echo.
pause
exit /b 1

:nopy
echo.
echo [ERROR] No usable Python found.
echo.
pause
exit /b 1
