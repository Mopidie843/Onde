#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Onde — ta musique en Wi-Fi, sur ton Raspberry Pi.   Script tout-en-un.
#
#   ./start.sh              installe tout (1re fois) puis lance Onde
#   ./start.sh service      démarrage automatique à chaque allumage du Pi  ← conseillé
#   ./start.sh open         lance Onde en arrière-plan et ouvre le navigateur
#   ./start.sh kiosk        Onde en plein écran sur l'écran du Pi
#   ./start.sh server       le Pi devient un serveur : démarrage seul, onde.local, sauvegardes   [share | headless | status]
#   ./start.sh remote       Onde accessible depuis Internet / la 4G (Tailscale)   [private | public | status | off]
#   ./start.sh ia           paroles synchronisées par IA (calage sur la voix, ~1 Go à télécharger)
#   ./start.sh hotspot      le Pi diffuse son propre Wi-Fi (voiture, sans box)   [auto = mode voiture | off | status]
#   ./start.sh restart | stop | status | logs | update | uninstall
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$DIR"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
ENV_FILE="$DIR/onde.env"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/onde.service"
LOG_DIR="$HOME/.local/share/onde"
LOG="$LOG_DIR/onde.log"
USER="${USER:-$(id -un)}"

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; C=$'\e[36m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B=; G=; C=; Y=; R=; N=; fi
say()  { printf '%s\n' "${C}♪${N} $*"; }
ok()   { printf '%s\n' "${G}✔${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
die()  { printf '%s\n' "${R}✖ $*${N}" >&2; exit 1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

port() {
  local p=""
  [ -f "$ENV_FILE" ] && p="$(grep -E '^ONDE_PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d '"'"' " || true)"
  echo "${p:-8000}"
}

# ------------------------------------------------------------------ installation
install_system() {
  command -v apt-get >/dev/null || return 0
  local pkgs=()
  command -v python3 >/dev/null || pkgs+=(python3)
  python3 -c 'import ensurepip, venv' 2>/dev/null || pkgs+=(python3-venv)
  command -v mpv >/dev/null || pkgs+=(mpv)          # lecture sur les haut-parleurs du Pi
  command -v ffmpeg >/dev/null || pkgs+=(ffmpeg)    # conversion, volume égalisé, téléchargement par nom
  if [ ${#pkgs[@]} -gt 0 ]; then
    say "Installation des paquets système : ${B}${pkgs[*]}${N} (ton mot de passe peut être demandé)"
    as_root apt-get update -qq || true
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}" \
      || warn "Impossible d'installer ${pkgs[*]} — Onde fonctionnera sans (mode enceinte désactivé si mpv manque, conversion/téléchargement par nom désactivés si ffmpeg manque)."
  fi
}

install_python() {
  command -v python3 >/dev/null || die "Python 3 est introuvable (sudo apt install python3)."
  if [ ! -x "$PY" ]; then
    say "Création de l'environnement Python…"
    python3 -m venv "$VENV" || die "Il manque python3-venv : sudo apt install python3-venv"
  fi
  local want have
  want="$(sha1sum requirements.txt | cut -d' ' -f1)"
  have="$(cat "$VENV/.onde-req" 2>/dev/null || true)"
  if [ "$want" != "$have" ]; then
    say "Installation des dépendances Python (la 1re fois : 1 à 3 minutes)…"
    "$PY" -m pip install -q --disable-pip-version-check --upgrade pip wheel || return 1
    "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt || return 1
    echo "$want" > "$VENV/.onde-req"
    ok "Dépendances installées"
  fi
}

write_env() {
  [ -s "$ENV_FILE" ] && return 0
  local music="$HOME/Music"
  [ -d "$HOME/Musique" ] && music="$HOME/Musique"
  cat > "$ENV_FILE.tmp" <<EOF
# ─── Configuration d'Onde ───  après une modification :  ./start.sh restart

# Dossier de ta musique (les imports y sont rangés par Artiste/Album)
ONDE_MUSIC_DIR=$music

# Autres dossiers à scanner, séparés par « : » (ex. une clé USB)
#ONDE_EXTRA_DIRS=/media/$USER/USB/Musique

# Port web  →  http://$(hostname).local:8000
ONDE_PORT=8000

# Mot de passe (vide = accès libre pour tous les appareils de ton Wi-Fi)
ONDE_PASSWORD=

# Chercher automatiquement les pochettes manquantes sur Internet (1 = oui, 0 = non)
ONDE_AUTO_COVERS=1

# Écrire les pochettes dans les fichiers MP3 eux-mêmes (1 = oui, 0 = non)
ONDE_EMBED_COVERS=1

# Rescan automatique du dossier musique, en secondes (0 = jamais)
ONDE_SCAN_INTERVAL=300

# Taille maximale d'un fichier importé (Mo)
ONDE_MAX_UPLOAD_MB=1024

# Mode enceinte : options de mpv, par ex. pour choisir la sortie audio
# (liste des sorties :  mpv --audio-device=help)
#ONDE_MPV_ARGS=--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0

# Point d'accès Wi-Fi du mode nomade (./start.sh hotspot) : nom du réseau, mot de passe
# (8 à 63 caractères simples), interface Wi-Fi du Pi
#ONDE_HOTSPOT_SSID=Onde-Music
#ONDE_HOTSPOT_PASSWORD=ondemusic
#ONDE_HOTSPOT_IFNAME=wlan0

# Mode serveur (./start.sh server) : nom du Pi sur le réseau → http://onde.local:8000
# (par défaut « onde » si le Pi s'appelle encore raspberrypi), et dossier réseau « Musique » (1 = oui)
#ONDE_HOSTNAME=onde
#ONDE_SHARE=0

# Paroles synchronisées par IA (./start.sh ia) : modèle tiny, base (défaut), small ou medium
#ONDE_ALIGN_MODEL=base
EOF
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  mkdir -p "$music"
  ok "Configuration créée : ${B}onde.env${N} (musique : $music)"
}

desktop_shortcut() {
  local apps="$HOME/.local/share/applications" file
  mkdir -p "$apps"
  file="$apps/onde.desktop"
  cat > "$file" <<EOF
[Desktop Entry]
Type=Application
Name=Onde
Comment=Ta musique en Wi-Fi
Exec=bash "$DIR/start.sh" open
Icon=$DIR/onde/static/icons/icon-512.png
Terminal=false
Categories=AudioVideo;Audio;Player;
EOF
  chmod +x "$file"
  for desk in "$HOME/Desktop" "$HOME/Bureau"; do
    if [ -d "$desk" ] && [ ! -f "$desk/Onde.desktop" ]; then
      cp "$file" "$desk/Onde.desktop"; chmod +x "$desk/Onde.desktop"
      command -v gio >/dev/null && gio set "$desk/Onde.desktop" metadata::trusted true 2>/dev/null || true
      ok "Icône « Onde » ajoutée sur le bureau"
    fi
  done
}

setup() {
  install_system
  install_python
  write_env
  desktop_shortcut 2>/dev/null || true
  mkdir -p "$LOG_DIR"
}

# ------------------------------------------------------------------ outils
running() {
  python3 - "$(port)" <<'EOF' 2>/dev/null
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/info", timeout=2)
except urllib.error.HTTPError:
    pass  # 401 quand un mot de passe est défini : Onde répond, il tourne bien
EOF
}
service_installed() { [ -f "$UNIT" ]; }
service_active() { systemctl --user is-active --quiet onde 2>/dev/null; }

urls() {
  local p; p="$(port)"
  printf '\n  %s♪  Onde est prêt !%s  Sur ton téléphone ou ton PC (même Wi-Fi), ouvre :\n' "$B" "$N"
  for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in *:*) ;; *) printf '       %shttp://%s:%s%s\n' "$G" "$ip" "$p" "$N" ;; esac
  done
  printf '       %shttp://%s.local:%s%s\n\n' "$G" "$(hostname)" "$p" "$N"
}

wait_up() {
  for _ in $(seq 1 40); do running && return 0; sleep 0.5; done
  return 1
}

start_background() {
  running && return 0
  if service_installed; then
    systemctl --user start onde
  else
    say "Démarrage d'Onde en arrière-plan (journal : $LOG)"
    # le journal ne grossit pas sans fin (carte SD) : au-delà de 5 Mo, on repart d'un fichier neuf
    if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
    nohup "$PY" -m onde >> "$LOG" 2>&1 &
  fi
  wait_up || die "Onde ne répond pas — regarde le journal : ./start.sh logs"
}

browser() {
  local url b
  url="http://localhost:$(port)"
  for b in chromium-browser chromium google-chrome firefox; do
    if command -v "$b" >/dev/null; then
      if [ "${1:-}" = kiosk ]; then
        nohup "$b" --kiosk --noerrdialogs --disable-infobars --no-first-run \
          --autoplay-policy=no-user-gesture-required --app="$url" >/dev/null 2>&1 &
      else
        nohup "$b" --app="$url" >/dev/null 2>&1 &
      fi
      return 0
    fi
  done
  command -v xdg-open >/dev/null && { xdg-open "$url" >/dev/null 2>&1 & return 0; }
  warn "Aucun navigateur trouvé : ouvre $url"
}

install_service() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Onde — serveur musical local
After=network-online.target sound.target pipewire.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m onde
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  # arrête une éventuelle instance lancée à la main
  pkill -f "^$VENV/bin/python -m onde$" 2>/dev/null || true
  systemctl --user enable --now onde
  # « linger » : le service démarre au boot, même sans ouvrir de session
  if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    as_root loginctl enable-linger "$USER" || warn "Active le démarrage sans session : sudo loginctl enable-linger $USER"
  fi
  wait_up || warn "Le service met du temps à démarrer : ./start.sh logs"
  ok "Onde démarrera automatiquement à chaque allumage du Pi"
}

