# ♪ Onde — ton Spotify à la maison

Onde transforme ton **Raspberry Pi 5** en serveur musical privé. Depuis n'importe quel téléphone,
tablette ou PC connecté au même Wi-Fi, tu retrouves une interface façon Spotify : bibliothèque,
playlists, titres likés, recherche instantanée et lecteur plein écran. Tu importes tes MP3 d'un
glisser-déposer et les pochettes s'ajoutent toutes seules.

- **Importer** : glisse des fichiers ou des dossiers entiers (MP3, M4A, FLAC, OGG, OPUS, WAV, AIFF, WMA,
  APE…), un album en `.zip`, colle un lien direct, ou **tape simplement le nom d'une musique** (« Artiste -
  Titre »). Onde cherche d'abord ses vraies infos (iTunes/Deezer : titre, artiste, album, durée, pochette),
  puis compare plusieurs résultats YouTube — et SoundCloud si besoin — pour choisir le meilleur (audio
  officiel, bonne durée, sans live ni remix), le télécharge en MP3 et écrit tags + pochette tout seul. Onde
  range tout dans `Musique/Artiste/Album/` et repère les doublons.
- **Télécharger** : un titre, un album ou une playlist entière (en .zip), sur n'importe quel appareil.
- **Pochettes** : Onde utilise la pochette intégrée au MP3 ou une image `cover.jpg` du dossier. Sinon, il
  la **cherche automatiquement en ligne** (Apple Music, Deezer). Tu peux aussi la changer à la main
  (recherche, image de ton téléphone, lien ou copier-coller). Elle est écrite dans le fichier MP3 lui-même.
- **Écran verrouillé** : la musique continue quand le téléphone est verrouillé. Titre, pochette et
  boutons s'affichent sur l'écran de verrouillage. La lecture reprend toute seule si le Wi-Fi décroche.
- **Mode enceinte** : c'est le Pi qui joue la musique, sur ses haut-parleurs (HDMI, USB, Bluetooth).
  Ton téléphone sert juste de télécommande : tu peux l'éteindre, la musique continue.
- **AirPlay et Chromecast** : depuis le bouton « Appareil » du lecteur (AirPlay sur iPhone/iPad/Mac avec
  Safari, Chromecast avec Chrome — le Chromecast ne marche qu'en HTTP).
- **Écoute hors connexion** : le bouton de téléchargement d'un album, d'une playlist ou des titres likés
  garde la musique sur ton téléphone ou ton PC. Loin du Pi (voiture, métro, voyage), Onde s'ouvre et joue
  ce que tu as téléchargé ; tes écoutes sont comptées au retour. Pour que l'appli s'ouvre sans le Pi, il faut
  le HTTPS avec le certificat installé (Réglages › Accès sécurisé) et, sur téléphone, l'icône sur l'écran d'accueil.
- **Playlists intelligentes** : elles se remplissent toutes seules selon des règles (genre, année, écoutes,
  jamais écoutés, ajoutés récemment, likés, format…), avec des modèles prêts à l'emploi. Bouton **+** de la
  bibliothèque.
- **Optimisé pour le Pi** : le scan est incrémental (seuls les fichiers nouveaux ou modifiés sont relus),
  les miniatures WebP sont gardées en cache et les longues listes s'affichent progressivement. Il n'y a
  aucun framework lourd.

---

## 1. Installation (une seule fois)

Copie le dossier `onde` sur le Pi (clé USB, `scp`…), puis dans un terminal :

```bash
cd ~/onde
chmod +x start.sh
./start.sh service
```

C'est tout. Le script installe tout ce qu'il faut (Python, dépendances, `mpv` pour le mode enceinte),
crée la configuration et une **icône « Onde » sur le bureau**. Il fait aussi démarrer Onde
**automatiquement à chaque allumage du Pi**.

À la fin, il affiche l'adresse à ouvrir, par exemple :

```
http://192.168.1.42:8000
http://raspberrypi.local:8000
```

> Tu préfères lancer Onde à la main, sans démarrage automatique ? `./start.sh` (arrêt : Ctrl+C).

## 2. Sur ton téléphone

1. Ouvre l'adresse ci-dessus dans **Safari** (iPhone) ou **Chrome** (Android).
2. Ajoute-la à l'écran d'accueil pour avoir une vraie « app » :
   - iPhone : bouton Partager → **Sur l'écran d'accueil** ;
   - Android : menu ⋮ → **Ajouter à l'écran d'accueil**.
3. Lance un titre et verrouille le téléphone : la musique continue, avec les commandes sur l'écran
   de verrouillage.

## 3. Ajouter de la musique

- **Depuis l'appli** : onglet *Importer*. Tu peux aussi glisser des fichiers n'importe où dans la fenêtre.
- **Directement sur le Pi** : copie tes dossiers dans le dossier musique (par défaut `~/Musique` ou
  `~/Music`). Ils sont détectés automatiquement en quelques minutes, ou tout de suite avec
  *Réglages → Rescanner*.
- **Clé USB / disque dur** : ajoute son chemin dans `onde.env` (`ONDE_EXTRA_DIRS=/media/pi/MaCle/Musique`).
  Si le disque est débranché, rien n'est effacé de ta bibliothèque.

## 4. Commandes utiles

| Commande | Effet |
|---|---|
| `./start.sh` | installe si besoin puis lance Onde dans le terminal |
| `./start.sh service` | démarrage automatique au boot (conseillé) |
| `./start.sh open` | lance en arrière-plan et ouvre l'appli sur le Pi |
| `./start.sh kiosk` | Onde en plein écran sur l'écran du Pi (écran tactile) |
| `./start.sh kiosk --boot` | idem, à chaque démarrage du bureau |
| `./start.sh restart` / `stop` / `status` | redémarrer / arrêter / état |
| `./start.sh logs` | journal en direct |
| `./start.sh uninstall` | retire le démarrage automatique (ta musique est conservée) |
| `./start.sh ia` | installe les paroles synchronisées par IA (même chose que le bouton des Réglages) |
| `./start.sh server` | le Pi devient un vrai serveur : démarrage seul au boot, `http://onde.local:8000`, SSH, sauvegarde nocturne (voir ci-dessous) |
| `./start.sh server share` | dossier réseau « Musique » pour glisser des fichiers depuis un PC (`\\onde\Musique`) |
| `./start.sh server headless` / `desktop` | démarrer sans / avec le bureau (au prochain redémarrage) |
| `./start.sh remote` | accès hors de la maison via Tailscale : `private` (par défaut, appli Tailscale sur le téléphone), `public` (sans appli, mot de passe de 8 caractères minimum), `status`, `off` |
| `./start.sh server status` / `backup` | état du serveur / sauvegarde de la base tout de suite |
| `./start.sh hotspot` | le Pi diffuse son propre Wi-Fi « Onde-Music » (voir ci-dessous) |
| `./start.sh hotspot auto` | mode voiture : point d'accès tout seul quand aucun Wi-Fi connu n'est à portée |
| `./start.sh hotspot off` / `status` | revenir au Wi-Fi habituel / état et QR codes |

Sur un PC Windows : double-clique sur **`start.bat`** (avec Python), ou utilise **`Onde.exe`** (sans Python,
voir plus bas).

### Mode nomade : le Pi crée son propre Wi-Fi (voiture, vacances, sans box)

```
./start.sh hotspot
```

Le Pi diffuse le Wi-Fi **Onde-Music** (mot de passe `ondemusic`), lance Onde, et affiche deux QR codes :
le premier connecte le téléphone à ce Wi-Fi, le second ouvre Onde (`http://10.42.0.1:8000`). Si le
téléphone signale « pas d'accès à Internet », choisis « Rester connecté ». Le point d'accès reste allumé
jusqu'au prochain redémarrage du Pi ou jusqu'à `./start.sh hotspot off`.

- **En voiture, sans rien taper :** `./start.sh hotspot auto` (une fois, à la maison). À chaque démarrage,
  si aucun Wi-Fi connu n'est à portée, le Pi diffuse le sien ; il revient tout seul sur ton Wi-Fi habituel
  dès qu'il le retrouve (vérifié toutes les 5 minutes, jamais pendant qu'un appareil est connecté).
  Onde démarre aussi tout seul (le service est installé si besoin).
