"""Profils d'Onde : pseudo + mot de passe (+ mail facultatif), droits, invitations, journal d'activité.

- Tant qu'aucun profil n'a de mot de passe, Onde reste en accès libre (comme avant) et tout le monde
  agit en administrateur. Dès que l'administrateur se donne un mot de passe (Réglages → Profils),
  il faut se connecter avec un pseudo et un mot de passe.
- Une seule bibliothèque pour tous ; likes, playlists, historique et statistiques propres à chacun.
- Droits : « admin » (tout), « member » (écoute + import, sans suppression ni réglages),
  « listener » (écoute seule). Les droits sont vérifiés par le serveur à chaque requête.
- Mots de passe : jamais stockés en clair (scrypt, sel aléatoire). Sessions : jeton aléatoire dans un
  cookie HttpOnly, seule son empreinte est gardée en base.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import smtplib
import ssl
import threading
import time
from contextvars import ContextVar
from email.message import EmailMessage

from . import config, db, terms

log = logging.getLogger("onde.users")

ADMIN_ID = 1
ROLES = {"admin": "Administrateur", "member": "Écoute + import", "listener": "Écoute seule"}
SESSION_DAYS = 365
COOKIE = "onde_session"
NAME_RE = re.compile(r"^[\w .'\-]{2,32}$", re.U)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
# Visiteur sans session sur une page publique (soirée, partage, connexion…) : jamais l'administrateur.
# Profil 0 : aucune donnée à lui (likes, playlists…), aucun droit en dehors des pages publiques.
GUEST = {"id": 0, "name": "Invité", "role": "guest", "email": "", "hasPassword": False, "terms": False}

current_user: ContextVar[dict | None] = ContextVar("onde_user", default=None)
_lock = threading.Lock()
_cache: dict[str, tuple[dict, float]] = {}   # empreinte du jeton -> (profil, fin de validité du cache)
_enabled: list = []                          # [bool] : au moins un mot de passe défini


# ------------------------------------------------------------------ profil courant
def _public(row) -> dict:
    keys = row.keys()
    return {"id": row["id"], "name": row["name"], "role": row["role"], "email": row["email"] or "",
            "hasPassword": bool(row["pw"]),
            "terms": "terms_version" in keys and row["terms_version"] == terms.VERSION}


_admin_cache: list = []


def admin_cached() -> dict:
    """L'administrateur, gardé quelques secondes (accès libre : une requête sur chaque appel sinon)."""
    with _lock:
        if _admin_cache and _admin_cache[1] > time.time():
            return _admin_cache[0]
    a = admin()
    with _lock:
        _admin_cache[:] = [a, time.time() + 5]
    return a


def refresh_cache() -> None:
    """Profils en cache oubliés (après un changement : conditions acceptées…)."""
    with _lock:
        _cache.clear()
        _admin_cache.clear()


def admin() -> dict:
    row = db.q1("SELECT * FROM users WHERE id=?", ADMIN_ID)
    return _public(row) if row else {"id": ADMIN_ID, "name": config.ADMIN_NAME, "role": "admin", "email": "",
                                     "hasPassword": False}


def current() -> dict:
    return current_user.get() or admin()


def uid() -> int:
    return db.current_uid.get()


def is_admin() -> bool:
    return current()["role"] == "admin"


def is_guest() -> bool:
    """Visiteur sans session (page publique : soirée, partage, connexion…) ?"""
    return current()["role"] == "guest"


class use:
    """Agit au nom d'un profil (requête, tâche de fond) : `with users.use(profil): …`."""

    def __init__(self, user: dict):
        self.user = user

    def __enter__(self):
        self._t1 = current_user.set(self.user)
        self._t2 = db.current_uid.set(self.user["id"])
        return self.user

    def __exit__(self, *exc):
        db.current_uid.reset(self._t2)
        current_user.reset(self._t1)


# ------------------------------------------------------------------ mots de passe
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"scrypt$16384$8$1${b(salt)}${b(h)}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        kind, n, r, p, salt, h = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(h)))
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


_DUMMY = None