# ------------------------------------------------------------------ point d'accès Wi-Fi (mode nomade)
# Le Pi diffuse son propre Wi-Fi : Onde marche en voiture, en vacances, sans box Internet.
# Profil NetworkManager dédié « Onde-Hotspot » (le profil générique « Hotspot » n'est pas touché) :
#   - adresse fixe 10.42.0.1 : l'adresse à ouvrir est connue d'avance, même pour les QR codes ;
#   - WPA2 seul, sans PMF : la puce Wi-Fi des Raspberry Pi gère mal le PMF en point d'accès, ce
#     qui empêche souvent les iPhone de s'y connecter ;
#   - 2,4 GHz : le plus compatible (vieux téléphones, autoradios) ;
#   - priorité -100 : en mode « auto », il ne passe qu'après tous tes réseaux Wi-Fi connus.
HOTSPOT_CON="Onde-Hotspot"
HOTSPOT_IP="10.42.0.1"
NM_SUDO=0
SUDO_N=""; [ -t 0 ] || SUDO_N="-n"  # sans terminal (minuterie) : sudo ne doit jamais attendre un mot de passe
HS_STATE="$LOG_DIR/hotspot.state"     # réglages du point d'accès actuellement appliqués
HS_TIMER="$UNIT_DIR/onde-hotspot.timer"
HS_TIMER_SVC="$UNIT_DIR/onde-hotspot.service"

envval() {  # envval CLÉ DÉFAUT : mêmes règles qu'onde/config.py (environnement, puis onde.env), sinon DÉFAUT
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
    v="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r' \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"*//' -e 's/"*$//' -e "s/^'*//" -e "s/'*\$//" \
            -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' || true)"
  fi
  printf '%s' "${v:-$2}"
}

