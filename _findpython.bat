@echo off
rem ===========================================================================
rem  Locate a usable Python interpreter and export it as PY.
rem
rem  Search order:
rem    1) Portable Python bundled with this toolkit:
rem         _libs\python-embed\python.exe          (zero install, recommended)
rem    2) System Python 3.8+ (py -3 / python)
rem    3) Legacy slim Python shipped by old game builds:
rem         ..\engine\tools\python\python.exe      (removed in v0.9.34)
rem
rem  NOTE: no setlocal here on purpose, so PY survives for the caller.
rem  PY may already contain quotes (needed for paths with spaces),
rem  so call it as %PY% -- never as "%PY%".
rem
rem  NOTE: keep this file pure ASCII. Non-ASCII characters inside
rem  parenthesised blocks make cmd.exe mis-parse the script.
rem ===========================================================================

set "PY="
set "PYCAND="

rem ---- 1) bundled portable Python ----
if exist "%~dp0_libs\python-embed\python.exe" set PY="%~dp0_libs\python-embed\python.exe"

rem ---- 2) system Python ----
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PYCAND=py -3"
)
if not defined PY if not defined PYCAND (
  where python >nul 2>nul
  if not errorlevel 1 set "PYCAND=python"
)
if not defined PY if defined PYCAND (
  rem importlib.metadata exists only in Python 3.8+, used as a version gate.
  %PYCAND% -c "import importlib.metadata" >nul 2>nul
  if not errorlevel 1 set "PY=%PYCAND%"
)

rem ---- 3) legacy engine-bundled Python ----
if not defined PY (
  if exist "%~dp0..\engine\tools\python\python.exe" set PY="%~dp0..\engine\tools\python\python.exe"
)

rem Force UTF-8 I/O so Chinese text and symbols like the check mark survive
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

exit /b 0