- **Nom et mot de passe :** `ONDE_HOTSPOT_SSID`, `ONDE_HOTSPOT_PASSWORD` (8 à 63 caractères simples)
  et `ONDE_HOTSPOT_IFNAME` (`wlan0`) dans `onde.env` — change le mot de passe par défaut, il est le même
  pour tout le monde.
- Il faut NetworkManager (Raspberry Pi OS « Bookworm » ou plus récent) et le pays du Wi-Fi réglé
  (`sudo raspi-config` → *Localisation Options* → *WLAN Country*). En SSH par le Wi-Fi, la session se coupe
  au passage en point d'accès : c'est normal, Onde continue de tourner.

### Onde.exe (Windows, sans installer Python, sans ligne de commande)

`Onde.exe` est un fichier unique : double-clic, Onde démarre sans fenêtre noire et s'ouvre dans le
navigateur. Une icône près de l'horloge permet de l'ouvrir, d'afficher l'adresse à taper sur le téléphone
(*Réglages*), de le lancer au démarrage de Windows, ou de quitter. Au premier lancement, Windows demande
d'autoriser le réseau privé : accepte, sinon le téléphone ne pourra pas se connecter. Les réglages
(`onde.env`) se créent à côté du fichier, les données et le journal (`onde.log`) dans
`%USERPROFILE%\\.local\\share\\onde`. Pour la conversion et le téléchargement par nom, pose un `ffmpeg.exe`
dans le même dossier.

**Obtenir `Onde.exe` sans rien taper :** GitHub le fabrique pour toi.
1. Sur github.com, crée un dépôt (privé), puis *Add file → Upload files* et glisse tout le contenu du dossier
   `onde` (y compris le dossier `.github`).
2. Attends 5 à 10 minutes (onglet *Actions*), puis ouvre *Releases* (colonne de droite) : `Onde.exe`
   est à télécharger. À chaque nouvel envoi de fichiers, une nouvelle version est fabriquée.

(Autre voie, sur un PC avec Python : double-clic sur `build_exe.bat`, résultat dans `dist\\Onde.exe`.)

### Application Android (Onde.apk)

Un fichier à installer sur le téléphone, qui ouvre Onde en plein écran et fait ce qu'un navigateur ne sait pas faire :
- **retrouve ton Onde tout seul** sur le Wi-Fi (maison, ou Wi-Fi « Onde-Music » du Pi en voiture) ;
- **musique en arrière-plan, écran éteint**, avec la notification (pochette, précédent / pause / suivant),
  l'écran verrouillé, le casque et les commandes de la voiture ;
- **reste sur le Wi-Fi du Pi** même sans Internet (Android bascule sinon sur la 4G et perd Onde) ;
- **notifications** (version 1.1) : paroles synchronisées par l'IA, musique ajoutée, téléchargement terminé, fin de la minuterie
  de sommeil, et « Onde est injoignable / de retour » ; chacune se coupe dans *Réglages*. Le serveur les tient dans un journal
  que l'appli interroge (aucun service tiers, aucun compte). Elles arrivent tant que l'appli vit ; pour les recevoir appli
  fermée, active *Notifications même appli fermée* (icône discrète permanente) et retire l'optimisation de batterie pour Onde ;