nm() {  # nmcli, avec sudo seulement si NetworkManager refuse sans (session SSH, droits polkit…)
  local out rc=0
  if [ "$NM_SUDO" = 1 ]; then LC_ALL=C sudo $SUDO_N nmcli "$@"; return; fi
  out="$(LC_ALL=C nmcli "$@" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then
    [ -z "$out" ] || printf '%s\n' "$out"
    return 0
  fi
  if [ "$(id -u)" -ne 0 ] && printf '%s' "$out" | grep -qiE 'authori[sz]|autoris|privilege|permission|polkit'; then
    NM_SUDO=1
    LC_ALL=C sudo $SUDO_N nmcli "$@"
    return
  fi
  printf '%s\n' "$out" >&2
  return "$rc"
}

nm_has() { LC_ALL=C nmcli -t -f NAME connection show ${2:+--active} 2>/dev/null | grep -Fqx "$1"; }

hotspot_settings() {
  HS_IF="$(envval ONDE_HOTSPOT_IFNAME wlan0)"
  HS_SSID="$(envval ONDE_HOTSPOT_SSID Onde-Music)"
  HS_PASS="$(envval ONDE_HOTSPOT_PASSWORD ondemusic)"
}

hotspot_check() {
  command -v nmcli >/dev/null 2>&1 || die "Le mode point d'accès a besoin de NetworkManager (nmcli), installé d'office depuis Raspberry Pi OS « Bookworm » (fin 2023). Sur un système plus ancien : sudo raspi-config → Advanced Options → Network Config → NetworkManager, puis redémarre le Pi."
  LC_ALL=C nmcli -t -f RUNNING general >/dev/null 2>&1 || die "NetworkManager ne répond pas : sudo systemctl enable --now NetworkManager"
  hotspot_settings
  local n
  n="$(printf '%s' "$HS_SSID" | wc -c)"
  { [ "$n" -ge 1 ] && [ "$n" -le 32 ]; } || die "Nom du Wi-Fi invalide (1 à 32 caractères) : ONDE_HOTSPOT_SSID dans onde.env"
  if printf '%s' "$HS_PASS" | LC_ALL=C grep -q '[^ -~]'; then
    die "Mot de passe du Wi-Fi : uniquement des caractères simples, sans accents ni emoji (ONDE_HOTSPOT_PASSWORD dans onde.env)"
  fi
  { [ "${#HS_PASS}" -ge 8 ] && [ "${#HS_PASS}" -le 63 ]; } || die "Mot de passe du Wi-Fi invalide : il faut de 8 à 63 caractères (ONDE_HOTSPOT_PASSWORD dans onde.env)"
  LC_ALL=C nmcli -t -f DEVICE,TYPE device status 2>/dev/null | grep -Fqx "$HS_IF:wifi" \
    || die "Interface Wi-Fi « $HS_IF » introuvable (voir : nmcli device status). Si ton Wi-Fi porte un autre nom, mets-le dans ONDE_HOTSPOT_IFNAME (onde.env)."
}

hotspot_profile() {  # hotspot_profile yes|no|keep : crée ou met à jour le profil (« keep » garde le mode auto)
  local auto="$1" exists=0
  nm_has "$HOTSPOT_CON" && exists=1
  if [ "$auto" = keep ]; then
    auto=no
    if [ "$exists" = 1 ] && [ "$(LC_ALL=C nmcli -g connection.autoconnect connection show "$HOTSPOT_CON" 2>/dev/null)" = yes ]; then
      auto=yes
    fi
  fi
  local opts=(connection.autoconnect "$auto" connection.autoconnect-priority -100
    802-11-wireless.mode ap 802-11-wireless.band bg
    ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24" ipv6.method ignore
    802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.psk "$HS_PASS"
    802-11-wireless-security.proto rsn 802-11-wireless-security.pairwise ccmp
    802-11-wireless-security.group ccmp 802-11-wireless-security.pmf disable)
  if [ "$exists" = 1 ]; then
    nm connection modify "$HOTSPOT_CON" connection.interface-name "$HS_IF" 802-11-wireless.ssid "$HS_SSID" "${opts[@]}" >/dev/null \
      || die "Impossible de mettre à jour le point d'accès (nmcli connection modify)."
  else
    nm connection add type wifi con-name "$HOTSPOT_CON" ifname "$HS_IF" ssid "$HS_SSID" "${opts[@]}" >/dev/null \
      || die "Impossible de créer le point d'accès (nmcli connection add)."
  fi
}

hotspot_ip() {
  local ip
  ip="$(LC_ALL=C nmcli -g IP4.ADDRESS device show "$HS_IF" 2>/dev/null | head -1 | cut -d/ -f1 | tr -d ' ')"
  printf '%s' "${ip:-$HOTSPOT_IP}"
}

ssh_via() {  # vrai si la session SSH en cours passe par l'interface $1 (elle sera coupée)
  [ -n "${SSH_CONNECTION:-}" ] && command -v ip >/dev/null 2>&1 || return 1
  ip route get "${SSH_CONNECTION%% *}" 2>/dev/null | grep -q " dev $1 "
}

detached() {  # lance une commande nmcli qui survit à la coupure de la session SSH
  local cmdline args
  args="$(printf '%q ' "$@")"
  cmdline="sleep 3; LC_ALL=C nmcli $args || LC_ALL=C sudo -n nmcli $args"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup bash -c "$cmdline" >/dev/null 2>&1 < /dev/null &
  else
    nohup bash -c "$cmdline" >/dev/null 2>&1 < /dev/null &
  fi
}

hotspot_message() {
  printf '\n  %s♪  %s%s\n\n' "$B" "Point d'accès Wi-Fi « $HS_SSID » actif." "$N"
  ONDE_HOTSPOT_SSID="$HS_SSID" ONDE_HOTSPOT_PASSWORD="$HS_PASS" "$PY" -m onde.hotspot --ip "$1" 2>/dev/null \
    || printf '     Connecte ton téléphone au Wi-Fi « %s » (mot de passe : %s), puis ouvre http://%s:%s\n' \
         "$HS_SSID" "$HS_PASS" "$1" "$(port)"
  if [ "$(LC_ALL=C nmcli -g connection.autoconnect connection show "$HOTSPOT_CON" 2>/dev/null)" = yes ]; then
    printf '\n     Mode automatique : le Pi revient tout seul sur ton Wi-Fi habituel dès qu'"'"'il le retrouve.\n'
  else
    printf '\n     Il reste allumé jusqu'"'"'au prochain redémarrage du Pi.\n'
  fi
  printf '     Pour revenir à ton Wi-Fi habituel :  %s./start.sh hotspot off%s\n\n' "$B" "$N"
}

