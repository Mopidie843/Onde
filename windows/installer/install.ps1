# Onde - installation des composants, étape par étape (appelé par l'installateur) :
#   powershell -ExecutionPolicy Bypass -File install.ps1 -AppDir "C:\...\Onde" -Step python|pip|modules|ffmpeg
# Sans -Step : tout, dans l'ordre (pour réparer une installation à la main).
# Python 3.12 « embarqué » : dans le dossier d'Onde, il ne touche pas au reste de Windows.
param([string]$AppDir = (Resolve-Path "$PSScriptRoot\..\..").Path, [string]$Step = 'all')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ver = '3.12.10'
$py = Join-Path $AppDir 'python'
$log = Join-Path $AppDir 'installation.log'

function Note($t) { Add-Content -Path $log -Value "$(Get-Date -Format s) $t" -Encoding UTF8 }

function Do-Python {
  if (Test-Path "$py\python.exe") { return }
  $zip = Join-Path $env:TEMP "onde-python-$ver.zip"
  $url = "https://www.python.org/ftp/python/$ver/python-$ver-embed-amd64.zip"
  for ($i = 1; $i -le 3; $i++) {
    try { Invoke-WebRequest -UseBasicParsing $url -OutFile $zip; break }
    catch { if ($i -eq 3) { throw "Téléchargement de Python impossible (connexion Internet ?) : $($_.Exception.Message)" }; Start-Sleep 3 }
  }
  Expand-Archive -Path $zip -DestinationPath $py -Force
  Remove-Item $zip -ErrorAction SilentlyContinue
}

function Do-Pip {
  # chemins de Python : ses modules + le dossier d'Onde, et « import site » (nécessaire à pip)
  $pth = Get-ChildItem $py -Filter 'python*._pth' | Select-Object -First 1
  if ($pth) {
    $base = [IO.Path]::GetFileNameWithoutExtension($pth.Name)
    Set-Content -Path $pth.FullName -Encoding ASCII -Value @("$base.zip", '.', 'Lib\site-packages', '..', 'import site')
  }
  $sp = Join-Path $py 'Lib\site-packages'
  New-Item -ItemType Directory -Force -Path $sp | Out-Null
  $whl = Get-ChildItem (Join-Path $AppDir 'windows\installer') -Filter 'pip-*.whl' | Select-Object -First 1
  if (-not $whl) { throw "Fichier pip introuvable dans l'installateur." }
  # Un .whl est un zip : on le déplie directement dans site-packages. On ne lance PAS le wheel comme un
  # script ni « pip install pip » : Windows refuse qu'un pip se modifie lui-même (erreur « To modify pip »).
  $zip = Join-Path $env:TEMP 'onde-pip.zip'
  Copy-Item $whl.FullName $zip -Force
  Expand-Archive -Path $zip -DestinationPath $sp -Force
  Remove-Item $zip -ErrorAction SilentlyContinue
  $v = & "$py\python.exe" -m pip --version 2>$null
  if ($LASTEXITCODE -ne 0) { throw "Installation de pip impossible." }
  Note "pip prêt : $v"
}

function Do-Modules {
  & "$py\python.exe" -m pip install --no-warn-script-location --disable-pip-version-check -q -r "$AppDir\requirements.txt" 2>&1 | ForEach-Object { Note $_ }
  if ($LASTEXITCODE -ne 0) { throw "Installation des modules impossible : vérifie ta connexion Internet, puis relance l'installation." }
  Copy-Item "$AppDir\requirements.txt" "$py\.onde-req" -Force
}

function Do-Ffmpeg {
  if (Test-Path "$AppDir\windows\tools\ffmpeg.exe") { return }
  try { & cmd /c "`"$AppDir\windows\tools\telecharger-outils.bat`"" 2>&1 | ForEach-Object { Note $_ } } catch { Note "ffmpeg : $($_.Exception.Message)" }
}

try {
  Note "étape $Step"
  switch ($Step) {
    'python'  { Do-Python }
    'pip'     { Do-Pip }
    'modules' { Do-Modules }
    'ffmpeg'  { Do-Ffmpeg }
    default   { Do-Python; Do-Pip; Do-Modules; Do-Ffmpeg }
  }
  exit 0
} catch {
  Note "ERREUR : $($_.Exception.Message)"
  Write-Output $_.Exception.Message
  exit 1
}