- **à distance** : deux adresses, maison et distance. L'appli essaie la maison, puis la distance (4G, autre Wi-Fi), et bascule
  toute seule en cas de panne (voir « Accès à distance » plus bas) ;
- imports de musique, téléchargements, mot de passe retenu, et « Changer de serveur » dans *Réglages*.

**Installer :** copie `Onde.apk` sur le téléphone (câble, mail, Drive…), ouvre-le, et autorise l'installation depuis
cette source quand Android le demande. Au premier lancement : *Chercher mon Onde* (ou tape l'adresse du Pi).
Android 13 et plus demande l'autorisation d'afficher les notifications : accepte, c'est elle qui montre les commandes
de lecture. Android 5.0 minimum.

**Fabriquer l'APK soi-même** (Linux, Raspberry Pi OS compris) :
`sudo apt install default-jdk-headless aapt apksigner zipalign dalvik-exchange android-sdk-platform-23`
puis `./android/build.sh` : le résultat est `android/build/Onde.apk`. Garde `android/build/onde.keystore` : c'est la clé
de signature, indispensable pour que les futures versions s'installent par-dessus celle-ci. Sans rien installer, GitHub
le fait aussi (même principe que pour `Onde.exe`) : l'APK arrive dans *Releases*.

## 5. Réglages (`onde.env`)

Le fichier est créé au premier lancement. Après une modification : `./start.sh restart`.

| Variable | Par défaut | Rôle |
|---|---|---|
| `ONDE_MUSIC_DIR` | `~/Musique` | dossier de la musique |
| `ONDE_EXTRA_DIRS` | — | autres dossiers à scanner (séparés par `:`) |
| `ONDE_PORT` | `8000` | port web |
| `ONDE_PASSWORD` | vide | mot de passe (plus simple : *Réglages → Mot de passe*) |
| `ONDE_AUTO_COVERS` | `1` | chercher les pochettes manquantes en ligne |
| `ONDE_EMBED_COVERS` | `1` | écrire les pochettes dans les fichiers |
| `ONDE_SCAN_INTERVAL` | `300` | rescan automatique (secondes, `0` = jamais) |
| `ONDE_MAX_UPLOAD_MB` | `1024` | taille maximale d'un import |
| `ONDE_MPV_ARGS` | — | options de mpv pour le mode enceinte (sortie audio…) |
| `ONDE_HTTPS` | `0` | HTTPS sur le réseau local (plus simple : l'interrupteur dans *Réglages → Accès sécurisé*) |
| `ONDE_HOTSPOT_SSID` | `Onde-Music` | nom du Wi-Fi du mode nomade |
| `ONDE_HOTSPOT_PASSWORD` | `ondemusic` | son mot de passe (8 à 63 caractères) |
| `ONDE_HOTSPOT_IFNAME` | `wlan0` | interface Wi-Fi du Pi |

**Choisir la sortie son du mode enceinte :** `mpv --audio-device=help` liste les sorties, puis par exemple
`ONDE_MPV_ARGS=--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0`. Le Pi 5 n'a pas de prise jack : utilise
le HDMI, une carte son USB ou une enceinte Bluetooth appairée.

**HTTPS local (cadenas dans le navigateur) :** dans Onde, *Réglages → Accès sécurisé*, active
l'interrupteur. Onde installe au besoin ce qu'il lui faut, crée son certificat et redémarre tout seul en
quelques secondes (pour le désactiver : même interrupteur). Les anciens liens en `http://` (favoris, icône
sur l'écran d'accueil) continuent de marcher : ils sont redirigés automatiquement. Sur le Pi lui-même
(kiosque), `http://localhost` reste en HTTP simple, sans avertissement.

Au premier accès, chaque appareil affiche un avertissement « connexion non privée » : c'est normal pour un
serveur privé. Pour ne plus jamais le voir, installe le certificat d'Onde **une fois par appareil** — la même
page te guide pas à pas selon ton appareil (iPhone, Android, Windows, Mac), avec un QR code à scanner pour
le téléphone. Il reste valable même si l'adresse IP du Pi change.

## 6. Utilisation

- **Clic** sur un titre : lecture. **Clic droit** (ou **appui long** sur mobile) ou bouton **⋯** :
  file d'attente, playlists, modifier les infos, changer la pochette, télécharger, supprimer.
- **Pochette** : clique sur la grande pochette d'un album pour la changer.
- **Paroles** : Onde les cherche tout seul en arrière-plan pour toute ta bibliothèque (les titres les plus
  écoutés d'abord) et réessaie régulièrement tant qu'elles ne sont pas trouvées. Un fichier `.lrc` du même
  nom que le morceau est utilisé en priorité. Paroles trouvées mais non synchronisées : l'IA les cale
  sur la voix (voir *Paroles synchronisées par IA*).
- **Playlists** : bouton **+** de la bibliothèque. Réorganise les titres en les glissant par la poignée ⠿.
- **Appareil** (icône écran à droite du lecteur) : choisis *Ce navigateur* ou *Raspberry Pi (haut-parleurs)*.
- **Clavier** : Espace (lecture/pause), ←/→ (±10 s), Ctrl+←/→ (titre précédent/suivant), Ctrl+↑/↓ (volume),
  M (muet), S (aléatoire), R (répéter), L (liker), Q (file d'attente), F (plein écran), / (rechercher).

## 7. En cas de souci

- **La page ne s'ouvre pas** : le téléphone et le Pi doivent être sur le même Wi-Fi. Vérifie avec
  `./start.sh status` et regarde `./start.sh logs`. Si `raspberrypi.local` ne marche pas, utilise l'adresse IP.
- **Pas de pochettes automatiques** : le Pi doit avoir accès à Internet. Lance *Réglages → Chercher les pochettes*.
- **Le mode enceinte est grisé** : installe mpv (`sudo apt install mpv`) puis `./start.sh restart`.
- **Pas de son en mode enceinte** : choisis la sortie avec `ONDE_MPV_ARGS` (voir plus haut).
- **La musique s'arrête écran verrouillé (iPhone)** : utilise Safari (ou l'icône ajoutée à l'écran
  d'accueil) et lance le premier titre d'un appui. iOS exige ce premier geste.
- **Un format ne se lit pas sur iPhone** (OGG/OPUS) : Safari ne sait pas les lire. Le MP3, le M4A et
  le FLAC fonctionnent partout, et le mode enceinte lit tous les formats.

## Architecture

```
onde/
├── start.sh            ← script tout-en-un (Linux / Raspberry Pi)
├── start.bat           ← lancement sur Windows
├── android/            ← appli Android (sources Java, build.sh) → Onde.apk
├── build_exe.bat       ← fabrique Onde.exe (recette : onde.spec, point d'entrée : onde_app.py)
├── requirements.txt    ← FastAPI, Uvicorn (uvloop + httptools), Mutagen, Pillow, httpx
├── tests/              ← tests automatiques :  pip install -r requirements-dev.txt  puis  python -m pytest
└── onde/
    ├── main.py         ← application, connexion, bibliothèque, titres, lecture (streaming avec Range, zip en flux)
    ├── web.py          ← outils partagés par les routes (réponse JSON rapide, erreurs, lecture d'un titre)
    ├── routes_playlists.py   ← playlists (intelligentes, pistes, import/export M3U, pochette, zip)
    ├── routes_import.py      ← import (envoi direct, lien, playlist en ligne)
    ├── routes_maintenance.py ← rescan, doublons, qualité, export de la bibliothèque, modifs en lot
    ├── routes_pi.py          ← mode enceinte (lecture sur le Pi)
    ├── routes_sync.py        ← lecture synchronisée entre appareils
    ├── stats.py        ← statistiques d'écoute et bilan de l'année (« Ton année »)
    ├── library.py      ← scan incrémental, tags ID3/MP4/FLAC/Vorbis, import, pochettes
    ├── covers.py       ← miniatures WebP, couleur dominante, recherche en ligne
    ├── net.py          ← client web partagé (httpx) : délais stricts, taille plafonnée, nouvel essai
    ├── lyrics.py       ← paroles (.lrc, tags, .txt, LRCLIB) et leur recherche automatique
    ├── clean.py        ← noms allégés pour la recherche (émojis, [AMV], 4K, séparateurs…)
    ├── devlog.py       ← journal en mémoire de la console développeur (F7), requêtes, infos système
    ├── notify.py       ← journal des notifications lu par l'appli Android
    ├── remote.py       ← état de l'accès à distance (Tailscale)
    ├── align.py        ← paroles brutes calées sur la voix (stable-whisper) → .lrc, en arrière-plan
    ├── align_worker.py ← le calage lui-même, dans un processus séparé (mémoire isolée, basse priorité)
    ├── netinfo.py      ← adresses du Pi, détection du point d'accès Wi-Fi
    ├── hotspot.py      ← QR codes du mode nomade (Wi-Fi + adresse d'Onde)
    ├── pi_player.py    ← mode enceinte (mpv piloté par socket IPC, état en temps réel via SSE)
    ├── tls.py          ← HTTPS : autorité locale + certificat du serveur (règles Apple), renouvelé seul
    ├── gateway.py      ← porte d'entrée HTTP/HTTPS sur le même port (redirections, certificat en http)
    ├── restart.py      ← redémarrage propre depuis l'interface
    ├── smart.py        ← playlists intelligentes (règles → requête SQL sûre)
    ├── db.py           ← SQLite en mode WAL
    └── static/         ← interface web (HTML/CSS/JS sans framework, aucune dépendance externe) ;
                           offline.js + sw.js : écoute hors connexion ;
                           devhooks.js + devconsole.js : console développeur (F7)
```

Les données (base, cache des pochettes) sont dans `~/.local/share/onde/`. Ta musique reste de simples
fichiers dans ton dossier : tu peux les copier ou les sauvegarder comme tu veux.

## Le Pi en serveur

`./start.sh server` fait tout en une fois, sans rien casser (chaque étape est facultative et peut être relancée) :

- **Démarrage tout seul** à chaque allumage, sans écran, clavier ni session ouverte ; Onde est relancé s'il plante, sans jamais abandonner.
- **Nom sur le réseau** : si le Pi s'appelle encore `raspberrypi`, il devient `onde` (`http://onde.local:8000`). Pour un autre nom : `ONDE_HOSTNAME=salon` dans `onde.env`. Onde apparaît aussi dans la liste des appareils du réseau (avahi).
- **SSH activé**, pour l'administrer sans écran, et **pare-feu** (ufw) : le port d'Onde est ouvert seulement si ufw est déjà actif.
- **Sauvegarde chaque nuit à 4 h** de la base (playlists, favoris, historique) dans `~/.local/share/onde/backups`, 7 copies gardées, vérifiées. Pour restaurer : `./start.sh stop`, copier une sauvegarde sur `onde.db`, `./start.sh restart`.
- **Dossier réseau** (`./start.sh server share`, ou `ONDE_SHARE=1`) : Windows `\\onde\Musique`, Mac/Linux `smb://onde.local/Musique`. Sans mot de passe d'Onde, accès libre sur ton Wi-Fi ; avec `ONDE_PASSWORD`, identifiant = ton compte du Pi, mot de passe = celui d'Onde. Les fichiers déposés apparaissent dans Onde en moins de 5 minutes.
- **Sans bureau** : `./start.sh server headless` libère de la mémoire (le mode plein écran `kiosk` n'est alors plus possible ; `desktop` pour revenir).

`./start.sh server status` vérifie chaque point.

## Paroles automatiques

Rien à faire : Onde cherche les paroles de toute la bibliothèque en arrière-plan, en commençant par le titre que tu
lances (et le suivant de la file). Pour chaque titre, dans l'ordre :

1. un fichier `.lrc` à côté du morceau (`Titre.lrc` ou `Titre.mp3.lrc`), puis les paroles des tags (ID3 USLT/SYLT,
   FLAC/Vorbis, MP4, APE, WMA), puis un `Titre.txt` à côté ;
2. [LRCLIB](https://lrclib.net) (gratuit, sans compte). Le nom envoyé est d'abord allégé : émojis, symboles, séparateurs,
   « [AMV] », « 4K », « Sub. Español », « (Official Video) »… sont retirés
   (`MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K` → `MOONLIGHT — Kali Uchis`), sans rien changer à tes
   fichiers ni à ta bibliothèque ; si l'artiste est écrit dans le titre, les deux ordres (« Titre ~ Artiste » et
   « Artiste - Titre ») sont essayés. Même allègement pour la recherche et le téléchargement de musique par nom.
   Puis correspondance exacte (titre, artiste, album, durée), puis des
   variantes (sans « feat. », « Remaster », « (Official Video) », numéro de piste ; artiste sans « VEVO » / « - Topic » ;
   premier artiste ; artiste de l'album ; « Artiste - Titre » rangé dans le titre), puis par le titre seul. Au plus 8
   requêtes par titre. Un résultat n'est gardé que si le titre, l'artiste et la durée correspondent : rien plutôt que les
   paroles d'une autre chanson. Des paroles synchronisées d'une autre version (durée différente) ne gardent que leur texte ;
3. les paroles non synchronisées sont remplacées par une version synchronisée dès que LRCLIB en a une (vérifié chaque
   mois), ou calées sur la voix par l'IA si elle est installée (ci-dessous).

Un titre introuvable est réessayé tout seul (30 min, 2 h, 6 h, 1 jour, 3 jours, puis chaque semaine). Sans Internet, rien
n'est compté comme un échec. *Réglages → Recherche des paroles → Tout rechercher* relance tout de suite les introuvables.
Les paroles effacées ou tapées à la main ne sont jamais remplacées.

## Console développeur (F7)

Touche **F7** sur ordinateur, ou *Réglages → Console développeur → Ouvrir* sur téléphone, tablette et dans l'appli Android.
Elle marche aussi à distance (4G/5G). Onglets :

- **Journal** : tout ce que fait le serveur (paroles, IA, imports, pochettes, lecteur du Pi, erreurs avec leur détail) et la
  page, en direct. Les erreurs des autres appareils (téléphone, appli, iPhone) y remontent aussi : ouvre la console sur le PC
  pour voir ce qui coince sur le téléphone. Filtres par niveau, provenance et texte ; case *détaillé* pour voir chaque requête
  vers LRCLIB. En bas, une ligne de commande JavaScript (`player`, `api`, `dev` ; tape `help`).
- **Réseau** : les requêtes de cette page, ou celles reçues par le serveur depuis tous les appareils.
- **Paroles** : chiffres, recherche en cours, 40 dernières recherches (résultat, source, requêtes, durée), boutons
  *Tout rechercher maintenant* et *Chercher le titre en cours*.
- **IA**, **Système** (machine, mémoire, température, disques, outils, réglages, tâches) et **Lecteur** (état de la lecture
  et de l'élément audio, chaque seconde).
- *Copier* / *Télécharger* : un rapport complet à joindre pour signaler un problème. Le journal reste en mémoire (3 000
  messages), jamais sur le disque, et les mots de passe n'y apparaissent pas.



Beaucoup de titres n'ont sur LRCLIB (ou dans leurs tags) que des paroles **non synchronisées**. Onde peut
les **caler tout seul sur la voix**, ligne par ligne, avec [stable-whisper](https://github.com/jianfch/stable-ts),
sur le serveur lui-même — sans compte, sans service en ligne.

**Installation** (une fois, ~1 Go, il faut Internet le temps du téléchargement) : *Réglages → Paroles
synchronisées par IA → Installer*, ou `./start.sh ia`. Le décodage audio passe par `ffmpeg` (celui du
système, sinon celui fourni par `imageio-ffmpeg`). Non disponible dans Onde.exe (trop lourd à embarquer :
lance `start.bat` à la place).

**Ensuite, tout est automatique :**

- dès qu'Onde a des paroles brutes pour un titre (LRCLIB, tags, ou collées à la main), il les cale en
  arrière-plan, un titre à la fois, les plus écoutés d'abord ;
- le texte est découpé ligne par ligne (lignes vides, `[Refrain]`, « Couplet 2 : » retirés) et chaque ligne
  garde sa place grâce à `original_split=True` — le texte n'est jamais regroupé ni entassé en fin de morceau ;
- résultat au format `[mm:ss.xx] ligne`, enregistré dans la base et écrit en `.lrc` à côté du morceau (un
  `.lrc` synchronisé existant n'est jamais remplacé ; un `.lrc` de paroles brutes est gardé en `.txt`) ;
- un alignement douteux (mauvaises paroles, autre version du morceau) est refusé plutôt qu'affiché faux.

**À la main** : dans le panneau des paroles, *Synchroniser sur la voix (IA)*, ou *Modifier → Enregistrer et
synchroniser* après avoir collé des paroles. En ligne de commande, pour n'importe quel fichier :

```bash
.venv/bin/python -m onde.align chanson.mp3 paroles.txt            # → chanson.lrc à côté
.venv/bin/python -m onde.align chanson.flac paroles.txt -o sortie.lrc --langue fr --modele small
```

**Sans jamais gêner la musique** : le calage tourne dans un processus séparé, en basse priorité, en
laissant un cœur libre. S'il manque de mémoire, seul ce processus s'arrête et Onde le dit clairement
(*Mémoire insuffisante… choisis le modèle « tiny »*) ; un fichier abîmé est signalé (*Fichier audio
illisible*) et n'est pas réessayé ; un échec passager est réessayé le lendemain (3 fois au plus).

**Modèle** (Réglages, ou `ONDE_ALIGN_MODEL`) : `tiny` (0,6 Go), `base` (0,9 Go, par défaut, bon compromis
sur Pi 5), `small` (1,9 Go), `medium` (4,8 Go). Sur un Pi 5 avec `base`, compte de l'ordre d'une à quelques
minutes par morceau (estimation : pas encore mesuré sur un vrai Pi). La langue est devinée d'après les paroles (sinon à l'écoute), ou fixée dans les Réglages.

## Accès à distance (4G, autre Wi-Fi)

Onde n'a besoin ni d'ouvrir de port sur ta box ni de nom de domaine : il passe par [Tailscale](https://tailscale.com) (gratuit
pour un usage perso). `./start.sh remote` installe Tailscale, te fait connecter ton compte, puis publie Onde en **https** avec un vrai
certificat à l'adresse `https://nom-du-pi.ton-reseau.ts.net`.

- **Privé** (par défaut) : seuls tes appareils où l'appli Tailscale est connectée y accèdent. C'est le plus sûr.
- **Public** (`./start.sh remote public`) : tout Internet peut atteindre la page de connexion, sans appli sur le téléphone. Un mot
  de passe `ONDE_PASSWORD` d'au moins 8 caractères est obligatoire ; après 8 essais ratés, l'adresse est bloquée 15 minutes.
- Dans la console Tailscale (*DNS* → HTTPS, et *Access controls* pour Funnel), active les certificats HTTPS ; le script le rappelle.
- `./start.sh remote off` retire le partage. Les Réglages d'Onde affichent l'adresse, et l'appli Android la retient toute seule.

Non testé avec un vrai compte Tailscale ni sur un vrai Pi : le script et les pages ont été vérifiés avec un faux `tailscale`.

## iPhone / iPad

Pas d'appli iOS gratuite à distribuer : publier sur l'App Store coûte 99 $/an (compte Apple Developer), et un fichier installé à la
main avec un identifiant Apple gratuit (AltStore, Sideloadly) expire au bout de 7 jours et demande un Mac ou un PC.
Le plus simple et gratuit : ouvre Onde dans Safari, *Partager* → *Sur l'écran d'accueil*. Hors de la maison (4G/5G),
fais-le avec l'adresse https de `./start.sh remote` (le QR code est dans *Réglages → Accès à distance*) : en mode privé,
installe d'abord l'appli Tailscale (gratuite) sur l'iPhone ; en mode public, rien à installer. Onde s'ouvre alors en plein écran
comme une appli et s'adapte à toutes les tailles d'écran. Limites : pas de notifications Onde (elles demanderaient un service de push, non prévu) et pas de recherche automatique
du serveur : tape son adresse une fois, ou utilise l'adresse à distance.


## Rangement automatique des noms

Au démarrage et après chaque scan ou import, Onde :

- **raccourcit les titres** (émojis, `[AMV]`, `4K`, `Sub. Español`, `||`… retirés : « MOONLIGHT🌙~ Kali Uchis || [AMV] 4K » → « MOONLIGHT — Kali Uchis »). Seul le nom affiché change, jamais les fichiers. Réglage : *Titres raccourcis*.
- **retire les morceaux « Slowed » et « Reverb »** (mot entier dans le titre ou le nom du fichier ; mots modifiables dans Réglages → Corbeille automatique). Ils vont dans `corbeille/` (dossier de données d'Onde), récupérables 30 jours ; un morceau récupéré n'est plus retiré. Garde-fou : si plus de la moitié de la bibliothèque est concernée, rien n'est supprimé.
- **la recherche** raccourcit aussi ce que tu tapes (« Recherche raccourcie : … »).

## Windows : outils fournis

`windows/tools/` reçoit `ffmpeg.exe` (conversion, volume, téléchargement par nom), téléchargé tout seul par `start.bat` au 1er lancement s’il manque. `start.bat` et Onde.exe les trouvent tout seuls (avec Onde.exe, copie le dossier `tools` à côté ; `build_exe.bat` le fait). `start.bat ia` installe l'IA des paroles.


## Playlist de départ

Au premier démarrage, Onde télécharge tout seul, en arrière-plan, la playlist de départ (Hips Don't Lie, Mastu, JOYCA, Dominic Fike, GIMS/SCH, Black M, Sexion d'Assaut…) : le serveur démarre tout de suite, les titres arrivent au fur et à mesure. Déjà présents → sautés ; échec (pas d'Internet) → retenté aux démarrages suivants (3 fois). Réglage *Playlist de départ* (ou `ONDE_STARTER_PACK=0`). Liste : `onde/first_run.py`.

## Téléchargement par nom : YouTube + SoundCloud

Un nom tapé (« bigflo et oli - alors alors ») est cherché sur YouTube **et** SoundCloud en même temps ; le meilleur résultat des deux est gardé (titre, artiste, durée ; extraits de 30 s et lives écartés). Les infos (album, pochette) ne sont reprises d'Apple Music / Deezer que si elles correspondent vraiment à ce qui a été tapé (« et » = « & »). Liens SoundCloud acceptés : un titre, une playlist (`/sets/…`), les likes ou tous les titres d'un artiste.

## Réseau faible (4G qui capte mal)

Une requête ratée est retentée, et Onde ne passe en « hors connexion » qu'après plusieurs échecs d'affilée : la musique continue et la page reste affichée. L'appli Android (1.2) réessaie toute seule sans page d'erreur et essaie `100.99.41.84` au premier lancement.


## Profils

Réglages → Mon profil → *Choisir un mot de passe* active les profils (pseudo de l'administrateur : `ONDE_ADMIN_NAME`, par défaut **Mopidie**). Ensuite chacun se connecte avec son pseudo.
- **Administrateur** : tout. **Écoute + import** : écouter, liker, ses playlists, importer — pas de suppression ni de réglages. **Écoute seule** : sans import. Vérifié par le serveur à chaque requête.
- **Inviter un pote** : lien (et QR code) valable 24 h / 7 j / 30 j, pour 1 à 20 personnes ; il choisit son pseudo et son mot de passe (mail facultatif). Pour qu'il vienne de chez lui sans Tailscale : accès public (Réglages → Accès à distance) avant de créer le lien.
- Une bibliothèque commune ; likes, playlists, historique et statistiques propres à chacun. Journal d'activité (connexions, imports, suppressions, écoutes) pour l'administrateur.
- Mots de passe stockés hachés (scrypt), sessions par cookie HttpOnly, blocage après 8 essais ratés.
- Mot de passe oublié par mail : renseigner `ONDE_SMTP_HOST`, `ONDE_SMTP_PORT` (587 ou 465), `ONDE_SMTP_USER`, `ONDE_SMTP_PASSWORD`, `ONDE_SMTP_FROM` dans `onde.env`. Sinon l'administrateur redonne un mot de passe depuis Réglages → Profils.

## Appli Android 1.3 (sûre pour les antivirus)

- Cible Android 15 (API 35) : plus d'alerte « appli conçue pour une ancienne version d'Android ».
- Plus de « certificat accepté sans vérification » : la clé du serveur est retenue à la 1re connexion (épinglage, comme SSH) et toute autre est refusée ; les vrais certificats (Tailscale) sont vérifiés normalement.
- Signée v1 + v2 + v3, services déclarés (lecture, notifications), permission de notification demandée (Android 13+), pas de sauvegarde des données de connexion.
- Si Play Protect la signale quand même (normal pour une appli installée hors Play Store) : « Plus de détails » → « Installer quand même », puis éventuellement « Envoyer l'appli pour analyse ».


## Nouveautés 1.2

- **Téléphone : tout sur un écran** — le lecteur, puis les paroles en direct, puis « À suivre » en faisant défiler.
- **Mode karaoké** : bouton « Mode karaoké » sous des paroles synchronisées (plein écran, ligne en cours en grand).
- **Fondu enchaîné** (3 à 12 s) et **égaliseur 5 bandes** avec préréglages : Réglages → Son sur cet appareil.
- **Minuterie de sommeil progressive** : le volume baisse doucement pendant les dernières minutes.
- **Hors connexion automatique** : en Wi-Fi, l'appareil garde tes titres likés et tes dernières écoutes.
- **Reprise sur un autre appareil** : en ouvrant Onde ailleurs, « Reprendre … là où tu l'as laissé ».
- **Mix** : Découvertes de la semaine (renouvelé le lundi), Il y a un an, Mix par artiste, Chill / Énergie / Soirée / Concentration.
- **Mode soirée** : File d'attente → « Mode soirée » → QR code ; tes potes ajoutent des titres depuis leur téléphone, sans compte.
- **Doublons** : garde la meilleure version (qualité d'abord), les autres vont à la corbeille ; likes et playlists suivent.
- **Qualité audio** : titres à 128 kbit/s ou moins, remplacés par une meilleure version trouvée en ligne.
- **Infos en lot** : sélectionne plusieurs titres → « Modifier les infos » ou « Réparer les infos en ligne ».
- **Artistes suivis** : « Suivre » sur la page d'un artiste → ses nouveautés sont téléchargées toutes seules.
- **Sauvegarde automatique** quotidienne de la base, chiffrée avec une phrase secrète (AES-256) ; restauration en un clic.
  À la main : `python -m onde.backup ouvrir FICHIER.ondebk "phrase"`.
- **Mise à jour en un clic** : Réglages → Mise à jour → choisis le nouveau zip (start.bat / start.sh ; pas Onde.exe).
- **Installateur Windows** : `build_installer.bat` (Inno Setup 6) → `dist/Onde-Installation-1.2.0.exe` avec raccourcis,
  démarrage avec Windows au choix et désinstallation.

## Comptes dans l'appli Android (1.4)

- Écran de compte de l'appli : **connexion** (pseudo + mot de passe, retenu), **créer mon compte** avec le lien ou le code
  d'invitation, **mot de passe oublié** (si l'envoi de mails est configuré), **changer de serveur**.
- Le lien d'invitation, ouvert sur un téléphone Android, propose « J'ai l'appli Onde : créer mon compte dans l'appli »
  (lien `onde://join`) : l'appli s'ouvre directement sur la création du compte, avec la bonne adresse.
- Réglages → Mon profil → « Se déconnecter » ramène à l'écran de compte de l'appli (pour changer de compte).
- La session vaut pour l'adresse de la maison et l'adresse à distance ; notifications et lecture en fond suivent le compte.

## Ton année (bilan façon « Wrapped »)

Sur la page **Statistiques**, le bouton **Ton année** ouvre ton bilan en diapositives plein écran : temps d'écoute,
artiste, titre et album de l'année, ton moment de la journée, mois et jour records, série de jours d'affilée,
nouveaux artistes découverts. Touche l'écran (ou flèches du clavier) pour avancer, Échap pour fermer ; un sélecteur
permet de revoir les années précédentes, et « Rejouer mon top » lance tes 5 titres préférés. Le bilan est propre à
chaque profil. (API : `GET /api/wrapped?year=2025`.)

## Nouveautés 1.3.3

- **« Mot de passe oublié ? » sans mail** : sur la page de connexion (et dans l'appli, APK 1.6), le pote entre son pseudo → l'admin reçoit une **notification** (appli Android) et une pastille + une carte « Mots de passe oubliés » dans le Tableau de bord, avec « Créer le lien ». Rien n'est révélé au pote sur l'existence du pseudo ; une demande par pseudo toutes les 10 min ; les notifications de demandes ne vont qu'à l'admin.

## Nouveautés 1.3.2

- **Mot de passe oublié sans mail** : Réglages → Profils → bouton « lien » (chaîne) à côté d'un profil. Onde crée un lien valable 24 h, à usage unique (un nouveau lien annule l'ancien). Tu l'envoies au pote : il choisit son mot de passe, ses anciens appareils sont déconnectés. Le journal d'activité garde la trace.
- **Son qui coupe** : le lecteur détecte une lecture sans son et se relance seul (1.3.1).

## Nouveautés 1.3

**Installer Onde sur Windows** : double-clique `Onde-Installation-1.3.4.exe` (à la racine du zip). Installation
guidée, sans droits administrateur : conditions d'utilisation à accepter, choix du dossier, raccourcis (Menu
Démarrer, Bureau, démarrage avec Windows), puis l'installateur télécharge Python 3.12 (dans le dossier d'Onde
seulement), les modules et ffmpeg. Désinstallation propre depuis « Applications installées » ; tes données
(bibliothèque, profils, réglages) restent dans ton dossier utilisateur. Windows SmartScreen peut afficher
« Windows a protégé votre ordinateur » (installateur non signé) : « Informations complémentaires » → « Exécuter quand même ».

**Mise à jour automatique** (Réglages → Mise à jour) : à chaque démarrage puis toutes les 12 h, en arrière-plan.
Choisis la source : un dépôt GitHub (`pseudo/depot` ; la *release* la plus récente qui contient le zip d'Onde fait foi,
les releases Onde.exe / Onde.apk sont ignorées ; GitHub fournit l'empreinte SHA-256, vérifiée) ou l'adresse d'un fichier `onde.json`
(`{"version": "1.3.1", "url": "https://…/onde.zip", "sha256": "…"}`). Le zip contient `android/Onde.apk` :
le serveur le donne aux téléphones, et l'appli (1.5) propose toute seule sa mise à jour (Android demande
confirmation ; il faut autoriser « installer des applis inconnues » pour Onde, une fois).

**Sortir une mise à jour depuis GitHub** : augmente `VERSION` dans `onde/config.py` et envoie sur `main`. Le workflow
« Zip de mise à jour » (`.github/workflows/release-zip.yml`) lance les tests puis publie la release `vX.Y.Z` avec
`onde-tout.zip` (+ son `.sha256`). Dans Réglages → Mise à jour, mets `Mopidie843/onde` comme source : les
installations le trouvent, vérifient l'empreinte et se mettent à jour toutes seules (Onde.exe excepté : il se remplace à la main).

**Conditions d'utilisation** (`CONDITIONS.txt`, page `/conditions`) : usage privé, rien partager ni revendre,
chacun responsable de ce qu'il importe, accès coupé en cas d'abus. Acceptées à l'installation, à la création
d'un profil (case à cocher) et une fois par profil existant. ⚠ Ce texte montre ta bonne foi et met la
responsabilité sur la personne qui triche ; il ne rend pas légal le téléchargement d'œuvres protégées.

**Espace par profil** : chaque titre importé compte pour le profil qui l'a ajouté. Par défaut 5 Go
(« écoute + import », `ONDE_QUOTA_MB`) et 2 Go (« écoute seule », titres demandés, `ONDE_QUOTA_LISTENER_MB`) ;
l'admin n'a pas de limite et change celle de chacun dans le tableau de bord. Plein → import refusé, et le
profil peut demander plus d'espace d'un clic.

**Demandes de titres** (onglet « Demander ») : un profil « écoute seule » demande un titre ; l'admin accepte
ou refuse depuis le tableau de bord. Sans réponse en 5 minutes (`ONDE_ASK_DELAY`), une mini IA décide :
non pour une version retouchée (slowed, nightcore, sped up, 8D, 1 heure…), un doublon, des paroles haineuses
ou un profil plein ; oui si l'artiste a déjà des titres dans la bibliothèque, ou si le titre existe vraiment
(paroles sur LRCLIB, infos Deezer). Règles locales : rien n'est envoyé à un service d'IA. 15 titres
acceptés automatiquement au plus par jour et par profil, 5 demandes en attente au plus.

**Partage de playlist** (menu ⋯ d'une playlist → « Partager ») : lien (et QR code) d'écoute seule, sans
compte ; il suit la playlist et se coupe d'un clic. Rien d'autre n'est accessible par ce lien.

**Tableau de bord** (admin) : connectés, ce qui s'écoute (et sur quel appareil), espace disque et par profil,
demandes (avec compte à rebours et « Avis IA »), erreurs d'import.

**Mix selon l'heure** : en tête des mixes de l'accueil — énergique le matin, pop à midi, concentration
l'après-midi, ça monte en fin de journée, calme le soir et la nuit (d'après les genres ; sans genres, ce que
tu écoutes d'habitude à cette heure-là).

**Plus fluide sur réseau faible (VPN, 4G)** : les pages principales sont gardées sur l'appareil (affichées
tout de suite si le réseau traîne, mises à jour en fond) et le titre suivant est téléchargé en entier pendant
l'écoute : changer de page ne fait plus couper la musique. Pochettes plus légères quand le réseau est lent.