hotspot_fp() { printf '%s\n' v1 "$HS_IF" "$HS_SSID" "$HS_PASS" | sha1sum | cut -d' ' -f1; }
hotspot_mark() { mkdir -p "$LOG_DIR"; (umask 077; printf '%s\n' "$(hotspot_fp)" > "$HS_STATE"); }

hotspot_on() {
  hotspot_check
  if nm_has "$HOTSPOT_CON" active && [ "$(cat "$HS_STATE" 2>/dev/null || true)" = "$(hotspot_fp)" ]; then
    ok "Le point d'accès « $HS_SSID » est déjà allumé."
    start_background
    hotspot_message "$(hotspot_ip)"
    return 0
  fi
  hotspot_profile keep
  nm radio wifi on >/dev/null 2>&1 || true
  if ssh_via "$HS_IF"; then
    warn "Tu es connecté en SSH par le Wi-Fi du Pi : cette session va se couper quand il passera en point d'accès."
    start_background
    hotspot_message "$HOTSPOT_IP"
    say "Le Pi bascule sur son propre Wi-Fi dans 3 secondes…"
    hotspot_mark
    detached -w 30 connection up "$HOTSPOT_CON" ifname "$HS_IF"
    exit 0
  fi
  say "Création du point d'accès Wi-Fi « $HS_SSID »…"
  if ! nm -w 30 connection up "$HOTSPOT_CON" ifname "$HS_IF" >/dev/null; then
    die "Le point d'accès n'a pas démarré. Vérifie que le pays du Wi-Fi est réglé (sudo raspi-config → Localisation Options → WLAN Country) et que le Wi-Fi n'est pas bloqué (rfkill list), puis réessaie."
  fi
  hotspot_mark
  start_background
  hotspot_message "$(hotspot_ip)"
}

hotspot_timer_install() {  # vérifie toutes les 5 min : retour au Wi-Fi habituel, ou point d'accès si plus rien
  mkdir -p "$UNIT_DIR"
  cat > "$HS_TIMER_SVC" <<EOF
[Unit]
Description=Onde — point d'accès Wi-Fi automatique (mode voiture)

[Service]
Type=oneshot
ExecStart=/bin/bash "$DIR/start.sh" hotspot check
EOF
  cat > "$HS_TIMER" <<EOF
[Unit]
Description=Onde — point d'accès Wi-Fi automatique (mode voiture)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now onde-hotspot.timer >/dev/null 2>&1 \
    || warn "Vérification automatique non activée (systemctl --user enable --now onde-hotspot.timer)."
}

hotspot_timer_remove() {
  [ -f "$HS_TIMER" ] || return 0
  systemctl --user disable --now onde-hotspot.timer >/dev/null 2>&1 || true
  rm -f "$HS_TIMER" "$HS_TIMER_SVC"
  systemctl --user daemon-reload 2>/dev/null || true
}

hotspot_auto() {
  hotspot_check
  hotspot_profile yes
  # sans démarrage automatique, le mode voiture ne servirait à rien : personne pour taper une commande
  service_installed || install_service
  hotspot_timer_install
  ok "Mode automatique activé : sans Wi-Fi connu à portée (en voiture…), le Pi diffusera « $HS_SSID » tout seul, et reviendra sur ton Wi-Fi habituel dès qu'il le retrouvera."
  if nm_has "$HOTSPOT_CON" active; then
    hotspot_on
    return
  fi
  case "$(LC_ALL=C nmcli -g GENERAL.STATE device show "$HS_IF" 2>/dev/null | head -1)" in
    100*) say "Le Pi est connecté à un Wi-Fi en ce moment : il y reste." ;;
    *) hotspot_on ;;  # aucun réseau en ce moment : on l'allume tout de suite
  esac
}

wifi_known_visible() {  # nom du réseau Wi-Fi enregistré (hors point d'accès) à portée, le prioritaire d'abord
  local visible line name best="" best_prio=-1000 prio ssid
  visible="$(LC_ALL=C nmcli -t -f SSID device wifi list ifname "$HS_IF" --rescan yes 2>/dev/null | sed 's/\\:/:/g')" || return 1
  while IFS= read -r line; do
    [ "${line##*:}" = 802-11-wireless ] || continue
    name="${line%:*}"; name="${name//\\:/:}"
    [ "$name" = "$HOTSPOT_CON" ] && continue
    [ "$(LC_ALL=C nmcli -g 802-11-wireless.mode connection show "$name" 2>/dev/null)" = ap ] && continue
    [ "$(LC_ALL=C nmcli -g connection.autoconnect connection show "$name" 2>/dev/null)" = yes ] || continue
    ssid="$(LC_ALL=C nmcli -g 802-11-wireless.ssid connection show "$name" 2>/dev/null)"
    printf '%s\n' "$visible" | grep -Fqx -- "$ssid" || continue
    prio="$(LC_ALL=C nmcli -g connection.autoconnect-priority connection show "$name" 2>/dev/null)"
    [ "${prio:-0}" -gt "$best_prio" ] 2>/dev/null && { best="$name"; best_prio="${prio:-0}"; }
  done < <(LC_ALL=C nmcli -t -f NAME,TYPE connection show 2>/dev/null)
  [ -n "$best" ] && printf '%s' "$best"
}

hotspot_clients() {  # vrai si un appareil est connecté au point d'accès (sans « iw » : on suppose que oui)
  command -v iw >/dev/null 2>&1 || return 0
  [ -n "$(iw dev "$HS_IF" station dump 2>/dev/null)" ]
}

