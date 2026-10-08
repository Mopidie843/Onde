; Installateur Windows d'Onde (NSIS 3, interface « Modern UI 2 »).
; Fabriqué par windows/installer/build.sh (Linux, makensis) ou build_installer.bat (Windows, NSIS).
;  * pas besoin des droits administrateur : installé dans %LOCALAPPDATA%\Programs\Onde ;
;  * conditions d'utilisation à accepter (case à cocher) ;
;  * Python 3.12 embarqué téléchargé et installé dans le dossier d'Onde (rien d'autre n'est modifié) ;
;  * raccourcis Menu Démarrer / Bureau, lancement avec Windows (au choix), désinstallation propre ;
;  * tes données (bibliothèque, profils, réglages) restent dans ton dossier utilisateur.
Unicode true
SetCompressor /SOLID lzma
ManifestDPIAware true

!define APPNAME "Onde"
!ifndef VERSION
  !define VERSION "1.3.4"
!endif
!define SRC "../.."
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Onde"

Name "${APPNAME}"
Caption "Installation d'${APPNAME} ${VERSION}"
BrandingText "Onde ${VERSION} — ta musique, chez toi"
OutFile "${SRC}/Onde-Installation-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Onde"
InstallDirRegKey HKCU "Software\Onde" "InstallDir"
RequestExecutionLevel user
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1036 "ProductName" "Onde"
VIAddVersionKey /LANG=1036 "FileDescription" "Installation d'Onde"
VIAddVersionKey /LANG=1036 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1036 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1036 "CompanyName" "Onde"
VIAddVersionKey /LANG=1036 "LegalCopyright" "Usage privé uniquement"

!include "MUI2.nsh"
!include "LogicLib.nsh"

!define MUI_ICON "${SRC}/windows/onde.ico"
!define MUI_UNICON "${SRC}/windows/onde.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEFINISHPAGE_BITMAP "welcome.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "welcome.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "header.bmp"
!define MUI_BGCOLOR "FFFFFF"

!define MUI_WELCOMEPAGE_TITLE "Bienvenue dans l'installation d'Onde"
!define MUI_WELCOMEPAGE_TEXT "Onde transforme ce PC en serveur de musique : ta bibliothèque, tes playlists et tes paroles, sur ton ordinateur et sur ton téléphone.$\r$\n$\r$\nL'installation télécharge les composants nécessaires (environ 60 Mo) : garde Internet branché.$\r$\n$\r$\nClique sur Suivant pour continuer."
!insertmacro MUI_PAGE_WELCOME

!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_LICENSEPAGE_CHECKBOX_TEXT "J'accepte : usage privé, je ne partage rien à personne."
!define MUI_PAGE_HEADER_TEXT "Conditions d'utilisation"
!define MUI_PAGE_HEADER_SUBTEXT "À lire avant d'installer Onde."
!define MUI_LICENSEPAGE_TEXT_TOP "En installant Onde, tu acceptes ces conditions."
!insertmacro MUI_PAGE_LICENSE "${SRC}/CONDITIONS.txt"

!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "Onde est installé"
!define MUI_FINISHPAGE_TEXT "Onde se met à jour tout seul, en arrière-plan, à chaque démarrage.$\r$\n$\r$\nSur ton téléphone : ouvre l'adresse affichée dans Onde (Réglages), ou installe l'appli Android."
!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "$\"$INSTDIR\onde_app.py$\""
!define MUI_FINISHPAGE_RUN_TEXT "Lancer Onde maintenant"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "French"

; ------------------------------------------------------------------ arrêt d'Onde s'il tourne
!macro StopOnde
  nsExec::ExecToStack 'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -like ''*onde_app.py*'' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force }"'
  Pop $0
  Pop $0
!macroend

!macro RunStep STEP LABEL
  DetailPrint "${LABEL}"
  nsExec::ExecToStack 'powershell -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\windows\installer\install.ps1" -AppDir "$INSTDIR" -Step ${STEP}'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "Erreur : $1"
    MessageBox MB_ICONSTOP|MB_OK "L'installation n'a pas pu se terminer :$\r$\n$\r$\n$1$\r$\n$\r$\nVérifie ta connexion Internet puis relance l'installation (tes données sont gardées).$\r$\nDétails : $INSTDIR\installation.log"
    Abort
  ${EndIf}
