#!/usr/bin/env bash
# Fabrique Onde.apk (sans Android Studio ni Gradle).
# Prérequis (Debian / Ubuntu / Raspberry Pi OS) :
#   sudo apt install default-jdk-headless aapt apksigner zipalign dalvik-exchange android-sdk-platform-23
# Résultat : android/build/Onde.apk  (signé ; clé dans android/build/onde.keystore, à garder pour les mises à jour)
set -euo pipefail
cd "$(dirname "$0")"

# Bibliothèque Android 14 (API 34) : le paquet Debian ne fournit que l'API 23, on la télécharge une fois
ANDROID_JAR="${ANDROID_JAR:-build/android-34.jar}"
TARGET_SDK="${ONDE_TARGET_SDK:-35}"   # Android 15 : évite l'alerte « appli conçue pour une ancienne version »
KEYSTORE="${ONDE_KEYSTORE:-build/onde.keystore}"
KEYPASS="${ONDE_KEYPASS:-ondeapp}"
VERSION_CODE="${ONDE_VERSION_CODE:-7}"
VERSION_NAME="${ONDE_VERSION_NAME:-1.6}"

for tool in aapt javac dalvik-exchange zipalign apksigner keytool; do
  command -v "$tool" >/dev/null || { echo "Il manque : $tool  (voir l'en-tête de ce script)"; exit 1; }
done
if [ ! -f "$ANDROID_JAR" ]; then
  mkdir -p "$(dirname "$ANDROID_JAR")"
  echo "• téléchargement d'android.jar (API 34, une seule fois)"
  curl -fL -o "$ANDROID_JAR" "https://raw.githubusercontent.com/Sable/android-platforms/master/android-34/android.jar" \
    || { echo "android.jar introuvable : $ANDROID_JAR"; exit 1; }
fi

mkdir -p build
rm -rf build/gen build/classes build/*.apk build/classes.dex
mkdir -p build/gen build/classes

echo "• ressources"
aapt package -f -M AndroidManifest.xml -S res -I "$ANDROID_JAR" -J build/gen -F build/unsigned.apk \
  --min-sdk-version 21 --target-sdk-version "$TARGET_SDK" --version-code "$VERSION_CODE" --version-name "$VERSION_NAME"

echo "• compilation"
if ! javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -bootclasspath "$ANDROID_JAR" \
  -d build/classes $(find src build/gen -name '*.java') > build/javac.log 2>&1; then
  grep -v "JAVA_TOOL_OPTIONS" build/javac.log || true
  echo "La compilation a échoué"; exit 1
fi
grep -v "^Note:\|JAVA_TOOL_OPTIONS" build/javac.log || true

echo "• dex"
dalvik-exchange --dex --output=build/classes.dex build/classes

echo "• assemblage"
( cd build && aapt add -f unsigned.apk classes.dex >/dev/null )
zipalign -f -p 4 build/unsigned.apk build/aligned.apk

if [ ! -f "$KEYSTORE" ]; then
  echo "• création de la clé de signature ($KEYSTORE)"
  keytool -genkeypair -keystore "$KEYSTORE" -storepass "$KEYPASS" -keypass "$KEYPASS" -alias onde \
    -keyalg RSA -keysize 2048 -validity 36500 -dname "CN=Onde, O=Onde" 2>&1 | grep -v JAVA_TOOL_OPTIONS || true
fi
apksigner sign --ks "$KEYSTORE" --ks-pass "pass:$KEYPASS" --key-pass "pass:$KEYPASS" --ks-key-alias onde \
  --out build/Onde.apk build/aligned.apk
apksigner verify build/Onde.apk
echo "✔ build/Onde.apk ($(du -h build/Onde.apk | cut -f1))"
# copie servie par le serveur Onde aux téléphones (mise à jour automatique de l'appli)
cp build/Onde.apk Onde.apk
printf '{"versionCode": %s, "versionName": "%s"}\n' "$VERSION_CODE" "$VERSION_NAME" > Onde.apk.json
echo "✔ Onde.apk + Onde.apk.json (fournis aux téléphones par le serveur)"