hotspot_tick() {  # minuterie du mode automatique (toutes les 5 min) : ne coupe jamais quelqu'un qui écoute
  command -v nmcli >/dev/null 2>&1 || return 0
  hotspot_settings
  nm_has "$HOTSPOT_CON" || return 0
  [ "$(LC_ALL=C nmcli -g connection.autoconnect connection show "$HOTSPOT_CON" 2>/dev/null)" = yes ] || return 0
  if ! nm_has "$HOTSPOT_CON" active; then
    case "$(LC_ALL=C nmcli -g GENERAL.STATE device show "$HS_IF" 2>/dev/null | head -1)" in
      30*) nm -w 30 connection up "$HOTSPOT_CON" ifname "$HS_IF" >/dev/null 2>&1 && hotspot_mark ;;  # plus aucun réseau
    esac
    return 0
  fi
  hotspot_clients && return 0
  # personne n'est connecté : le Pi ne peut pas chercher d'autres réseaux en diffusant le sien, on coupe
  # quelques secondes, on rejoint un Wi-Fi connu s'il est là, sinon on rallume le point d'accès
  LC_ALL=C nmcli -t -f TYPE connection show 2>/dev/null | grep -c '^802-11-wireless$' | grep -qx 1 && return 0  # aucun autre Wi-Fi enregistré
  nm device disconnect "$HS_IF" >/dev/null 2>&1 || return 0
  local home
  home="$(wifi_known_visible || true)"
  if [ -n "$home" ] && nm -w 45 connection up "$home" >/dev/null 2>&1; then
    return 0
  fi
  nm -w 30 connection up "$HOTSPOT_CON" ifname "$HS_IF" >/dev/null 2>&1 || nm device connect "$HS_IF" >/dev/null 2>&1 || true
}

hotspot_off() {
  command -v nmcli >/dev/null 2>&1 || die "NetworkManager (nmcli) est introuvable."
  hotspot_settings
  hotspot_timer_remove
  rm -f "$HS_STATE"
  if ! nm_has "$HOTSPOT_CON"; then
    ok "Le point d'accès d'Onde n'est pas configuré : rien à faire."
    return 0
  fi
  if nm_has "$HOTSPOT_CON" active && ssh_via "$HS_IF"; then
    warn "Ta session SSH passe par le point d'accès : elle va se couper. Le Pi rejoindra ton Wi-Fi habituel ; reconnecte-toi à son adresse sur ce réseau."
    detached connection delete "$HOTSPOT_CON"
    exit 0
  fi
  nm connection delete "$HOTSPOT_CON" >/dev/null || die "Impossible de supprimer le point d'accès (nmcli connection delete)."
  nm device connect "$HS_IF" >/dev/null 2>&1 || true  # rejoint le meilleur Wi-Fi connu à portée
  ok "Point d'accès arrêté (mode automatique compris) : le Pi revient sur ton Wi-Fi habituel."
}

hotspot_status() {
  command -v nmcli >/dev/null 2>&1 || die "NetworkManager (nmcli) est introuvable."
  hotspot_settings
  if ! nm_has "$HOTSPOT_CON"; then
    say "Point d'accès d'Onde : pas configuré (./start.sh hotspot pour l'allumer)."
    return 0
  fi
  local auto
  auto="$(LC_ALL=C nmcli -g connection.autoconnect connection show "$HOTSPOT_CON" 2>/dev/null || true)"
  if nm_has "$HOTSPOT_CON" active; then
    ok "Point d'accès « $HS_SSID » allumé."
    [ "$auto" = yes ] && say "Mode automatique : activé."
    running || warn "Onde n'est pas lancé : ./start.sh open"
    hotspot_message "$(hotspot_ip)"
  else
    say "Point d'accès « $HS_SSID » configuré, mais éteint."
    [ "$auto" = yes ] && say "Mode automatique activé : il s'allumera tout seul sans Wi-Fi connu à portée."
  fi
  return 0
}

hotspot_setup() {
  if [ ! -x "$PY" ]; then setup; return; fi  # 1re installation : il faut Internet, comme d'habitude
  write_env
  mkdir -p "$LOG_DIR"
  # Onde mis à jour mais pas d'Internet (en voiture…) : on démarre avec les dépendances déjà là
  install_python || warn "Dépendances non mises à jour (pas d'Internet ?) : Onde démarre avec celles déjà installées."
}

# ------------------------------------------------------------------ mode serveur
# Le Pi devient un vrai petit serveur : il démarre seul, sans écran ni clavier, s'annonce sur le réseau
# (http://onde.local), se répare s'il plante, sauvegarde tes playlists chaque nuit, et peut partager
# le dossier de musique pour y glisser des fichiers depuis un PC (dossier réseau « Musique »).
ETC="${ONDE_ETC:-/etc}"          # (surchargeable pour les tests)
BK_TIMER="$UNIT_DIR/onde-backup.timer"
BK_SVC="$UNIT_DIR/onde-backup.service"
SMB_BEGIN="# >>> Onde >>>"
SMB_END="# <<< Onde <<<"

have() { command -v "$1" >/dev/null 2>&1 || [ -x "/usr/sbin/$1" ]; }
write_root() {  # write_root FICHIER [MODE] : le contenu arrive sur l'entrée standard
  local tmp; tmp="$(mktemp)"; cat > "$tmp"
  as_root mkdir -p "$(dirname "$1")"
  as_root install -m "${2:-644}" "$tmp" "$1"; rm -f "$tmp"
}
apt_need() {  # apt_need COMMANDE PAQUET : installe le paquet si la commande manque ; faux si impossible
  have "$1" && return 0
  command -v apt-get >/dev/null || { warn "$2 manque et apt-get est introuvable : installe-le à la main."; return 1; }
  say "Installation de ${B}$2${N}"
  as_root apt-get update -qq || true
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$2" >/dev/null \
    || { warn "Impossible d'installer $2 (pas d'Internet ?)."; return 1; }
}