!macroend

Section "Onde (obligatoire)" SecMain
  SectionIn RO
  !insertmacro StopOnde
  SetOutPath "$INSTDIR"
  SetOverwrite on
  DetailPrint "Copie d'Onde…"
  File /r /x __pycache__ /x *.pyc "${SRC}/onde"
  File "${SRC}/onde_app.py"
  File "${SRC}/requirements.txt"
  File "${SRC}/requirements-ai.txt"
  File "${SRC}/CONDITIONS.txt"
  File "${SRC}/README.md"
  SetOutPath "$INSTDIR\windows"
  File "${SRC}/windows/onde.ico"
  SetOutPath "$INSTDIR\windows\tools"
  File /x ffmpeg.exe /x ffprobe.exe "${SRC}/windows/tools/*.*"
  SetOutPath "$INSTDIR\windows\installer"
  File "install.ps1"
  File "pip-*.whl"
  SetOutPath "$INSTDIR\android"
  File /nonfatal "${SRC}/android/Onde.apk"
  File /nonfatal "${SRC}/android/Onde.apk.json"
  SetOutPath "$INSTDIR"

  !insertmacro RunStep "python" "Téléchargement et installation de Python 3.12 (11 Mo)…"
  !insertmacro RunStep "pip" "Préparation de l'installateur de modules…"
  !insertmacro RunStep "modules" "Installation des modules d'Onde (1 à 3 minutes)…"
  DetailPrint "Téléchargement de ffmpeg (conversion audio)…"
  nsExec::ExecToStack 'powershell -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\windows\installer\install.ps1" -AppDir "$INSTDIR" -Step ffmpeg'
  Pop $0
  Pop $0

  CreateDirectory "$SMPROGRAMS\Onde"
  CreateShortcut "$SMPROGRAMS\Onde\Onde.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\onde_app.py"' "$INSTDIR\windows\onde.ico" 0
  CreateShortcut "$SMPROGRAMS\Onde\Conditions d'utilisation.lnk" "$INSTDIR\CONDITIONS.txt"
  CreateShortcut "$SMPROGRAMS\Onde\Désinstaller Onde.lnk" "$INSTDIR\Desinstaller-Onde.exe"

  WriteUninstaller "$INSTDIR\Desinstaller-Onde.exe"
  WriteRegStr HKCU "Software\Onde" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Onde"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Onde"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\windows\onde.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Desinstaller-Onde.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Raccourci sur le Bureau" SecDesktop
  CreateShortcut "$DESKTOP\Onde.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\onde_app.py"' "$INSTDIR\windows\onde.ico" 0
SectionEnd

Section "Lancer Onde au démarrage de Windows" SecStartup
  CreateShortcut "$SMSTARTUP\Onde.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\onde_app.py" --background' "$INSTDIR\windows\onde.ico" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "Onde et ses composants (Python embarqué, modules, ffmpeg)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Une icône Onde sur le Bureau."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Onde démarre tout seul avec Windows, en arrière-plan (pratique pour écouter depuis ton téléphone)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  !insertmacro StopOnde
  Delete "$DESKTOP\Onde.lnk"
  Delete "$SMSTARTUP\Onde.lnk"
  RMDir /r "$SMPROGRAMS\Onde"
  RMDir /r "$INSTDIR\onde"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\windows"
  RMDir /r "$INSTDIR\android"
  Delete "$INSTDIR\onde_app.py"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\requirements-ai.txt"
  Delete "$INSTDIR\CONDITIONS.txt"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\installation.log"
  Delete "$INSTDIR\Desinstaller-Onde.exe"
  RMDir "$INSTDIR"  ; (seulement s'il est vide : onde.env et tes fichiers restent)
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\Onde"
  DetailPrint "Tes données (bibliothèque, profils, réglages) sont gardées dans ton dossier utilisateur (.local\share\onde)."
SectionEnd
