#!/usr/bin/env bash
# Fabrique Onde-Installation-x.y.z.exe (à la racine du projet) avec NSIS (makensis), sous Linux.
#   sudo apt install nsis    puis    bash windows/installer/build.sh
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' ../../onde/config.py)
python3 -c "import sys; sys.path.insert(0, '../..'); from onde import terms; open('../../CONDITIONS.txt', 'w', encoding='utf-8-sig', newline='').write(terms.text())"
rm -f ../../Onde-Installation-*.exe
makensis -V2 -DVERSION="$VERSION" onde.nsi
ls -la ../../Onde-Installation-"$VERSION".exe