server_hostname() {  # nom du Pi sur le réseau : onde.local (seulement si c'est encore le nom d'usine, ou si demandé)
  local want cur; want="$(envval ONDE_HOSTNAME "")"; cur="$(hostname)"
  if [ -z "$want" ] && [ "$cur" = raspberrypi ]; then want=onde; fi
  if [ -z "$want" ] || [ "$want" = "$cur" ]; then return 0; fi
  if ! printf '%s' "$want" | grep -Eq '^[a-z0-9][a-z0-9-]{0,30}$'; then
    warn "ONDE_HOSTNAME « $want » invalide (lettres minuscules, chiffres, tirets) : nom du Pi inchangé."; return 0
  fi
  as_root hostnamectl set-hostname "$want" || { warn "Nom du Pi inchangé."; return 0; }
  if grep -Eq '^127\.0\.1\.1[[:space:]]' "$ETC/hosts" 2>/dev/null; then
    as_root sed -i -E "s/^(127\.0\.1\.1[[:space:]]+).*/\1$want/" "$ETC/hosts"
  else
    printf '127.0.1.1\t%s\n' "$want" | as_root tee -a "$ETC/hosts" >/dev/null
  fi
  ok "Le Pi s'appelle maintenant ${B}$want${N} → http://$want.local:$(port)"
}

server_announce() {  # avahi : « nom.local » fonctionne, et Onde apparaît dans la liste des appareils du réseau
  apt_need avahi-daemon avahi-daemon || return 0
  write_root "$ETC/avahi/services/onde.service" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Onde sur %h</name>
  <service>
    <type>_http._tcp</type>
    <port>$(port)</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
EOF
  as_root systemctl enable --now avahi-daemon >/dev/null 2>&1 || warn "avahi-daemon non démarré."
  as_root systemctl restart avahi-daemon >/dev/null 2>&1 || true
}

server_ssh() {  # sans écran, SSH est la seule façon d'administrer le Pi
  if have raspi-config; then as_root raspi-config nonint do_ssh 0 >/dev/null 2>&1 || true
  elif systemctl cat ssh >/dev/null 2>&1; then as_root systemctl enable --now ssh >/dev/null 2>&1 || true
  fi
}

server_firewall() {  # pare-feu ufw : seulement s'il est déjà actif (sinon rien n'est bloqué)
  have ufw || return 0
  as_root ufw status 2>/dev/null | grep -q '^Status: active' || return 0
  as_root ufw allow "$(port)/tcp" >/dev/null || true
  as_root ufw allow 5353/udp >/dev/null || true
  ok "Pare-feu : port $(port) ouvert"
}

server_backup_run() {  # copie cohérente de la base (playlists, favoris, historique), 7 sauvegardes gardées
  local data db out
  data="$(envval ONDE_DATA_DIR "$LOG_DIR")"; db="$data/onde.db"; BACKUP_DIR="$data/backups"
  [ -f "$db" ] || { warn "Pas encore de base à sauvegarder."; return 0; }
  mkdir -p "$BACKUP_DIR"
  out="$BACKUP_DIR/onde-$(date +%Y%m%d-%H%M%S).db"
  python3 - "$db" "$out" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else 1)
EOF
  # (le « || » protège de set -e : ls échoue sans sauvegarde, ce qui n'est pas une erreur)
  ls -1t "$BACKUP_DIR"/onde-*.db 2>/dev/null | tail -n +8 | while read -r old; do rm -f "$old"; done || true
  ok "Sauvegarde : $out"
}

server_backup_timer() {  # chaque nuit vers 4 h (ou au prochain allumage si le Pi était éteint)
  mkdir -p "$UNIT_DIR"
  cat > "$BK_SVC" <<EOF
[Unit]
Description=Onde — sauvegarde de la base

[Service]
Type=oneshot
ExecStart=/bin/bash "$DIR/start.sh" backup
EOF
  cat > "$BK_TIMER" <<EOF
[Unit]
Description=Onde — sauvegarde quotidienne

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now onde-backup.timer >/dev/null 2>&1 \
    || warn "Sauvegarde automatique non activée (systemctl --user enable --now onde-backup.timer)."
}

smb_strip() {  # smb_strip FICHIER : le fichier sans le bloc d'Onde
  awk -v b="$SMB_BEGIN" -v e="$SMB_END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$1"
}

server_share_on() {  # dossier réseau « Musique » (Windows : \\onde\Musique ; Mac/Linux : smb://onde/Musique)
  apt_need smbd samba || return 1
  local music conf pass tmp
  music="$(envval ONDE_MUSIC_DIR "$HOME/Music")"; mkdir -p "$music"
  conf="$ETC/samba/smb.conf"; pass="$(envval ONDE_PASSWORD "")"
  tmp="$(mktemp)"
  { [ -f "$conf" ] && smb_strip "$conf" || printf '[global]\n   map to guest = Bad User\n'
    printf '%s\n[Musique]\n   comment = Musique d'"'"'Onde\n   path = %s\n   browseable = yes\n   read only = no\n   create mask = 0664\n   directory mask = 0775\n   force user = %s\n' "$SMB_BEGIN" "$music" "$USER"
    if [ -n "$pass" ]; then printf '   guest ok = no\n   valid users = %s\n' "$USER"; else printf '   guest ok = yes\n'; fi
    printf '%s\n' "$SMB_END"
  } > "$tmp"
  if have testparm && ! testparm -s "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; warn "Configuration Samba invalide : dossier réseau non activé."; return 1
  fi
  as_root mkdir -p "$(dirname "$conf")"
  as_root install -m 644 "$tmp" "$conf"; rm -f "$tmp"
  if [ -n "$pass" ]; then  # même mot de passe qu'Onde, pour ne pas ouvrir une porte dérobée
    printf '%s\n%s\n' "$pass" "$pass" | as_root smbpasswd -a -s "$USER" >/dev/null 2>&1 \
      || warn "Mot de passe Samba non défini : sudo smbpasswd -a $USER"
  fi
  as_root systemctl enable --now smbd >/dev/null 2>&1 || warn "smbd non démarré."
  as_root systemctl restart smbd >/dev/null 2>&1 || true
  as_root ufw allow Samba >/dev/null 2>&1 || true
  ok "Dossier réseau ${B}Musique${N} : \\\\$(hostname)\\Musique   (Mac/Linux : smb://$(hostname).local/Musique)"
  if [ -n "$pass" ]; then say "   identifiant : $USER   mot de passe : celui d'Onde"; else say "   sans mot de passe (comme Onde) — mets ONDE_PASSWORD pour le protéger"; fi
  say "   Les fichiers déposés apparaissent dans Onde en moins de 5 minutes."
}

