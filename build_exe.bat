@echo off
rem ------------------------------------------------------------
rem  Fabrique Onde.exe : un seul fichier, sans Python a installer
rem  sur les PC ou on le lance. Double-clique, patiente 2 a 5 min :
rem  le resultat est dans le dossier "dist".
rem  Il faut Python 3.10 ou plus sur CE PC (https://www.python.org).
rem ------------------------------------------------------------
title Onde - fabrication de Onde.exe
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto havepy
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
:havepy
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto nopython

if exist ".venv-build\Scripts\python.exe" goto venvok
echo [Onde] Creation de l'environnement de fabrication...
%PY% -m venv .venv-build
if errorlevel 1 goto fail
:venvok
set "BPY=%~dp0.venv-build\Scripts\python.exe"

echo [Onde] Installation des outils, la 1re fois : quelques minutes...
"%BPY%" -m pip install -q --disable-pip-version-check --upgrade pip
if errorlevel 1 goto fail
"%BPY%" -m pip install -q --disable-pip-version-check -r requirements.txt "pyinstaller>=6.6"
if errorlevel 1 goto fail

echo [Onde] Fabrication de Onde.exe...
"%BPY%" -m PyInstaller --noconfirm --clean onde.spec
if errorlevel 1 goto fail

if exist "windows\tools\ffmpeg.exe" (
  echo [Onde] Copie des outils Windows ^(ffmpeg^) a cote de Onde.exe...
  xcopy /e /i /y /q "windows\tools" "dist\tools" >nul
)

echo.
echo [Onde] Termine : %~dp0dist\Onde.exe
echo Copie Onde.exe (avec le dossier tools) ou tu veux et double-clique dessus. Ses reglages (onde.env)
echo se creent a cote ; un ffmpeg.exe pose dans le meme dossier est utilise.
echo.
explorer dist
pause
exit /b 0

:nopython
echo.
echo Python 3.10 ou plus est introuvable. Installe-le depuis
echo https://www.python.org/downloads/ et coche "Add python.exe to PATH".
pause
exit /b 1

:fail
echo.
echo La fabrication a echoue : copie les messages ci-dessus pour me les envoyer.
pause
exit /b 1
