@echo off
rem -------------------------------------------------------------
rem  Onde - version Windows (pour tester sur un PC).
rem  Double-clique : installe tout la 1re fois puis lance Onde.
rem  Sur le Raspberry Pi, utilise plutot ./start.sh
rem -------------------------------------------------------------
title Onde
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 est introuvable. Installe-le depuis https://www.python.org/downloads/
  echo ^(coche Add python.exe to PATH pendant l'installation^)
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo * Creation de l'environnement Python...
  %PY% -m venv .venv || (echo Impossible de creer l'environnement Python & pause & exit /b 1)
)

rem (re)installe les dependances la 1re fois, puis a chaque mise a jour de requirements.txt
fc /b requirements.txt ".venv\.onde-req" >nul 2>nul
if errorlevel 1 (
  echo * Installation des dependances ^(1 a 2 minutes^)...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo Echec de l'installation & pause & exit /b 1)
  copy /y requirements.txt ".venv\.onde-req" >nul
)

rem outils Windows fournis (ffmpeg.exe) : Onde les trouve dans windows\tools
set "HAVE_FF="
where ffmpeg >nul 2>nul
if not errorlevel 1 set "HAVE_FF=1"
if exist "windows\tools\ffmpeg.exe" set "HAVE_FF=1"
if not defined HAVE_FF call "windows\tools\telecharger-outils.bat"
if exist "windows\tools\ffmpeg.exe" set "PATH=%PATH%;%~dp0windows\tools"

rem start.bat ia : installe le calage des paroles par IA (stable-whisper), puis lance Onde
if /i "%~1"=="ia" (
  echo * Installation de l'IA des paroles ^(plusieurs minutes, ~2 Go^)...
  ".venv\Scripts\python.exe" -m onde.align install || (echo Echec de l'installation de l'IA & pause & exit /b 1)
)

rem port choisi dans onde.env (8000 par defaut)
set "PORT=8000"
if exist onde.env for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"ONDE_PORT=" onde.env') do set "PORT=%%b"

rem ouvre le navigateur quand le serveur est pret (http://localhost marche aussi avec le HTTPS active)
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%PORT%"

rem Onde peut se redemarrer lui-meme (ex. activation du HTTPS dans Reglages) : code de sortie 3
set "ONDE_LAUNCHER=start.bat"
:run
".venv\Scripts\python.exe" -m onde
if %errorlevel% equ 3 goto run
pause