server_share_off() {
  local conf="$ETC/samba/smb.conf" tmp
  [ -f "$conf" ] && grep -Fqx "$SMB_BEGIN" "$conf" || return 0
  tmp="$(mktemp)"; smb_strip "$conf" > "$tmp"
  as_root install -m 644 "$tmp" "$conf"; rm -f "$tmp"
  as_root systemctl restart smbd >/dev/null 2>&1 || true
  ok "Dossier réseau retiré"
}

server_setup() {
  say "Le Pi devient un serveur Onde…"
  setup
  install_service
  server_hostname
  server_announce
  server_ssh
  server_firewall
  server_backup_timer
  case "$(envval ONDE_SHARE 0)" in 1|yes|true|on) server_share_on || true ;; esac
  ok "Serveur prêt : il redémarre tout seul au boot, sans écran ni session ouverte."
  urls
  say "Dossier réseau pour déposer ta musique : ${B}./start.sh server share${N}"
  say "Sans écran (plus rapide, moins de mémoire) :  ${B}./start.sh server headless${N}   (puis redémarre le Pi)"
}

server_status() {
  local t='non' b='non' h; h="$(hostname)"; BACKUP_DIR="$(envval ONDE_DATA_DIR "$LOG_DIR")/backups"
  running && ok "Onde répond sur le port $(port)" || warn "Onde ne répond pas (./start.sh logs)"
  service_installed && { systemctl --user is-enabled --quiet onde 2>/dev/null && ok "Démarrage automatique : oui" || warn "Service installé mais pas activé"; } \
    || warn "Démarrage automatique : non  (./start.sh server)"
  loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes' && ok "Démarre sans ouvrir de session : oui" || warn "Démarrage sans session : non"
  systemctl is-active --quiet avahi-daemon 2>/dev/null && ok "Nom réseau : $h.local" || warn "avahi absent : $h.local ne marchera pas partout"
  systemctl is-active --quiet ssh 2>/dev/null && ok "SSH : actif" || warn "SSH : inactif"
  [ -f "$BK_TIMER" ] && b='oui'
  ok "Sauvegarde quotidienne : $b ($(ls -1 "$BACKUP_DIR"/onde-*.db 2>/dev/null | wc -l) copie(s) dans $BACKUP_DIR)"
  [ -f "$ETC/samba/smb.conf" ] && grep -Fqx "$SMB_BEGIN" "$ETC/samba/smb.conf" && t='oui'
  ok "Dossier réseau « Musique » : $t"
  ok "Au démarrage : $(systemctl get-default 2>/dev/null || echo '?')"
}

# ------------------------------------------------------------------ accès depuis Internet (Tailscale)
# Aucun port à ouvrir sur la box. Deux modes :
#   privé   seuls tes appareils (appli Tailscale, même compte) joignent Onde — le plus sûr ;
#   public  (Funnel) toute personne qui connaît l'adresse voit la page de connexion — sans appli sur le
#           téléphone ; le mot de passe d'Onde (8 caractères au moins) est alors obligatoire.
remote_install() {
  have tailscale && return 0
  apt_need curl curl || die "curl est nécessaire pour installer Tailscale."
  say "Installation de Tailscale (script officiel : tailscale.com/install.sh)"
  curl -fsSL https://tailscale.com/install.sh | as_root sh || die "Installation de Tailscale impossible (pas d'Internet ?)."
  have tailscale || die "Tailscale ne s'est pas installé correctement."
}

remote_up() {
  if tailscale status --json 2>/dev/null | grep -Eq '"BackendState": *"Running"'; then return 0; fi
  say "Connexion à ton compte Tailscale : ${B}ouvre le lien qui va s'afficher${N} (téléphone ou PC), connecte-toi, puis reviens ici."
  as_root tailscale up --hostname="$(hostname)" --accept-dns=false || die "Connexion à Tailscale impossible."
  as_root tailscale set --operator="$USER" >/dev/null 2>&1 || true  # ensuite, plus besoin de sudo pour consulter l'état
}

remote_target() {  # adresse locale d'Onde que Tailscale doit relayer
  case "$(envval ONDE_HTTPS 0)" in 1|yes|true|on) printf 'https+insecure://127.0.0.1:%s' "$(port)" ;; *) printf 'http://127.0.0.1:%s' "$(port)" ;; esac
}

