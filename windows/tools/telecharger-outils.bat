@echo off
rem Telecharge ffmpeg.exe (une seule fois) dans ce dossier. Lance automatiquement par start.bat.
cd /d "%~dp0"
if exist ffmpeg.exe exit /b 0
echo * Telechargement de ffmpeg (environ 55 Mo, une seule fois)...
curl.exe -L -f -o ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip
if errorlevel 1 goto fail
tar.exe -xf ffmpeg.zip --strip-components=2 "*/bin/ffmpeg.exe"
if not exist ffmpeg.exe goto fail
del ffmpeg.zip
echo ffmpeg installe.
exit /b 0
:fail
if exist ffmpeg.zip del ffmpeg.zip
echo Telechargement impossible : pose ffmpeg.exe dans ce dossier (https://github.com/BtbN/FFmpeg-Builds/releases).
exit /b 1
