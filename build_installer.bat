@echo off
rem ------------------------------------------------------------
rem  Refabrique l'installateur Windows d'Onde (Onde-Installation-x.y.z.exe, a la racine).
rem  Le zip d'Onde contient deja l'installateur tout pret : ce script ne sert que si tu
rem  modifies Onde toi-meme. Il faut NSIS 3 : https://nsis.sourceforge.io/Download
rem ------------------------------------------------------------
title Onde - fabrication de l'installateur
cd /d "%~dp0"
set "NSIS="
if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" set "NSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if exist "%ProgramFiles%\NSIS\makensis.exe" set "NSIS=%ProgramFiles%\NSIS\makensis.exe"
if not defined NSIS (
  echo NSIS est introuvable : installe-le depuis https://nsis.sourceforge.io/Download puis relance.
  start https://nsis.sourceforge.io/Download
  pause
  exit /b 1
)
set "VER="
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"VERSION = " onde\config.py') do set "VER=%%~v"
set "VER=%VER: =%"
set "VER=%VER:"=%"
"%NSIS%" /DVERSION=%VER% "windows\installer\onde.nsi"
if errorlevel 1 (echo Echec de la fabrication de l'installateur & pause & exit /b 1)
echo.
echo [Onde] Termine : Onde-Installation-%VER%.exe
pause