remote_setup() {  # remote_setup private|public
  local mode="$1" pass; pass="$(envval ONDE_PASSWORD "")"
  if [ "$mode" = public ]; then
    if [ "${#pass}" -lt 8 ]; then
      die "Mode public : un mot de passe d'au moins 8 caractères est obligatoire. Mets ONDE_PASSWORD=... dans onde.env (ou dans les Réglages d'Onde), puis ./start.sh restart et relance cette commande."
    fi
    warn "Mode public : toute personne qui connaît l'adresse peut atteindre la page de connexion. Choisis un mot de passe long et unique."
  elif [ -z "$pass" ]; then
    warn "Aucun mot de passe sur Onde : tout appareil de ton compte Tailscale y accède librement. Conseillé : en mettre un (Réglages)."
  fi
  remote_install
  remote_up
  as_root tailscale serve reset >/dev/null 2>&1 || true
  if [ "$mode" = public ]; then
    as_root tailscale funnel --bg "$(remote_target)" || die "Funnel refusé. Active « HTTPS » et « Funnel » dans la console Tailscale (login.tailscale.com/admin/dns puis /admin/acls), puis réessaie."
  else
    as_root tailscale serve --bg "$(remote_target)" || die "Tailscale Serve refusé. Active « HTTPS Certificates » dans login.tailscale.com/admin/dns, puis réessaie."
  fi
  sleep 1
  remote_status
  if [ "$mode" = private ]; then
    say "Sur ton téléphone : installe l'appli ${B}Tailscale${N}, connecte-toi au même compte, puis ouvre l'adresse ci-dessus."
  else
    say "Ouvre cette adresse depuis n'importe où (4G, autre Wi-Fi) : elle demande le mot de passe d'Onde."
  fi
  say "Dans l'appli Onde (Android), l'adresse à distance est retenue toute seule dès que tu t'y connectes une fois depuis la maison."
}

remote_status() {
  if [ -x "$PY" ]; then "$PY" -m onde.remote || true; else warn "Onde n'est pas encore installé : ./start.sh"; fi
}

remote_off() {
  have tailscale || { ok "Rien à retirer : Tailscale n'est pas installé."; return 0; }
  as_root tailscale serve reset >/dev/null 2>&1 || true
  ok "Partage retiré : plus d'adresse https ni d'accès public. Tailscale reste connecté (tes appareils Tailscale joignent encore le Pi par son adresse 100.x) ; ./start.sh remote pour repartir."
}

# ------------------------------------------------------------------ commandes
cmd="${1:-run}"
cmd="${cmd#--}"
case "$cmd" in
  run|start)
    setup
    if running; then
      ok "Onde tourne déjà$(service_active && echo ' (service)')."
      urls
      exit 0
    fi
    say "Arrêt : Ctrl+C.   Astuce : ${B}./start.sh service${N} pour un démarrage automatique au boot."
    exec "$PY" -m onde
    ;;
  service|install)
    setup
    install_service
    urls
    ;;
  open)
    setup
    start_background
    browser
    ;;
  kiosk)
    setup
    start_background
    browser kiosk
    if [ "${2:-}" = "--boot" ]; then
      mkdir -p "$HOME/.config/autostart"
      sed -e 's/^Name=Onde$/Name=Onde (plein écran)/' -e "s|start.sh\" open|start.sh\" kiosk|" \
        "$HOME/.local/share/applications/onde.desktop" > "$HOME/.config/autostart/onde-kiosk.desktop"
      ok "Onde s'ouvrira en plein écran à chaque démarrage du bureau"
    fi
    ;;
  hotspot|ap)
    case "${2:-on}" in
      on|start) hotspot_setup; hotspot_on ;;
      auto)     hotspot_setup; hotspot_auto ;;
      off|stop) hotspot_off ;;
      status)   hotspot_status ;;
      check)    hotspot_tick ;;  # (minuterie du mode automatique)
      *) die "Usage : ./start.sh hotspot [on|auto|off|status]" ;;
    esac
    ;;
  server|serveur)
    case "${2:-on}" in
      on|start)  server_setup ;;
      share)     setup; server_share_on ;;
      unshare)   server_share_off ;;
      headless)  as_root systemctl set-default multi-user.target && ok "Le Pi démarrera sans bureau (après redémarrage). Onde n'en a pas besoin." ;;
      desktop)   as_root systemctl set-default graphical.target && ok "Le Pi démarrera avec le bureau (après redémarrage)." ;;
      status)    server_status ;;
      *) die "Usage : ./start.sh server [on|share|unshare|headless|desktop|status]" ;;
    esac
    ;;
  backup)
    server_backup_run
    ;;
  remote|distance)
    case "${2:-private}" in
      private|prive|privé|on) remote_setup private ;;
      public|funnel)          remote_setup public ;;
      status)                 remote_status ;;
      off|stop)               remote_off ;;
      *) die "Usage : ./start.sh remote [private|public|status|off]" ;;
    esac
    ;;
  ia|ai|align)
    setup
    "$PY" -m onde.align install
    if service_active; then systemctl --user restart onde && ok "Onde redémarré"; fi
    ;;
  restart)
    if service_active; then
      systemctl --user restart onde
    else
      pkill -f "^$VENV/bin/python -m onde$" 2>/dev/null || true
      for _ in $(seq 1 20); do running || break; sleep 0.3; done
      start_background
    fi
    wait_up && ok "Onde a redémarré" && urls
    ;;
  stop)
    if service_active; then systemctl --user stop onde; fi
    pkill -f "^$VENV/bin/python -m onde$" 2>/dev/null || true
    ok "Onde est arrêté"
    ;;
  status)
    if running; then ok "Onde tourne"; urls; else warn "Onde n'est pas lancé"; fi
    service_installed && systemctl --user --no-pager status onde | head -5 || true
    ;;
  logs)
    if service_installed; then journalctl --user -u onde -f -n 100; else tail -n 100 -f "$LOG"; fi
    ;;
  update)
    if [ -d .git ]; then git pull --ff-only; fi
    setup
    service_active && systemctl --user restart onde && ok "Onde mis à jour et redémarré"
    ;;
  uninstall)
    systemctl --user disable --now onde onde-hotspot.timer onde-backup.timer 2>/dev/null || true
    rm -f "$UNIT" "$HS_TIMER" "$HS_TIMER_SVC" "$BK_TIMER" "$BK_SVC" "$HOME/.local/share/applications/onde.desktop" "$HOME/Desktop/Onde.desktop" \
      "$HOME/Bureau/Onde.desktop" "$HOME/.config/autostart/onde-kiosk.desktop"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Démarrage automatique retiré. Ta musique et tes playlists sont conservées."
    ;;
  help|-h|h)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "Commande inconnue : $cmd   (./start.sh help)"
    ;;
esac