def _dummy_check(pw: str) -> None:
    """Même durée qu'une vraie vérification (on ne révèle pas si un pseudo existe)."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(8))
    check_password(pw, _DUMMY)


def validate_password(pw: str) -> str | None:
    if len(pw) < 6:
        return "Choisis un mot de passe d'au moins 6 caractères."
    if len(pw) > 128:
        return "Mot de passe trop long (128 caractères au plus)."
    if pw != pw.strip():
        return "Pas d'espace au début ni à la fin du mot de passe."
    return None


def validate_name(name: str) -> str | None:
    if not NAME_RE.match(name or ""):
        return "Pseudo de 2 à 32 caractères (lettres, chiffres, espace, . ' - _)."
    return None


# ------------------------------------------------------------------ création / migration
def ensure_admin() -> None:
    """L'administrateur (id 1) existe toujours. L'ancien mot de passe unique (ONDE_PASSWORD)
    devient son mot de passe, une seule fois."""
    now = time.time()
    db.x("CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id)")  # (déconnexion de tous ses appareils)
    db.x("INSERT OR IGNORE INTO users(id, name, role, created) VALUES(?,?,?,?)",
         ADMIN_ID, config.ADMIN_NAME, "admin", now)
    db.x("UPDATE users SET role='admin' WHERE id=?", ADMIN_ID)
    done = db.q1("SELECT v FROM meta WHERE k='users:legacy_pw'")
    if config.PASSWORD and not done:
        row = db.q1("SELECT pw FROM users WHERE id=?", ADMIN_ID)
        if row and not row["pw"]:
            db.x("UPDATE users SET pw=? WHERE id=?", hash_password(config.PASSWORD), ADMIN_ID)
            log.info("profils : l'ancien mot de passe d'Onde devient celui de %s", config.ADMIN_NAME)
        db.x("INSERT OR REPLACE INTO meta(k, v) VALUES('users:legacy_pw', '1')")
    _changed()


def _changed() -> None:
    with _lock:
        _cache.clear()
        _enabled.clear()
        _admin_cache.clear()


def accounts_enabled() -> bool:
    """Connexion obligatoire ? (dès qu'un profil a un mot de passe)"""
    with _lock:
        if _enabled:
            return _enabled[0]
    try:
        on = bool(db.q1("SELECT 1 FROM users WHERE pw IS NOT NULL AND pw <> '' LIMIT 1"))
    except Exception:
        on = bool(config.PASSWORD)
    with _lock:
        _enabled[:] = [on]
    return on


def all_users() -> list[dict]:
    rows = db.q("""SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id) AS sessions,
                          (SELECT COUNT(*) FROM plays p WHERE p.user_id=u.id) AS plays
                   FROM users u ORDER BY u.id""")
    return [{**_public(r), "created": r["created"], "lastSeen": r["last_seen"], "sessions": r["sessions"],
             "plays": r["plays"]} for r in rows]


def get(user_id: int) -> dict | None:
    row = db.q1("SELECT * FROM users WHERE id=?", user_id)
    return _public(row) if row else None


def _texts(**values) -> None:
    """Pseudo, mail, droits, mot de passe : du texte (ou rien), sinon une erreur lisible plutôt qu'un plantage."""
    errors = {"name": "Pseudo invalide.", "email": "Adresse mail invalide.", "role": "Droits inconnus.",
              "password": "Mot de passe invalide."}
    for key, v in values.items():
        if v is not None and not isinstance(v, str):
            raise ValueError(errors.get(key, "Requête invalide."))


def _next_id() -> int:
    """Numéro du prochain profil. Jamais celui d'un profil supprimé (SQLite le réutiliserait) : le
    nouveau venu hériterait sinon de ce qui serait resté au nom de l'ancien."""
    row = db.q1("SELECT v FROM meta WHERE k='users:max_id'")
    try:
        seen = int(row["v"]) if row else 0
    except ValueError:
        seen = 0
    return max(seen, db.q1("SELECT MAX(id) m FROM users")["m"] or 0) + 1


def _prepare(name: str, password: str, role: str, email: str) -> tuple[str, str, str]:
    """Vérifie les champs d'un nouveau profil. Renvoie (pseudo, mail, empreinte du mot de passe) ;
    l'empreinte (scrypt, lente) est calculée ici, hors de toute transaction."""
    _texts(name=name, password=password, role=role, email=email)
    name = " ".join((name or "").split())
    email = (email or "").strip()
    err = validate_name(name) or validate_password(password or "")
    if not err and email and not EMAIL_RE.match(email):
        err = "Adresse mail invalide."
    if not err and role not in ROLES:
        err = "Droits inconnus."
    if err:
        raise ValueError(err)
    return name, email, hash_password(password)


def _insert(name: str, email: str, pw: str, role: str) -> int:
    with db.tx():  # (pseudo libre et numéro du profil : vérifiés sans concurrence)
        if db.q1("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", name):
            raise ValueError("Ce pseudo est déjà pris.")
        new_id = _next_id()
        db.x("INSERT INTO users(id, name, pw, email, role, created) VALUES(?,?,?,?,?,?)",
             new_id, name, pw, email, role, time.time())
        db.x("INSERT OR REPLACE INTO meta(k, v) VALUES('users:max_id', ?)", str(new_id))
    return new_id


def create(name: str, password: str, role: str = "listener", email: str = "") -> dict:
    name, email, pw = _prepare(name, password, role, email)
    new_id = _insert(name, email, pw, role)
    _changed()
    return get(new_id)


def update(user_id: int, *, name: str | None = None, email: str | None = None, role: str | None = None,
           password: str | None = None) -> dict:
    _texts(name=name, email=email, role=role, password=password)
    row = db.q1("SELECT * FROM users WHERE id=?", user_id)
    if not row:
        raise KeyError(user_id)
    if name is not None:
        name = " ".join(name.split())
        err = validate_name(name)
        if err:
            raise ValueError(err)
        if db.q1("SELECT 1 FROM users WHERE name=? COLLATE NOCASE AND id<>?", name, user_id):
            raise ValueError("Ce pseudo est déjà pris.")
        db.x("UPDATE users SET name=? WHERE id=?", name, user_id)
    if email is not None:
        email = email.strip()
        if email and not EMAIL_RE.match(email):
            raise ValueError("Adresse mail invalide.")
        db.x("UPDATE users SET email=? WHERE id=?", email, user_id)
    if role is not None:
        if role not in ROLES:
            raise ValueError("Droits inconnus.")
        if user_id == ADMIN_ID and role != "admin":
            raise ValueError(f"{row['name']} reste administrateur.")
        db.x("UPDATE users SET role=? WHERE id=?", role, user_id)
    if password is not None:
        if password == "" and user_id == ADMIN_ID:
            db.x("UPDATE users SET pw=NULL WHERE id=?", user_id)  # retour à l'accès libre (si personne d'autre)
        else:
            err = validate_password(password)
            if err:
                raise ValueError(err)
            db.x("UPDATE users SET pw=? WHERE id=?", hash_password(password), user_id)
    _changed()
    return get(user_id)


def delete(user_id: int) -> None:
    if user_id == ADMIN_ID:
        raise ValueError("L'administrateur ne peut pas être supprimé.")
    # Tout ce qui est à son nom part avec lui (sessions, liens et demandes de mot de passe : par la
    # base, ON DELETE CASCADE). Toute nouvelle table liée à un profil s'ajoute ici.
    with db.tx():
        # (son numéro ne resservira jamais : voir _next_id)
        db.x("INSERT OR REPLACE INTO meta(k, v) VALUES('users:max_id', ?)", str(_next_id() - 1))
        db.x("DELETE FROM likes WHERE user_id=?", user_id)
        db.x("DELETE FROM playlists WHERE owner_id=?", user_id)
        db.x("DELETE FROM smart_playlists WHERE owner_id=?", user_id)
        db.x("DELETE FROM shares WHERE owner_id=?", user_id)
        if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follows'"):  # (créée par follow.py)
            db.x("DELETE FROM follows WHERE user_id=?", user_id)
        db.x("DELETE FROM meta WHERE k=?", f"resume:{user_id}")
        db.x("DELETE FROM notifications WHERE user_id=?", user_id)
        db.x("""UPDATE asks SET status='refused', decided=?, decided_by='admin', reason='Profil supprimé'
                WHERE user_id=? AND status IN ('pending', 'downloading')""", time.time(), user_id)
        db.x("UPDATE tracks SET added_by=NULL WHERE added_by=?", user_id)  # (titres gardés, hors de tout quota)
        db.x("UPDATE plays SET user_id=0 WHERE user_id=?", user_id)  # (les écoutes restent dans la popularité)
        db.x("DELETE FROM users WHERE id=?", user_id)
    _changed()


# ------------------------------------------------------------------ connexion
def authenticate(name: str, password: str) -> dict | None:
    name = " ".join((name or "").split())
    row = db.q1("SELECT * FROM users WHERE name=? COLLATE NOCASE", name) if name else \
        db.q1("SELECT * FROM users WHERE id=?", ADMIN_ID)  # (ancien écran : mot de passe seul = administrateur)
    if not row or not row["pw"]:
        _dummy_check(password or "")
        return None
    return _public(row) if check_password(password or "", row["pw"]) else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: int, device: str = "", ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.x("INSERT INTO sessions(id, user_id, created, last_used, device, ip) VALUES(?,?,?,?,?,?)",
         _digest(token), user_id, now, now, (device or "")[:160], (ip or "")[:64])
    db.x("UPDATE users SET last_seen=? WHERE id=?", now, user_id)
    return token


def session_user(token: str | None) -> dict | None:
    if not token or len(token) > 100:
        return None
    key = _digest(token)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    row = db.q1("""SELECT u.*, s.last_used FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.id=? AND s.created > ?""", key, now - SESSION_DAYS * 86400)
    user = _public(row) if row else None
    if row and now - (row["last_used"] or 0) > 300:
        db.x("UPDATE sessions SET last_used=? WHERE id=?", now, key)
        db.x("UPDATE users SET last_seen=? WHERE id=?", now, row["id"])
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (user, now + 30)
    return user


def end_session(token: str | None) -> None:
    if token:
        db.x("DELETE FROM sessions WHERE id=?", _digest(token))
        _changed()


def end_all_sessions(user_id: int, keep: str | None = None) -> None:
    db.x("DELETE FROM sessions WHERE user_id=? AND id<>?", user_id, _digest(keep) if keep else "")
    _changed()


INVITE_KEEP_DAYS = 30  # invitations expirées gardées encore un mois (visibles dans la liste de l'admin)
PURGE_EVERY = 86400


def purge() -> dict[str, int]:
    """Ménage : sessions trop vieilles (plus de SESSION_DAYS jours, refusées de toute façon), liens de
    mot de passe expirés, invitations expirées depuis plus d'un mois (et leur lien gardé dans meta)."""
    now = time.time()
    with db.tx():
        sessions = db.x("DELETE FROM sessions WHERE created < ?", now - SESSION_DAYS * 86400).rowcount
        resets = db.x("DELETE FROM resets WHERE expires < ?", now).rowcount
        old = [r["id"] for r in db.q("SELECT id FROM invites WHERE expires < ?", now - INVITE_KEEP_DAYS * 86400)]
        for invite_id in old:
            db.x("DELETE FROM meta WHERE k=?", "invite:" + invite_id[:16])
            db.x("DELETE FROM invites WHERE id=?", invite_id)
    if sessions:
        _changed()
    return {"sessions": sessions, "resets": resets, "invites": len(old)}


_purge_stop = threading.Event()
_purge_thread: threading.Thread | None = None


def _purge_loop() -> None:
    while True:
        try:
            done = purge()
            if any(done.values()):
                log.info("ménage des profils : %s", done)
        except Exception:
            log.exception("ménage des profils")
        if _purge_stop.wait(PURGE_EVERY):
            return


def start_background() -> None:
    """Ménage au démarrage, puis une fois par jour."""
    global _purge_thread
    _purge_stop.clear()
    if _purge_thread is None or not _purge_thread.is_alive():
        _purge_thread = threading.Thread(target=_purge_loop, name="onde-users-purge", daemon=True)
        _purge_thread.start()


def stop() -> None:
    _purge_stop.set()


def cookie_token(headers) -> str | None:
    for k, v in headers:
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                n, _, value = part.strip().partition("=")
                if n == COOKIE:
                    return value
    return None


# ------------------------------------------------------------------ droits
_ADMIN_ONLY = re.compile(r"^/api/(dev/(logs|http|debug|info)|users|invites|activity|tidy|settings(?!/password)|"
                         r"library/(export|scan|covers|artists|duplicates|quality)|tracks/batch|follow-check|remote(?!/url$)|align/install|backup|update|dashboard|pwreqs|asks/\d+/(approve|refuse|ai))")
_LISTENER_WRITE = [
    ("POST", r"/api/tracks/\d+/played"), ("PUT", r"/api/liked/\d+"), ("DELETE", r"/api/liked/\d+"),
    ("*", r"/api/playlists(/\d+(/.*)?)?"), ("*", r"/api/smart(/.*)?"),
    ("POST", r"/api/sync/broadcast"), ("POST", r"/api/pi/cmd"), ("POST", r"/api/dev/client"),
    ("*", r"/api/me(/.*)?"), ("POST", r"/api/settings/password"), ("*", r"/api/jam(/.*)?"),
    ("POST", r"/api/resume"), ("POST", r"/api/asks"), ("DELETE", r"/api/asks/\d+"),
]
_MEMBER_WRITE = [("PUT", r"/api/follows/.+"), ("DELETE", r"/api/follows/.+"), ("POST", r"/api/upload"), ("POST", r"/api/import-url"), ("POST", r"/api/import-playlist"),
                 ("POST", r"/api/playlists/import")]
_RANK = {"listener": 0, "member": 1, "admin": 2}


def allowed(role: str, method: str, path: str) -> bool:
    if role == "admin":
        return True
    if role not in _RANK:  # (visiteur sans session : seulement les pages publiques, vues par main.Guard)
        return False
    if _ADMIN_ONLY.match(path):
        return False
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    rules = _LISTENER_WRITE + (_MEMBER_WRITE if _RANK.get(role, 0) >= 1 else [])
    for m, pattern in rules:
        if (m == "*" or m == method) and re.fullmatch(pattern, path):
            return True
    return False


# ------------------------------------------------------------------ invitations
def create_invite(role: str, days: float = 7, max_uses: int = 1, note: str = "") -> str:
    if role not in ("member", "listener"):
        raise ValueError("Une invitation donne les droits « écoute + import » ou « écoute seule ».")
    token = secrets.token_urlsafe(18)
    now = time.time()
    db.x("INSERT INTO invites(id, role, created, expires, max_uses, created_by, note) VALUES(?,?,?,?,?,?,?)",
         _digest(token), role, now, now + max(0.04, min(float(days), 365)) * 86400, max(1, min(int(max_uses), 50)),
         uid(), (note or "")[:80])
    # Le jeton est gardé en clair dans meta : c'est ce qui permet à l'admin de réafficher le lien
    # (liste des invitations). Seule l'empreinte sert à vérifier ; l'entrée part avec l'invitation
    # (delete_invite, ou purge() un mois après son expiration).
    db.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", "invite:" + _digest(token)[:16], token)
    return token


def invite_info(token: str) -> dict | None:
    if not token or len(token) > 60:
        return None
    r = db.q1("SELECT * FROM invites WHERE id=?", _digest(token))
    if not r or r["expires"] < time.time() or r["uses"] >= r["max_uses"]:
        return None
    return {"role": r["role"], "expires": r["expires"], "left": r["max_uses"] - r["uses"]}


def use_invite(token: str, name: str, password: str, email: str = "", accepted: bool = False) -> dict:
    """Crée le profil d'un invité. Tout se fait dans une transaction : une invitation à usage unique
    ne sert qu'une fois, même avec deux inscriptions simultanées (et ne s'use pas si le pseudo est pris)."""
    gone = "Invitation expirée ou déjà utilisée : demande un nouveau lien."
    info = invite_info(token) if isinstance(token, str) else None
    if not info:
        raise ValueError(gone)
    name, email, pw = _prepare(name, password, info["role"], email)
    with db.tx():
        cur = db.x("UPDATE invites SET uses = uses + 1 WHERE id=? AND uses < max_uses AND expires > ?",
                   _digest(token), time.time())
        if cur.rowcount == 0:  # (utilisée entre-temps par quelqu'un d'autre)
            raise ValueError(gone)
        new_id = _insert(name, email, pw, info["role"])  # (pseudo pris : l'invitation n'est pas comptée)
        if accepted:
            terms.accept(new_id)
    _changed()
    return get(new_id)


def invites() -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM invites ORDER BY created DESC LIMIT 50"):
        tok = db.q1("SELECT v FROM meta WHERE k=?", "invite:" + r["id"][:16])
        out.append({"id": r["id"][:16], "role": r["role"], "created": r["created"], "expires": r["expires"],
                    "uses": r["uses"], "maxUses": r["max_uses"], "note": r["note"],
                    "active": r["expires"] > time.time() and r["uses"] < r["max_uses"],
                    "token": tok["v"] if tok else None})
    return out


def delete_invite(short_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{16}", short_id or ""):
        raise KeyError(short_id)
    db.x("DELETE FROM invites WHERE substr(id, 1, 16)=?", short_id)
    db.x("DELETE FROM meta WHERE k=?", "invite:" + short_id)


# ------------------------------------------------------------------ mot de passe oublié (par mail)
def mail_available() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def send_mail(to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, to, subject
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=20) as s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
            s.starttls(context=ctx)
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)


def start_reset(email: str, base_url: str) -> None:
    """Envoie un lien de réinitialisation (valable 1 h) si un profil a ce mail. Ne dit jamais si le mail existe."""
    email = (email or "").strip()
    if not email or not mail_available():
        return
    row = db.q1("SELECT id, name FROM users WHERE email=? COLLATE NOCASE", email)
    if not row:
        return
    token = secrets.token_urlsafe(24)
    db.x("DELETE FROM resets WHERE user_id=?", row["id"])  # un seul lien actif : le précédent est annulé
    db.x("INSERT INTO resets(id, user_id, expires) VALUES(?,?,?)", _digest(token), row["id"], time.time() + 3600)
    link = f"{base_url.rstrip('/')}/reset/{token}"
    send_mail(email, "Onde — nouveau mot de passe",
              f"Bonjour {row['name']},\n\nPour choisir un nouveau mot de passe Onde, ouvre ce lien (valable 1 heure) :\n"
              f"{link}\n\nSi ce n'est pas toi, ignore ce message.")
    log_activity("reset_mail", row["name"], user_id=row["id"], name=row["name"])


ADMIN_RESET_HOURS = 24


def admin_reset_link(user_id: int) -> tuple[str, float]:
    """Lien de réinitialisation créé par l'admin (sans serveur de mail), valable 24 h, à usage unique.
    Un seul lien actif par profil : le précédent est annulé. Renvoie (jeton, expiration)."""
    if not db.q1("SELECT 1 FROM users WHERE id=?", user_id):
        raise KeyError(user_id)
    token = secrets.token_urlsafe(24)
    expires = time.time() + ADMIN_RESET_HOURS * 3600
    db.x("DELETE FROM resets WHERE user_id=?", user_id)
    db.x("INSERT INTO resets(id, user_id, expires) VALUES(?,?,?)", _digest(token), user_id, expires)
    db.x("UPDATE pwreqs SET status='done' WHERE user_id=? AND status='pending'", user_id)  # sa demande est traitée
    return token, expires


PWREQ_COOLDOWN = 600  # secondes : une même personne ne prévient pas l'admin en boucle


def request_reset(name: str) -> bool:
    """« Mot de passe oublié » sans serveur de mail : prévient l'admin (notification + tableau de bord).
    Ne dit jamais à l'appelant si le pseudo existe. L'admin lui-même n'est pas concerné."""
    from . import notify  # (import tardif : notify n'a pas besoin de ce module)
    name = " ".join((name or "").split())[:40]
    if not name:
        return False
    row = db.q1("SELECT id, name FROM users WHERE name=? COLLATE NOCASE", name) \
        or db.q1("SELECT id, name FROM users WHERE email=? COLLATE NOCASE AND email!=''", name)  # (le mail marche aussi)
    if not row or row["id"] == 1:
        return False
    now = time.time()
    last = db.q1("SELECT created FROM pwreqs WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", row["id"])
    if last and now - last["created"] < PWREQ_COOLDOWN:
        return True
    db.x("DELETE FROM pwreqs WHERE user_id=? AND status='pending'", row["id"])
    db.x("INSERT INTO pwreqs(user_id, created) VALUES(?,?)", row["id"], now)
    db.x("DELETE FROM pwreqs WHERE status!='pending' AND created < ?", now - 7 * 86400)
    notify.add("password", f"{row['name']} a oublié son mot de passe", "Crée-lui un lien pour en choisir un nouveau.", "#/dashboard")
    log_activity("pwreq", row["name"], user_id=row["id"], name=row["name"])
    return True


def pwreqs() -> list[dict]:
    rows = db.q("""SELECT p.id, p.user_id, p.created, u.name FROM pwreqs p JOIN users u ON u.id = p.user_id
                   WHERE p.status='pending' ORDER BY p.id DESC""")
    return [{"id": r["id"], "userId": r["user_id"], "name": r["name"], "created": r["created"]} for r in rows]


def dismiss_pwreq(req_id: int) -> None:
    db.x("DELETE FROM pwreqs WHERE id=?", req_id)


def reset_valid(token: str) -> bool:
    r = db.q1("SELECT expires FROM resets WHERE id=?", _digest(token or ""))
    return bool(r and r["expires"] >= time.time())


def finish_reset(token: str, password: str) -> dict:
    r = db.q1("SELECT * FROM resets WHERE id=?", _digest(token or ""))
    if not r or r["expires"] < time.time():
        raise ValueError("Lien expiré ou déjà utilisé : demande un nouveau lien à l'admin.")
    # (jamais de mot de passe vide ici : pour l'admin, update() l'enlèverait et rouvrirait l'accès libre)
    err = validate_password(password) if isinstance(password, str) else "Choisis un nouveau mot de passe."
    if err:
        raise ValueError(err)
    user = update(r["user_id"], password=password)
    db.x("DELETE FROM resets WHERE user_id=?", r["user_id"])
    db.x("UPDATE pwreqs SET status='done' WHERE user_id=? AND status='pending'", r["user_id"])
    end_all_sessions(r["user_id"])
    return user


# ------------------------------------------------------------------ journal d'activité
ACTIONS = {
    "login": "connexion", "logout": "déconnexion", "login_failed": "connexion refusée", "signup": "profil créé (invitation)",
    "delete_track": "titre supprimé", "import": "import", "download": "téléchargement", "upload": "fichiers envoyés",
    "settings": "réglages modifiés", "user": "profil modifié", "invite": "invitation créée", "password": "mot de passe changé",
    "reset_mail": "mail de mot de passe envoyé", "reset_link": "lien de mot de passe créé", "pwreq": "mot de passe oublié (demande)", "backup": "sauvegarde", "trash": "corbeille", "update": "mise à jour",
    "ask": "demande à l'admin", "terms": "conditions acceptées", "share": "playlist partagée", "import_error": "erreur d'import",
}


def log_activity(action: str, detail: str = "", ip: str = "", user_id: int | None = None, name: str | None = None) -> None:
    try:
        u = current()
        db.x("INSERT INTO activity(ts, user_id, name, action, detail, ip) VALUES(?,?,?,?,?,?)",
             time.time(), u["id"] if user_id is None else user_id, u["name"] if name is None else name,
             action, (detail or "")[:300], (ip or "")[:64])
        if db.q1("SELECT COUNT(*) n FROM activity")["n"] > 20000:
            db.x("DELETE FROM activity WHERE id IN (SELECT id FROM activity ORDER BY id LIMIT 2000)")
    except Exception:
        log.exception("journal d'activité")


def activity(limit: int = 200, user_id: int | None = None, with_plays: bool = True) -> list[dict]:
    args: list = []
    where = ""
    if user_id:
        where, args = " WHERE user_id=?", [user_id]
    out = [{"ts": r["ts"], "userId": r["user_id"], "name": r["name"], "action": r["action"],
            "label": ACTIONS.get(r["action"], r["action"]), "detail": r["detail"], "ip": r["ip"]}
           for r in db.q(f"SELECT * FROM activity{where} ORDER BY id DESC LIMIT ?", *args, limit)]
    if with_plays:
        names = {r["id"]: r["name"] for r in db.q("SELECT id, name FROM users")}
        pw = " WHERE p.user_id=?" if user_id else ""
        for r in db.q(f"""SELECT p.played_at, p.user_id, t.title, t.artist FROM plays p JOIN tracks t ON t.id=p.track_id
                          {pw} ORDER BY p.played_at DESC LIMIT ?""", *args, limit):
            out.append({"ts": r["played_at"], "userId": r["user_id"], "name": names.get(r["user_id"], "?"),
                        "action": "play", "label": "écoute", "detail": f"{r['title']} — {r['artist']}", "ip": ""})
        out.sort(key=lambda x: -x["ts"])
    return out[:limit]
