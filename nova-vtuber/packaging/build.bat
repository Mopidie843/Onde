@echo off
rem Fabrique Nova VTuber.exe puis l'installateur, sur ce PC.  Double-cliquez sur packaging\build.bat
rem Il faut : Python 3.12 (python.org) ; pour l'installateur, Inno Setup 6 (jrsoftware.org/isdl.php).
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
if not exist main.py (echo main.py introuvable : ce script doit rester dans le dossier packaging de Nova. & pause & exit /b 1)
if not exist src\ui\main_window.py (echo src\ui\main_window.py introuvable : le projet est incomplet. & pause & exit /b 1)

echo [1/5] Environnement de fabrication...
if not exist .venv-build\Scripts\python.exe (py -3.12 -m venv .venv-build || python -m venv .venv-build) || goto :err
set PY=.venv-build\Scripts\python.exe
%PY% -m pip install --upgrade pip >nul
if exist requirements.txt (%PY% -m pip install -r requirements.txt || goto :err)
%PY% -m pip install -r packaging\requirements-exe.txt || goto :err

echo [2/5] Ressources (modèles de suivi, avatars)...
%PY% -c "import setup_assets, sys; m = setup_assets.missing_assets(); m and setup_assets.main()" || goto :err

echo [3/5] Fabrication de Nova VTuber.exe...
%PY% -m PyInstaller --noconfirm --clean packaging\nova.spec || goto :err

echo [4/5] Test de démarrage...
set NOVA_SMOKE_MS=6000
"dist\Nova VTuber\Nova VTuber.exe" --smoke-test
if errorlevel 1 (echo Le .exe ne démarre pas : voir "dist\Nova VTuber\logs\nova.log". & goto :err)
for %%D in (config profiles logs) do if exist "dist\Nova VTuber\%%D" rmdir /s /q "dist\Nova VTuber\%%D"

echo [5/5] Installateur...
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (echo Inno Setup 6 absent : seul le dossier dist\Nova VTuber a été fabriqué. & goto :fin)
"%ISCC%" /Q packaging\installer.iss || goto :err

:fin
echo.
echo Terminé : dossier dist  (Nova VTuber\Nova VTuber.exe et Nova-VTuber-Installation-*.exe)
explorer dist
pause
exit /b 0
:err
echo.
echo ÉCHEC de la fabrication (voir les messages ci-dessus).
pause
exit /b 1
