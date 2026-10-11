; Installateur de Nova VTuber (Inno Setup 6) : iscc packaging\installer.iss /DAppVersion=1.0.0
; Installé pour l'utilisateur seul (pas besoin d'être administrateur) dans un dossier où Nova peut écrire
; ses réglages, profils et avatars. Une mise à jour s'installe par-dessus sans rien perdre.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Nova VTuber"
#define AppExe "Nova VTuber.exe"

[Setup]
AppId={{A4115520-1868-5AA2-B645-3B19E155CB77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Nova VTuber
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Nova-VTuber-Installation-{#AppVersion}
SetupIconFile=nova.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardSizePercent=110
WizardImageFile=wizard1.bmp,wizard2.bmp
WizardSmallImageFile=wizard_small1.bmp,wizard_small2.bmp
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=force
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[CustomMessages]
fr.Welcome=Nova anime votre avatar VRM avec votre webcam et votre micro, pour vos lives et vos appels.
fr.DesktopIcon=Créer un raccourci sur le Bureau
fr.StartWithWindows=Lancer Nova au démarrage de Windows
fr.Launch=Lancer Nova VTuber maintenant
fr.DeleteData=Supprimer aussi vos réglages, profils, avatars et accessoires ?%n%n(Choisissez « Non » pour les garder en cas de réinstallation.)

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "startup"; Description: "{cm:StartWithWindows}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Excludes: "\avatars\*,\config\*,\profiles\*,\logs\*"; Flags: ignoreversion recursesubdirs createallsubdirs
; avatars, accessoires et fonds fournis : jamais écrasés s'ils ont été modifiés
Source: "..\dist\{#AppName}\avatars\*"; DestDir: "{app}\avatars"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := ExpandConstant('{cm:Welcome}') + #13#10#13#10 + WizardForm.WelcomeLabel2.Caption;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    App := ExpandConstant('{app}');
    if DirExists(App) and (MsgBox(ExpandConstant('{cm:DeleteData}'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(App, True, True, True);
  end;
end;
