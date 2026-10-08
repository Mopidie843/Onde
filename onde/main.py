"""Onde — serveur musical local (FastAPI)."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import html as htmllib
import io
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from . import api_models as M
from . import asks, autoupdate, dashboard, quota, share, terms
from . import align, backup, clean, jam, updater, first_run, follow, users, config, covers, db, devlog, discover, library, lyrics, net, netinfo, notify, remote, restart, settings, stats, tls, transcode, tidy
from .pi_player import player
from .routes_import import router as import_router
from .routes_maintenance import router as maintenance_router
from .routes_pi import router as pi_router
from .routes_playlists import _add_to_playlist
from .routes_playlists import router as playlists_router
from .routes_sync import router as sync_router
from .web import STREAMS_CLOSING, J, _404, _cover_from_request, _disposition, _flag, _playlist, _read_limited, _reindex, _stats, _track_row, shared

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("onde")
devlog.install()  # journal en mémoire pour la console développeur (F7)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    backup.apply_pending_restore()  # (restauration demandée avant le redémarrage)
    db.init()
    shared.loop = asyncio.get_running_loop()
    devlog.attach_uvicorn()
    player.attach_loop(shared.loop)
    library.start_background()
    first_run.run()  # playlist de départ, en arrière-plan (une seule fois)
    follow.start_background()  # nouveautés des artistes suivis
    backup.start_background()  # sauvegarde automatique de la base
    asks.start_background()  # demandes de titres : décision auto au bout de 5 min
    autoupdate.start_background()  # nouvelle version ? installée toute seule (si une source est choisie)
    lyrics.start_background()
    if align.on_lyrics not in lyrics.listeners:
        lyrics.listeners.append(align.on_lyrics)  # paroles brutes trouvées → calées sur la voix
    align.start_background()
    yield
    autoupdate.stop()
    asks.stop()
    backup.stop()
    follow.stop()
    first_run.stop()
    align.stop()
    lyrics.stop()
    library.stop()
    player.shutdown()
    net.close()
    library.jobs.shutdown(wait=False, cancel_futures=True)
    library.bg.shutdown(wait=False, cancel_futures=True)
    db.optimize()


app = FastAPI(title="Onde", version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")


def _static_version() -> str:
    h = hashlib.sha1(config.VERSION.encode())
    for p in sorted(config.STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(f"{p.name}{p.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:10]


STATIC_V = _static_version()


class Static(StaticFiles):
    """Fichiers statiques revalidés (ETag) : mises à jour visibles immédiatement."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", Static(directory=config.STATIC_DIR), name="static")


# ----------------------------------------------------- mot de passe + gzip
def _secret() -> bytes:
    f = config.DATA_DIR / "secret.key"
    if not f.exists():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        f.write_bytes(secrets.token_bytes(32))
        os.chmod(f, 0o600)
    return f.read_bytes()


def _same(a: str, b: str) -> bool:
    """Comparaison à temps constant. Sur des octets : `compare_digest` refuse le texte non ASCII
    (un mot de passe accentué, « café1234 », provoquait sinon une erreur 500 et bloquait tout)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


NO_GZIP = re.compile(r"^/api/(tracks/\d+/(stream|download)|albums/\d+/download|playlists/\d+/download"
                     r"|cover/|pi/events|sync/events|notifications|upload)|^/static/icons/")
PUBLIC = ("/login", "/logout", "/invite/", "/forgot", "/reset/", "/jam/", "/api/jam/", "/share/", "/api/share/", "/conditions", "/api/terms", "/api/app/", "/api/auth/", "/static/icons/", "/manifest.webmanifest", "/favicon",
          "/onde.crt", "/onde-cert.pem", "/sw.js")


class Guard:
    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=700, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not scope["path"].startswith("/api/"):
            return await self._serve(scope, receive, send)
        # requêtes de l'API : notées pour la console développeur (F7), erreurs serveur au journal
        t0 = time.perf_counter()
        status, size = [0], [0]

        async def send_and_note(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            elif message["type"] == "http.response.body":
                size[0] += len(message.get("body") or b"")
            await send(message)
        try:
            await self._serve(scope, receive, send_and_note)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            try:
                ip = client_ip(Request(scope))
            except Exception:
                ip = "?"
            path, query = scope["path"], scope.get("query_string", b"").decode("latin-1")
            devlog.http(scope.get("method", "GET"), path, query, status[0] or 500, ms, ip, size[0])
            if (status[0] or 500) >= 500:
                log.error("HTTP %s %s %s (%.0f ms, %s)", status[0] or 500, scope.get("method"), path, ms, ip)

    async def _serve(self, scope, receive, send):
        path = scope["path"]
        public = path.startswith(PUBLIC)
        if users.accounts_enabled():
            user = await run_in_threadpool(users.session_user, users.cookie_token(scope.get("headers", [])))
            if user is None and not public:
                if path.startswith("/api/"):
                    resp = JSONResponse({"detail": "Connexion requise"}, status_code=401)
                else:
                    resp = RedirectResponse("/login", status_code=303)
                return await resp(scope, receive, send)
        else:
            user = None  # accès libre : l'administrateur
        user = user or users.admin_cached()
        if not public and not users.allowed(user["role"], scope.get("method", "GET"), path):
            resp = JSONResponse({"detail": "Ton profil n'a pas le droit de faire ça (demande à l'administrateur)."},
                                status_code=403)
            return await resp(scope, receive, send)
        with users.use(user):
            if NO_GZIP.match(path):
                return await self.app(scope, receive, send)
            return await self.gzip(scope, receive, send)


app.add_middleware(Guard)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
_index_cache: dict[str, str] = {}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    html = _index_cache.get("index")
    if html is None:
        html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{V}}", STATIC_V)
        _index_cache["index"] = html
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    data = {
        "name": "Onde", "short_name": "Onde", "start_url": "/", "display": "standalone",
        "background_color": "#08080b", "theme_color": "#08080b", "lang": "fr",
        "description": "Ta musique, chez toi.",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json")


_sw_cache: dict[str, str] = {}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Service worker (écoute hors connexion) : garde l'appli sur l'appareil. Servi à la racine pour
    couvrir toutes les pages ; la liste des fichiers à garder et la version sont injectées ici."""
    js = _sw_cache.get("sw")
    if js is None:
        assets = ["/", "/manifest.webmanifest", f"/static/css/app.css?v={STATIC_V}", f"/static/js/app.js?v={STATIC_V}"]
        for sub in ("js", "icons"):
            for f in sorted((config.STATIC_DIR / sub).iterdir()):
                if f.is_file() and f.suffix in (".js", ".png") and f.name != "app.js":
                    assets.append(f"/static/{sub}/{f.name}")
        js = ((config.STATIC_DIR / "sw.js").read_text(encoding="utf-8")
              .replace("__VERSION__", STATIC_V).replace("__ASSETS__", json.dumps(assets)))
        _sw_cache["sw"] = js
    return Response(js, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


AUTH_CSS = """:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;background:radial-gradient(1200px 600px at 20% -10%,#3a2a78 0%,transparent 60%),#08080b;
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#f4f4f6;padding:16px}
form{width:min(380px,100%);background:#131318;border:1px solid #23232b;border-radius:16px;padding:28px;display:grid;gap:14px;box-shadow:0 20px 60px #0008}
img{width:56px;height:56px;border-radius:14px}h1{margin:0;font-size:24px}p{margin:0;color:#a3a3ad;font-size:14px}
label{display:grid;gap:6px;font-size:13px;color:#c9c9d1}
input{width:100%;padding:13px 15px;border-radius:10px;border:1px solid #2c2c36;background:#0d0d11;color:inherit;font:inherit;outline:none}
input:focus{border-color:#a48bff;box-shadow:0 0 0 3px #a48bff33}
button{padding:14px;border:0;border-radius:999px;background:#a48bff;color:#0d0b14;font:inherit;font-weight:700;cursor:pointer}
.err{color:#ff8f8f}.ok{color:#8fe3a8}a{color:#a48bff;font-size:14px;text-align:center}
label.chk{display:flex;gap:10px;align-items:flex-start;font-size:13px;line-height:1.4}label.chk input{width:18px;height:18px;flex:none;margin:1px 0 0;accent-color:#a48bff}label.chk a{font-size:13px}"""


def _auth_page(title: str, intro: str, fields: str, button: str, action: str, msg: str = "", foot: str = "",
               status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Onde — {htmllib.escape(title)}</title>
<link rel="icon" href="/static/icons/icon-192.png"><meta name="theme-color" content="#08080b"><style>{AUTH_CSS}</style></head>
<body><form method="post" action="{htmllib.escape(action)}"><img src="/static/icons/icon-192.png" alt="">
<h1>{htmllib.escape(title)}</h1><p>{intro}</p>{msg}{fields}<button type="submit">{htmllib.escape(button)}</button>{foot}</form></body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _err(text: str) -> str:
    return f'<p class="err">{htmllib.escape(text)}</p>'


_F_NAME = '<label>Pseudo<input name="name" autocomplete="username" required maxlength="32" autofocus></label>'
_F_PWD = '<label>Mot de passe<input type="password" name="password" autocomplete="current-password" required maxlength="128"></label>'
_F_NEWPWD = ('<label>Mot de passe (6 caractères ou plus)<input type="password" name="password" autocomplete="new-password" '
             'required minlength="6" maxlength="128"></label>')
_F_TERMS = ('<label class="chk"><input type="checkbox" name="terms" value="1" required><span>J\'ai lu et j\'accepte les '
            '<a href="/conditions" target="_blank">conditions d\'utilisation</a> : usage privé, je ne partage rien à personne.</span></label>')
_F_MAIL = '<label>Mail (facultatif, pour retrouver ton mot de passe)<input type="email" name="email" autocomplete="email" maxlength="254"></label>'


def _login_page(msg: str = "", status: int = 200) -> HTMLResponse:
    foot = '<a href="/forgot">Mot de passe oublié ?</a>'
    return _auth_page("Onde", "Connecte-toi avec ton pseudo et ton mot de passe.", _F_NAME + _F_PWD,
                      "Se connecter", "/login", msg, foot, status)


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 8192:
        return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _with_session(resp, request: Request, user: dict):
    token = users.new_session(user["id"], request.headers.get("user-agent", ""), client_ip(request))
    resp.set_cookie(users.COOKIE, token, max_age=users.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=_secure(request))
    return resp


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    if not users.accounts_enabled():
        return RedirectResponse("/")
    return _login_page()


# Protection contre le forçage du mot de passe (indispensable dès qu'Onde est joignable depuis Internet) :
# après 8 essais ratés, l'adresse est bloquée 15 minutes ; chaque essai raté ralentit en plus la réponse.
LOGIN_MAX_FAILS = 8
LOGIN_LOCK_SECONDS = 15 * 60
_login_fails: dict[str, list[float]] = {}  # adresse -> [échecs, fin du blocage, dernier échec]
_login_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """Adresse du visiteur. Derrière un relais local (Tailscale Serve/Funnel, Cloudflare Tunnel…) la
    connexion arrive de 127.0.0.1 : on lit alors l'adresse transmise par le relais (la plus à droite,
    celle qu'il a ajoutée lui-même)."""
    peer = request.client.host if request.client else "?"
    if peer in ("127.0.0.1", "::1", "localhost"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip() or peer
    return peer


def login_blocked(ip: str) -> int:
    """Secondes restantes de blocage pour cette adresse (0 : libre)."""
    with _login_lock:
        rec = _login_fails.get(ip)
        if rec and rec[1] > time.time():
            return int(rec[1] - time.time()) + 1
        return 0


def login_failed(ip: str) -> int:
    now = time.time()
    with _login_lock:
        if len(_login_fails) > 2000:  # mémoire bornée
            for k in [k for k, v in _login_fails.items() if v[1] < now and now - v[2] > 3600]:
                del _login_fails[k]
        rec = _login_fails.setdefault(ip, [0, 0.0, now])
        if rec[1] and rec[1] <= now:  # blocage terminé : on repart de zéro
            rec[0], rec[1] = 0, 0.0
        rec[0] += 1
        rec[2] = now
        if rec[0] >= LOGIN_MAX_FAILS:
            rec[1] = now + LOGIN_LOCK_SECONDS
        return int(rec[0])


def login_ok(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    ip = client_ip(request)
    wait = login_blocked(ip)
    if wait:
        resp = _login_page(_err(f"Trop d'essais. Réessaie dans {wait // 60 + 1} min."), 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    form = await _form(request)
    user = await run_in_threadpool(users.authenticate, form.get("name", ""), form.get("password", ""))
    if user:
        login_ok(ip)
        with users.use(user):
            await run_in_threadpool(users.log_activity, "login", request.headers.get("user-agent", "")[:120], ip)
        return _with_session(RedirectResponse("/", status_code=303), request, user)
    fails = login_failed(ip)
    await run_in_threadpool(users.log_activity, "login_failed", form.get("name", "")[:40], ip, 0, form.get("name", "")[:40])
    await asyncio.sleep(min(10, 1 + fails * 0.5))  # ralentit le forçage
    return _login_page(_err("Pseudo ou mot de passe incorrect."), 401)


@app.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
def logout(request: Request):
    token = users.cookie_token(request.scope.get("headers", []))
    user = users.session_user(token)
    if user:
        with users.use(user):
            users.log_activity("logout", "", client_ip(request))
    users.end_session(token)
    resp = RedirectResponse("/login" if users.accounts_enabled() else "/", status_code=303)
    resp.delete_cookie(users.COOKIE)
    return resp


# ------------------------------------------------------------------ connexion pour l'appli Android (JSON)
@app.get("/api/auth/state")
def auth_state(request: Request):
    user = users.session_user(users.cookie_token(request.scope.get("headers", [])))
    return {"accounts": users.accounts_enabled(), "mail": users.mail_available(), "version": config.VERSION,
            "me": user if user or not users.accounts_enabled() else None}


@app.get("/api/auth/invite/{token}")
def auth_invite(token: str):
    info = users.invite_info(token)
    return {"valid": bool(info), "role": info["role"] if info else None,
            "roleLabel": users.ROLES[info["role"]] if info else None}


async def _json(request: Request) -> dict:
    try:
        body = json.loads((await request.body())[:8192] or b"{}")
    except ValueError:
        raise HTTPException(400, "Requête invalide")
    if not isinstance(body, dict):
        raise HTTPException(400, "Requête invalide")
    return body


@app.post("/api/auth/login")
async def auth_login(request: Request):
    ip = client_ip(request)
    wait = login_blocked(ip)
    if wait:
        raise HTTPException(429, f"Trop d'essais. Réessaie dans {wait // 60 + 1} min.")
    body = await _json(request)
    user = await run_in_threadpool(users.authenticate, str(body.get("name") or ""), str(body.get("password") or ""))
    if not user:
        fails = login_failed(ip)
        await run_in_threadpool(users.log_activity, "login_failed", str(body.get("name") or "")[:40], ip, 0,
                                str(body.get("name") or "")[:40])
        await asyncio.sleep(min(10, 1 + fails * 0.5))
        raise HTTPException(401, "Pseudo ou mot de passe incorrect.")
    login_ok(ip)
    with users.use(user):
        await run_in_threadpool(users.log_activity, "login", "appli Android", ip)
    return _with_session(JSONResponse({"ok": True, "user": user}), request, user)


@app.post("/api/auth/signup")
async def auth_signup(request: Request):
    ip = client_ip(request)
    if login_blocked(ip):
        raise HTTPException(429, "Trop d'essais, réessaie plus tard.")
    body = await _json(request)
    token = str(body.get("token") or "").strip()
    m = re.search(r"/invite/([A-Za-z0-9_\-]+)", token)  # (lien complet collé)
    if m:
        token = m.group(1)
    if not body.get("terms"):
        raise HTTPException(400, "Il faut accepter les conditions d'utilisation (mets l'appli à jour si la case n'apparaît pas).")
    try:
        user = await run_in_threadpool(users.use_invite, token, str(body.get("name") or ""),
                                       str(body.get("password") or ""), str(body.get("email") or ""), True)
    except ValueError as exc:
        login_failed(ip)
        raise HTTPException(400, str(exc))
    with users.use(user):
        await run_in_threadpool(users.log_activity, "signup", f"{users.ROLES[user['role']]} (appli)", ip)
    return _with_session(JSONResponse({"ok": True, "user": user}), request, user)


@app.post("/api/auth/forgot")
async def auth_forgot(request: Request):
    ip = client_ip(request)
    if login_blocked(ip):
        raise HTTPException(429, "Trop d'essais, réessaie plus tard.")
    login_failed(ip)
    body = await _json(request)
    who = str(body.get("name") or body.get("email") or "")
    if not users.mail_available():  # pas de mail : l'admin est prévenu (notification), on ne révèle rien sur le pseudo
        try:
            await run_in_threadpool(users.request_reset, who)
        except Exception as exc:
            log.warning("demande de mot de passe : %s", exc)
        return {"ok": True, "admin": True}
    base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    try:
        await run_in_threadpool(users.start_reset, who, base)
    except Exception as exc:
        log.warning("mail de mot de passe non envoyé : %s", exc)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = users.cookie_token(request.scope.get("headers", []))
    users.end_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(users.COOKIE)
    return resp


# ------------------------------------------------------------------ invitation : créer son profil
def _invite_page(token: str, msg: str = "", status: int = 200, base: str = "") -> HTMLResponse:
    info = users.invite_info(token)
    if not info:
        return _auth_page("Invitation", "Ce lien d'invitation a expiré ou a déjà servi. Demande un nouveau lien.",
                          "", "Retour", "/login", status=410)
    app_link = ""
    if base:  # l'appli Android s'ouvre directement sur « créer mon compte » (lien onde://)
        q = urllib.parse.urlencode({"server": base, "token": token})
        app_link = (f'<a href="onde://join?{htmllib.escape(q)}">📱 J\'ai l\'appli Onde (Android) : '
                    f'créer mon compte dans l\'appli</a>')
    return _auth_page("Bienvenue sur Onde", f"Crée ton profil ({htmllib.escape(users.ROLES[info['role']].lower())}).",
                      _F_NAME + _F_NEWPWD + _F_MAIL + _F_TERMS, "Créer mon profil", f"/invite/{urllib.parse.quote(token)}", msg,
                      foot=app_link, status=status)


@app.get("/invite/{token}", response_class=HTMLResponse, include_in_schema=False)
def invite_page(token: str, request: Request):
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return _invite_page(token, base=f"{scheme}://{request.headers.get('host', '')}")


@app.post("/invite/{token}", include_in_schema=False)
async def invite_signup(token: str, request: Request):
    ip = client_ip(request)
    if login_blocked(ip):
        return _invite_page(token, _err("Trop d'essais, réessaie plus tard."), 429)
    form = await _form(request)
    if not form.get("terms"):
        return _invite_page(token, _err("Coche la case : il faut accepter les conditions d'utilisation."), 400)
    try:
        user = await run_in_threadpool(users.use_invite, token, form.get("name", ""), form.get("password", ""),
                                       form.get("email", ""), True)
    except ValueError as exc:
        login_failed(ip)
        return _invite_page(token, _err(str(exc)), 400)
    with users.use(user):
        await run_in_threadpool(users.log_activity, "signup", users.ROLES[user["role"]], ip)
    return _with_session(RedirectResponse("/", status_code=303), request, user)


# ------------------------------------------------------------------ mot de passe oublié (si un serveur de mail est configuré)
@app.get("/forgot", response_class=HTMLResponse, include_in_schema=False)
def forgot_page():
    if not users.mail_available():
        return _auth_page("Mot de passe oublié", "Entre ton pseudo : l'administrateur reçoit une notification et t'envoie un lien "
                          "pour choisir un nouveau mot de passe.",
                          '<label>Pseudo<input name="name" autocomplete="username" required autofocus maxlength="40"></label>',
                          "Prévenir l'admin", "/forgot")
    return _auth_page("Mot de passe oublié", "Entre le mail de ton profil : tu recevras un lien pour choisir un nouveau mot de passe.",
                      '<label>Mail<input type="email" name="email" required autofocus></label>', "Recevoir le lien", "/forgot")


@app.post("/forgot", include_in_schema=False)
async def forgot(request: Request):
    ip = client_ip(request)
    if login_blocked(ip):
        return _auth_page("Mot de passe oublié", "Trop d'essais, réessaie plus tard.", "", "Retour", "/login", status=429)
    login_failed(ip)  # (compte comme un essai : pas d'envoi de mails en rafale)
    form = await _form(request)
    if not users.mail_available():  # pas de mail : on prévient l'admin (jamais d'info sur l'existence du pseudo)
        try:
            await run_in_threadpool(users.request_reset, form.get("name", ""))
        except Exception as exc:
            log.warning("demande de mot de passe : %s", exc)
        return _auth_page("Mot de passe oublié", "C'est noté : si ce pseudo existe, l'administrateur vient d'être prévenu. "
                          "Il t'enverra un lien pour choisir un nouveau mot de passe.", "", "Retour à la connexion", "/login")
    base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    try:
        await run_in_threadpool(users.start_reset, form.get("email", ""), base)
    except Exception as exc:
        log.warning("mail de mot de passe non envoyé : %s", exc)
    return _auth_page("Mot de passe oublié", "Si ce mail correspond à un profil, un lien vient d'être envoyé (vérifie les spams).",
                      "", "Retour à la connexion", "/login")


@app.get("/reset/{token}", response_class=HTMLResponse, include_in_schema=False)
def reset_page(token: str):
    if not users.reset_valid(token):
        return _auth_page("Lien expiré", "Ce lien n'est plus valable (expiré ou déjà utilisé). Demande à l'admin de t'en envoyer un nouveau.",
                          "", "Retour à la connexion", "/login", status=410)
    return _auth_page("Nouveau mot de passe", "Choisis ton nouveau mot de passe.", _F_NEWPWD, "Enregistrer",
                      f"/reset/{urllib.parse.quote(token)}")


@app.post("/reset/{token}", include_in_schema=False)
async def reset_password(token: str, request: Request):
    form = await _form(request)
    try:
        user = await run_in_threadpool(users.finish_reset, token, form.get("password", ""))
    except ValueError as exc:
        return _auth_page("Nouveau mot de passe", "Choisis ton nouveau mot de passe.", _F_NEWPWD, "Enregistrer",
                          f"/reset/{urllib.parse.quote(token)}", _err(str(exc)), status=400)
    return _with_session(RedirectResponse("/", status_code=303), request, user)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# API — bibliothèque
# --------------------------------------------------------------------------- #
def lan_urls() -> list[str]:
    """Adresses où ouvrir Onde (QR code des Réglages) : point d'accès Wi-Fi d'abord s'il est actif,
    puis le réseau principal. Toutes les interfaces : en point d'accès sans box, il n'y a souvent
    pas de route par défaut."""
    scheme = "https" if config.HTTPS else "http"
    urls = [f"{scheme}://{ip}:{config.PORT}" for ip in netinfo.lan_ips()]
    urls.append(f"{scheme}://{socket.gethostname()}.local:{config.PORT}")
    return urls


def _cert_url() -> str | None:
    """Lien http:// (sans avertissement) vers l'autorité locale, pour le QR code des Réglages."""
    if not tls.CA_CERT.is_file():
        return None
    host = next((u.split("://", 1)[1] for u in lan_urls()), f"localhost:{config.PORT}")
    return f"http://{host}/onde.crt" if config.HTTPS else "/onde.crt"


@app.get("/api/info")
def info():
    return {
        "version": config.VERSION, "musicDir": str(config.MUSIC_DIR),
        "extraDirs": [str(p) for p in config.EXTRA_DIRS], "pi": player.available,
        "stats": _stats(), "password": users.accounts_enabled(), "maxUploadMb": config.MAX_UPLOAD_MB,
        "me": users.current(), "roles": users.ROLES,
        "formats": sorted(config.AUDIO_TYPES), "settings": settings.all_settings(),
        "ffmpeg": bool(config.FFMPEG), "watching": library.status["watching"], "urls": lan_urls(),
        "https": config.HTTPS, "tlsAvailable": tls.available(), "canRestart": restart.available(),
        "certUrl": _cert_url(),
    }


@app.get("/api/notifications")
async def notifications(after: str = "", wait: float = 0):
    """Événements d'Onde depuis le numéro `after` (`latest` ou vide : juste le dernier numéro).
    `wait` (secondes, 30 au plus) : attend qu'il se passe quelque chose avant de répondre."""
    try:
        start = int(after) if after.strip() not in ("", "latest") else None
    except ValueError:
        start = None
    deadline = time.monotonic() + max(0.0, min(notify.MAX_WAIT, wait))
    admin = users.is_admin()
    while True:
        res = notify.feed(start, admin)
        if res["events"] or res["reset"] or time.monotonic() >= deadline or STREAMS_CLOSING.is_set():
            return J(res)
        await asyncio.sleep(0.7)


# ----------------------------------------------------------- console développeur (F7)
@app.get("/api/dev/logs")
async def dev_logs(after: str = "", wait: float = 0, limit: int = 1000):
    """Journal d'Onde depuis le numéro `after` (vide : les derniers). `wait` : attend du nouveau (25 s au plus)."""
    start = int(after) if after.strip().isdigit() else None
    deadline = time.monotonic() + max(0.0, min(25.0, wait))
    limit = max(1, min(devlog.MAX_LOGS, limit))
    while True:
        res = devlog.logs(start, limit)
        if res["items"] or res["reset"] or start is None or time.monotonic() >= deadline or STREAMS_CLOSING.is_set():
            return J(res)
        await asyncio.sleep(0.5)


@app.get("/api/dev/http")
def dev_http(after: str = ""):
    """Requêtes reçues par Onde, de tous les appareils (pochettes et flux audio : seulement les erreurs)."""
    return J(devlog.http_log(int(after) if after.strip().isdigit() else None))


@app.post("/api/dev/client")
def dev_client(request: Request, body: dict = Body(...)):
    """Erreurs d'une page (téléphone, appli Android, iPhone…), pour les voir depuis la console."""
    items = body.get("items")
    return {"kept": devlog.client(client_ip(request), str(body.get("device") or "?"),
                                  items if isinstance(items, list) else [], str(body.get("page") or ""))}


@app.put("/api/dev/debug")
def dev_debug(body: dict = Body(...)):
    """Journal détaillé (DEBUG) : chaque requête vers LRCLIB, chaque nouvel essai réseau…"""
    devlog.set_debug(bool(body.get("on")))
    log.info("journal détaillé %s", "activé" if devlog.debug_on() else "désactivé")
    return {"debug": devlog.debug_on()}


@app.get("/api/dev/info")
def dev_info():
    """Tout sur le serveur : système, mémoire, disques, chemins, outils, réglages, tâches de fond."""
    try:
        import yt_dlp.version as ytv
        ytdlp = ytv.__version__
    except Exception:
        ytdlp = None
    env = {k: ("••••••" if "PASSWORD" in k or "SECRET" in k or "TOKEN" in k else v)
           for k, v in sorted(os.environ.items()) if k.startswith("ONDE_")}
    try:
        db_size = config.DB_PATH.stat().st_size
    except OSError:
        db_size = None
    try:
        al = align.status()
        ai = {"available": al.get("available"), "model": al.get("model"), "reason": al.get("reason")}
    except Exception as exc:
        ai = {"available": False, "reason": str(exc)}
    return J({
        **devlog.system(), "version": config.VERSION, "debug": devlog.debug_on(), "port": config.PORT,
        "https": config.HTTPS, "password": users.accounts_enabled(), "pi": player.available,
        "paths": {"music": str(config.MUSIC_DIR), "extra": [str(p) for p in config.EXTRA_DIRS],
                  "data": str(config.DATA_DIR), "db": str(config.DB_PATH), "dbSize": db_size},
        "disks": devlog.disks(config.MUSIC_DIR, config.DATA_DIR),
        "tools": {"ffmpeg": config.FFMPEG or None, "ytdlp": ytdlp, "ai": ai},
        "library": _stats(), "watching": library.status.get("watching"),
        "urls": lan_urls(), "settings": settings.all_settings(), "env": env,
    })


@app.get("/api/remote")
def remote_status(force: int = 0):
    """Accès à distance (Tailscale) : adresse à utiliser hors de la maison. Voir ./start.sh remote."""
    return J(remote.status(bool(force)))


@app.get("/api/settings")
def get_settings():
    return settings.all_settings()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    changed = settings.update(body)
    if changed:
        users.log_activity("settings", ", ".join(f"{k}={v}" for k, v in changed.items())[:300])
    if "pi_audio_device" in changed and player.available:
        try:
            player.set_audio_device(changed["pi_audio_device"])
        except Exception as exc:
            log.info("sortie audio non appliquée : %s", exc)
    if changed.get("artist_images") or changed.get("analyze_loudness"):
        library.schedule_background()
    if "normalize" in changed:
        player.refresh_volume()
    if changed.get("online_lyrics"):
        lyrics.wake()  # recherche automatique des paroles : reprend tout de suite
    if changed.get("auto_align") or "align_model" in changed:
        align.wake()
    return settings.all_settings()


@app.get("/api/home")
def home():
    return J({
        "recentAlbums": M.albums(order="added DESC", limit=14),
        "recentlyPlayed": M.albums(having="played IS NOT NULL", order="played DESC", limit=14),
        "topTracks": M.tracks("t.plays > 0", order="t.plays DESC, t.last_played DESC", limit=10),
        "randomAlbums": M.albums("a.name <> ?", config.SINGLES, order="RANDOM()", limit=14),
        "artists": M.artists(order="plays", limit=14),
        "playlists": M.playlist_summary(),
        "mixes": discover.mixes(),
        "stats": _stats(),
    })


TRACK_SORTS = {
    "added": "t.added_at DESC", "title": "t.title COLLATE NOCASE", "artist": "t.artist COLLATE NOCASE, a.name COLLATE NOCASE, t.track_no",
    "album": "a.name COLLATE NOCASE, t.disc_no, t.track_no", "plays": "t.plays DESC, t.last_played DESC",
    "duration": "t.duration DESC",
}


@app.get("/api/tracks")
def list_tracks(sort: str = "added"):
    return J(M.tracks(order=TRACK_SORTS.get(sort, TRACK_SORTS["added"])))


@app.get("/api/tracks/by-ids")
def tracks_by_ids(ids: str = ""):
    wanted = [int(i) for i in ids.split(",") if i.strip().isdigit()][:2000]
    return J(M.tracks_by_ids(wanted))


@app.get("/api/tracks/{tid}")
def get_track(tid: int):
    return M.track_by_id(tid) or _404()


@app.patch("/api/tracks/{tid}")
def edit_track(tid: int, body: dict = Body(...)):
    row = _track_row(tid)
    fields = {k: body[k] for k in library.TAG_KEYS if k in body}
    old_album = db.q1("SELECT * FROM albums WHERE id=?", row["album_id"])
    try:
        library.write_tags(row["path"], fields)
    except Exception as exc:
        raise HTTPException(400, f"Impossible d'écrire les tags : {exc}")
    _reindex(tid, row["path"], old_album)
    if any(k in fields for k in ("title", "artist")):
        lyrics.forget(tid)
    library.bump()
    return M.track_by_id(tid)


@app.delete("/api/tracks/{tid}")
def delete_track(tid: int):
    row = _track_row(tid)
    users.log_activity("delete_track", f"{row['title']} — {row['artist']}")
    try:
        os.remove(row["path"])
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HTTPException(400, f"Suppression impossible : {exc}")
    with db.tx():
        db.x("DELETE FROM tracks WHERE id=?", tid)
        library.cleanup_albums()
    library.bump()
    # supprime les dossiers devenus vides
    d = Path(row["path"]).parent
    while d != config.MUSIC_DIR and config.MUSIC_DIR in d.parents:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent
    return {"ok": True}


@app.post("/api/tracks/{tid}/played")
def track_played(tid: int, at: float = 0):
    """Une écoute. `at` (horodatage) : écoute faite hors connexion, transmise plus tard."""
    now = time.time()
    when = at if now - 90 * 86400 < at < now + 60 else None
    library.record_play(tid, "offline" if when else "web", at=when)
    return {"ok": True}


@app.get("/api/tracks/{tid}/stream")
def stream(tid: int, t: int = 0):
    """Flux audio (avance rapide possible). t=1 : version convertie pour les navigateurs limités."""
    row = _track_row(tid)
    if not os.path.exists(row["path"]):
        _404("Fichier absent")
    lyrics.prioritize(tid)  # titre lancé (ou préchargé) : ses paroles sont cherchées avant les autres
    if t and transcode.available():
        try:
            path = transcode.cached(tid, row["path"], row["mtime"] or 0)
        except Exception as exc:
            raise HTTPException(500, f"Conversion impossible : {exc}")
        return FileResponse(path, media_type="audio/flac", headers={"Cache-Control": "no-cache"})
    return FileResponse(row["path"], media_type=config.AUDIO_TYPES.get(row["ext"], "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/tracks/{tid}/lyrics")
def get_lyrics(tid: int, force: int = 0):
    return J(align.decorate(tid, lyrics.get(tid, bool(force))))


@app.put("/api/tracks/{tid}/lyrics")
def put_lyrics(tid: int, body: dict = Body(...)):
    _track_row(tid)
    return J(align.decorate(tid, lyrics.save(tid, str(body.get("text") or ""))))


@app.get("/api/lyrics/status")
def lyrics_status():
    """Recherche automatique des paroles : chiffres, recherche en cours, derniers résultats."""
    return J(lyrics.status())


@app.post("/api/lyrics/retry")
def lyrics_retry():
    """« Tout rechercher maintenant » : les titres introuvables sont recherchés tout de suite."""
    return J({"count": lyrics.retry_all()})


@app.post("/api/tracks/{tid}/lyrics/align")
def align_lyrics(tid: int, body: dict = Body(default={})):
    """Cale sur la voix les paroles brutes du titre (ou `text`, enregistré d'abord) : en tête de file,
    en arrière-plan. L'avancement se lit ensuite avec GET /api/tracks/{tid}/lyrics."""
    _track_row(tid)
    reason = align.unavailable_reason()
    if reason:
        raise HTTPException(400, f"Calage par IA indisponible : {reason}. Installe-le depuis les Réglages.")
    text = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
    if text:
        if lyrics.is_synced(text):
            raise HTTPException(400, "Ces paroles sont déjà synchronisées : il suffit de les enregistrer.")
        lyrics.save(tid, text)
    try:
        align.request(tid)
    except align.AlignError as exc:
        raise HTTPException(400, str(exc))
    return J(align.decorate(tid, lyrics.get(tid)))


@app.get("/api/align/status")
def align_status():
    return J(align.status())


@app.post("/api/align/install")
def align_install():
    if config.FROZEN:
        raise HTTPException(400, "Le calage par IA n'est pas disponible dans Onde.exe : lance Onde avec start.bat.")
    return J(align.start_install())


@app.get("/api/tracks/{tid}/download")
def download(tid: int):
    row = _track_row(tid)
    name = f"{row['artist']} - {row['title']}{row['ext']}"
    return FileResponse(row["path"], media_type="application/octet-stream",
                        headers={"Content-Disposition": _disposition(library.safe_name(name, "titre") )})


@app.post("/api/tracks/{tid}/cover")
async def track_cover(tid: int, request: Request):
    await run_in_threadpool(_track_row, tid)
    cid = await _cover_from_request(request)
    cid = await run_in_threadpool(library.set_track_cover, tid, cid, _flag(request, "embed", True))
    return {"cover": cid, "color": M.color_of(cid)}


# ------------------------------------------------------------------ albums
ALBUM_SORTS = {"added": "added DESC", "name": "a.name COLLATE NOCASE", "artist": "a.artist COLLATE NOCASE, a.year",
               "year": "a.year DESC", "played": "played DESC", "plays": "plays DESC"}


@app.get("/api/albums")
def list_albums(sort: str = "added"):
    return J(M.albums(order=ALBUM_SORTS.get(sort, ALBUM_SORTS["added"])))


@app.get("/api/albums/{aid}")
def get_album(aid: int):
    rows = M.albums("a.id=?", aid)
    if not rows:
        _404("Album introuvable")
    album = rows[0]
    album["tracks"] = M.album_tracks(aid)
    album["genres"] = sorted({t["genre"] for t in album["tracks"] if t["genre"]})
    album["more"] = [a for a in M.albums("a.artist = ? COLLATE NOCASE AND a.id <> ?", album["artist"], aid,
                                         order="a.year DESC", limit=12)]
    return J(album)


@app.patch("/api/albums/{aid}")
def edit_album(aid: int, body: dict = Body(...)):
    old = db.q1("SELECT * FROM albums WHERE id=?", aid) or _404("Album introuvable")
    mapping = {"name": "album", "artist": "album_artist", "year": "year", "genre": "genre"}
    fields = {mapping[k]: v for k, v in body.items() if k in mapping}
    rows = db.q("SELECT id, path FROM tracks WHERE album_id=?", aid)
    errors = 0
    for r in rows:
        try:
            library.write_tags(r["path"], fields)
            _reindex(r["id"], r["path"], old)
        except Exception as exc:
            errors += 1
            log.warning("album %s: %s", r["path"], exc)
    new = db.q1("SELECT album_id FROM tracks WHERE id=?", rows[0]["id"]) if rows else None
    library.bump()
    return {"id": new["album_id"] if new else aid, "errors": errors}


@app.post("/api/albums/{aid}/cover")
async def album_cover(aid: int, request: Request):
    if not await run_in_threadpool(db.q1, "SELECT id FROM albums WHERE id=?", aid):
        _404("Album introuvable")
    cid = await _cover_from_request(request)
    cid = await run_in_threadpool(library.set_album_cover, aid, cid, _flag(request, "embed", True))
    return {"cover": cid, "color": M.color_of(cid)}


@app.get("/api/albums/{aid}/download")
def album_zip(aid: int):
    a = db.q1("SELECT name, artist FROM albums WHERE id=?", aid) or _404()
    tracks = db.q("SELECT path, title, track_no, ext FROM tracks WHERE album_id=? ORDER BY disc_no, track_no", aid)
    files = []
    for i, t in enumerate(tracks, 1):
        files.append((t["path"], library.safe_name(f"{t['track_no'] or i:02d} - {t['title']}", f"{i:02d}") + t["ext"]))
    name = library.safe_name(f"{a['artist']} - {a['name']}", "album") + ".zip"
    return StreamingResponse(library.zip_stream(files), media_type="application/zip",
                             headers={"Content-Disposition": _disposition(name)})


# ------------------------------------------------------------------ artistes / genres
ARTIST_SORTS = {"name": "name", "plays": "plays", "played": "played"}


@app.get("/api/artists")
def list_artists(sort: str = "name"):
    return J(M.artists(order=ARTIST_SORTS.get(sort, "name")))


@app.get("/api/artists/{name:path}")
def get_artist(name: str):
    tracks = M.tracks("(t.artist = ? COLLATE NOCASE OR a.artist = ? COLLATE NOCASE)", name, name,
                      order="t.plays DESC, a.year DESC, t.disc_no, t.track_no")
    if not tracks:
        _404("Artiste introuvable")
    albums = M.albums("a.artist = ? COLLATE NOCASE", name, order="a.year DESC, added DESC")
    own = {a["id"] for a in albums}
    appears = M.albums(f"a.id IN (SELECT album_id FROM tracks WHERE artist = ? COLLATE NOCASE)", name,
                       order="a.year DESC")
    appears = [a for a in appears if a["id"] not in own]
    covers_count: dict[str, int] = {}
    for t in tracks:
        if t["cover"]:
            covers_count[t["cover"]] = covers_count.get(t["cover"], 0) + 1
    album_cover = max(covers_count, key=covers_count.get) if covers_count else None
    photo, locked = M.artist_image(name)
    cover = photo or album_cover
    display = tracks[0]["albumArtist"] if db.norm(tracks[0]["albumArtist"]) == db.norm(name) else tracks[0]["artist"]
    return J({"name": display, "cover": cover, "photo": bool(photo), "photoLocked": locked,
              "following": follow.is_following(display),
              "color": M.color_of(cover), "tracks": tracks, "albums": albums, "appears": appears,
              "plays": sum(t["plays"] for t in tracks), "count": len(tracks)})


@app.post("/api/artists/{name:path}/image")
async def artist_image(name: str, request: Request):
    cid = await _cover_from_request(request)
    await run_in_threadpool(library.set_artist_image, name, cid)
    return {"cover": cid, "color": M.color_of(cid)}


@app.delete("/api/artists/{name:path}/image")
def artist_image_reset(name: str):
    library.set_artist_image(name, None)
    return {"ok": True}


@app.get("/api/genres")
def list_genres():
    rows = db.q("""SELECT genre, COUNT(*) n, (SELECT COALESCE(t2.cover_id, a2.cover_id) FROM tracks t2
                   JOIN albums a2 ON a2.id=t2.album_id WHERE t2.genre = t.genre COLLATE NOCASE
                   AND COALESCE(t2.cover_id, a2.cover_id) IS NOT NULL ORDER BY t2.plays DESC LIMIT 1) cover
                   FROM tracks t WHERE genre <> '' GROUP BY genre COLLATE NOCASE ORDER BY n DESC""")
    return [{"name": r["genre"], "count": r["n"], "cover": r["cover"]} for r in rows]


# ------------------------------------------------------------------ radio, mixes, stats
@app.get("/api/radio")
def radio(track: int = 0, tracks: str = "", album: int = 0, artist: str = "", genre: str = "",
          exclude: str = "", limit: int = 40):
    seeds = [track] if track else []
    seeds += [int(x) for x in tracks.split(",") if x.strip().isdigit()][:20]
    if album:
        seeds += [r["id"] for r in db.q("SELECT id FROM tracks WHERE album_id=?", album)]
    ex = {int(x) for x in exclude.split(",") if x.strip().isdigit()}
    ids = discover.radio(seed_tracks=seeds, artist=artist or None, genre=genre or None,
                         exclude=ex, limit=max(1, min(limit, 150)))
    return J({"tracks": M.tracks_by_ids(ids)})


@app.get("/api/mix/{kind}")
def mix(kind: str, arg: str = ""):
    name, ids = discover.mix_tracks(kind, arg)
    return J({"name": name, "tracks": M.tracks_by_ids(ids)})


@app.get("/api/stats")
def get_stats(days: int = 30):
    return J(stats.compute(max(0, min(days, 3650))))


@app.get("/api/wrapped")
def get_wrapped(year: int = 0):
    return J(stats.wrapped(year or None))


@app.get("/api/history")
def get_history(limit: int = 100, before: float = 0):
    return J(stats.history(max(1, min(limit, 500)), before or None))


@app.get("/api/genres/{name:path}")
def get_genre(name: str):
    tracks = M.tracks("t.genre = ? COLLATE NOCASE", name, order="t.plays DESC, t.added_at DESC")
    if not tracks:
        _404("Genre introuvable")
    return J({"name": tracks[0]["genre"], "tracks": tracks})


# ------------------------------------------------------------------ recherche
def _search(q: str) -> dict:
    terms = db.like_terms(q)
    if not terms:
        return {"tracks": [], "albums": [], "artists": [], "playlists": []}
    tw = " AND ".join("t.search LIKE ? ESCAPE '\\'" for _ in terms)
    aw = " AND ".join("a.search LIKE ? ESCAPE '\\'" for _ in terms)
    words = db.norm(q).split()
    playlists = [p for p in M.playlist_summary() if all(w in db.norm(p["name"] + " " + p["description"]) for w in words)]
    return {
        "tracks": M.tracks(tw, *terms, order="t.plays DESC, t.title COLLATE NOCASE", limit=60),
        "albums": M.albums(aw + " AND a.name <> ?", *terms, config.SINGLES, order="plays DESC, a.name", limit=24),
        "artists": M.artists(order="plays", limit=18, terms=words),
        "playlists": playlists[:12],
    }


_vocab: dict = {"gen": None, "words": [], "set": set(), "index": {}}
# Plusieurs recherches arrivent en même temps (une par lettre tapée, sur plusieurs appareils) : sans
# verrou, une requête pouvait lire l'index d'une version du vocabulaire et la liste de mots d'une
# autre — suggestions fausses, voire IndexError. On lit et remplace les trois ensemble, sous verrou ;
# la construction (coûteuse) se fait hors verrou.
_vocab_lock = threading.Lock()


def _bigrams(w: str) -> set[str]:
    return {w[i:i + 2] for i in range(len(w) - 1)}


def _vocabulary() -> tuple[set[str], list[str], dict[str, list[int]]]:
    """(mots connus, liste des mots, index des paires de lettres), toujours cohérents entre eux."""
    gen = library.status["gen"]
    with _vocab_lock:
        if _vocab["gen"] == gen:
            return _vocab["set"], _vocab["words"], _vocab["index"]
    words: set[str] = set()
    for r in db.q("SELECT search FROM tracks UNION ALL SELECT search FROM albums"):
        words.update(w for w in (r["search"] or "").split() if len(w) >= 3)
    wl = list(words)
    index: dict[str, list[int]] = {}
    for i, w in enumerate(wl):
        for bg in _bigrams(w):
            index.setdefault(bg, []).append(i)
    with _vocab_lock:
        _vocab.update(gen=gen, words=wl, set=words, index=index)
    return words, wl, index


def _suggest(q: str) -> str | None:
    """Correction des fautes de frappe (« beyonse » -> « beyonce »).

    Pour rester rapide sur une grosse bibliothèque (des dizaines de milliers de mots), on ne compare
    finement que les quelques centaines de mots qui partagent le plus de paires de lettres avec le mot
    tapé (index construit une fois par état de la bibliothèque)."""
    known, wordlist, index = _vocabulary()
    out, changed = [], False
    for w in db.norm(q).split():
        if len(w) < 3 or w in known:
            out.append(w)
            continue
        counts: Counter = Counter()
        for bg in _bigrams(w):
            counts.update(index.get(bg, ()))
        candidates = [wordlist[i] for i, _ in counts.most_common(400)]
        m = difflib.get_close_matches(w, candidates, n=1, cutoff=0.7)
        out.append(m[0] if m else w)
        changed = changed or bool(m)
    return " ".join(out) if changed else None


@app.get("/api/search")
def search(q: str = ""):
    short = clean.query(q) if q.strip() else ""
    shortened = bool(short) and short != " ".join(q.split())
    res = _search(short) if shortened else _search(q)
    if shortened and any(res.values()):
        res["shortened"] = short  # le nom tapé était alourdi (émojis, « [AMV] », « 4K »…) : on a cherché l'essentiel
    else:
        res = _search(q)
    if q.strip() and not any(res.values()):
        fix = _suggest(q)
        if fix:
            res = _search(fix)
            res["suggestion"] = fix
    return J(res)


# ------------------------------------------------------------------ rangement (corbeille)
@app.get("/api/tidy")
def tidy_status():
    return {"items": tidy.items(), "words": tidy.words(), "keepDays": tidy.KEEP_DAYS}


@app.post("/api/tidy/run")
def tidy_run():
    return tidy.run()


@app.post("/api/tidy/restore/{key}")
def tidy_restore(key: str):
    try:
        tidy.restore(key)
    except KeyError:
        _404("Introuvable dans la corbeille")
    library.scan()
    return {"ok": True}


@app.delete("/api/tidy")
def tidy_empty():
    return {"deleted": tidy.empty()}


# ------------------------------------------------------------------ likes
@app.get("/api/recent")
def recent_tracks(limit: int = 50):
    """Les derniers titres écoutés (sans doublon) : gardés sur l'appareil pour l'écoute hors connexion."""
    rows = db.q("""SELECT track_id, MAX(played_at) m FROM plays WHERE user_id = onde_uid()
                   GROUP BY track_id ORDER BY m DESC LIMIT ?""", max(1, min(limit, 200)))
    return J({"name": "Écoutés récemment", "tracks": M.tracks_by_ids([r["track_id"] for r in rows])})


@app.get("/api/liked")
def liked():
    rows = db.q(M.TRACK_SELECT.replace("LEFT JOIN likes l", "JOIN likes l") + " ORDER BY l.liked_at DESC")
    return J([M.track(r) for r in rows])


@app.put("/api/liked/{tid}")
def like(tid: int):
    _track_row(tid)
    db.x("INSERT OR IGNORE INTO likes(user_id, track_id, liked_at) VALUES(onde_uid(),?,?)", tid, time.time())
    return {"liked": True}


@app.delete("/api/liked/{tid}")
def unlike(tid: int):
    db.x("DELETE FROM likes WHERE track_id=? AND user_id=onde_uid()", tid)
    return {"liked": False}


# ------------------------------------------------------------------ playlists
@app.get("/api/playlists")
def playlists():
    return M.playlist_summary()


@app.post("/api/playlists")
def create_playlist(body: dict = Body(default={})):
    now = time.time()
    count = db.q1("SELECT COUNT(*) n FROM playlists WHERE owner_id=onde_uid()")["n"]
    name = (body.get("name") or "").strip()[:120] or f"Ma playlist n° {count + 1}"
    cur = db.x("INSERT INTO playlists(name, description, created_at, updated_at, owner_id) VALUES(?,?,?,?,onde_uid())",
               name, (body.get("description") or "").strip()[:500], now, now)
    pid = cur.lastrowid
    ids = [int(i) for i in body.get("trackIds") or []]
    if ids:
        _add_to_playlist(pid, ids)
    return _playlist(pid)


@app.get("/api/playlists/{pid}")
def get_playlist(pid: int):
    p = _playlist(pid)
    p["tracks"] = M.playlist_tracks(pid)
    return J(p)


@app.patch("/api/playlists/{pid}")
def edit_playlist(pid: int, body: dict = Body(...)):
    _playlist(pid)
    if "name" in body:
        db.x("UPDATE playlists SET name=? WHERE id=?", (body["name"] or "").strip()[:120] or "Sans titre", pid)
    if "description" in body:
        db.x("UPDATE playlists SET description=? WHERE id=?", (body["description"] or "").strip()[:500], pid)
    db.x("UPDATE playlists SET updated_at=? WHERE id=?", time.time(), pid)
    return _playlist(pid)


@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int):
    _playlist(pid)  # (seulement ses propres playlists)
    db.x("DELETE FROM playlists WHERE id=? AND owner_id=onde_uid()", pid)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Pochettes
# --------------------------------------------------------------------------- #
@app.get("/api/cover/{cid}/{size}")
def cover(cid: str, size: int, f: str = "webp"):
    path = covers.thumb_file(cid, size, "jpg" if f == "jpg" else "webp")
    if not path:
        _404("Pochette introuvable")
    media = "image/jpeg" if path.suffix == ".jpg" else "image/webp"
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/covers/search")
def cover_search(q: str = "", kind: str = "album"):
    return covers.search_online(q, kind if kind in ("song", "artist") else "album")


@app.get("/api/meta/search")
def meta_search(q: str = ""):
    return covers.search_meta(q)


@app.get("/onde.crt", include_in_schema=False)
@app.get("/onde-cert.pem", include_in_schema=False)
def download_cert():
    """L'autorité locale d'Onde, à installer une fois par appareil pour que le navigateur ne
    t'avertisse plus (voir Réglages > Accès sécurisé). En `.crt` : c'est l'extension que Windows
    et Android reconnaissent ; le type `application/x-x509-ca-cert` fait proposer directement
    l'installation par Safari sur iPhone/iPad."""
    if not tls.CA_CERT.is_file():
        _404("HTTPS n'a pas encore été activé sur ce serveur")
    return FileResponse(tls.CA_CERT, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": _disposition("onde.crt"), "Cache-Control": "no-store"})


def _set_env_var(key: str, value: str) -> None:
    """Met à jour (ou ajoute) une variable dans `onde.env`, sans toucher au reste du fichier.

    Met aussi à jour `os.environ` : au démarrage, `config._load_env_file()` recopie le fichier dans
    `os.environ` avec `setdefault`, et cet environnement est transmis tel quel au process relancé.
    Sans cette ligne, la valeur héritée l'emporterait sur le fichier modifié et le changement serait
    ignoré dès le deuxième basculement."""
    env_path = Path(os.environ.get("ONDE_ENV_FILE", str(config.ROOT / "onde.env")))
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    out: list[str] = []
    found = False
    for line in lines:
        if line.strip().split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        label = {"ONDE_HTTPS": "Accès sécurisé (HTTPS)", "ONDE_PASSWORD": "Mot de passe"}.get(key, key)
        out.append(f"# {label} — modifié depuis les Réglages d'Onde")
        out.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    if env_path.is_file():  # garde les droits du fichier (ex. chmod 600 s'il contient un mot de passe)
        try:
            import shutil
            shutil.copymode(env_path, tmp)
        except OSError:
            pass
    os.replace(tmp, env_path)
    os.environ[key] = value


_install_lock = threading.Lock()


def _install_cryptography() -> str | None:
    """Installe le module `cryptography` dans le Python qui fait tourner Onde (son `.venv` avec
    start.sh / start.bat). Renvoie None si tout va bien, sinon la raison de l'échec."""
    import importlib
    import subprocess
    import sys

    with _install_lock:
        if tls.available():
            return None
        if config.FROZEN:
            return "cette version d'Onde.exe a été construite sans le module HTTPS (cryptography)"
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
               "cryptography>=42"]
        log.info("installation du module HTTPS : %s", " ".join(cmd))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        importlib.invalidate_caches()
        if r.returncode == 0 and tls.available():
            return None
        out = (r.stderr or "") + (r.stdout or "")
        if "externally-managed" in out:
            return "ce Python est géré par le système — lance Onde avec ./start.sh, qui lui crée son propre environnement"
        if "No module named pip" in out:
            return "pip n'est pas installé pour ce Python"
        lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
        return lines[-1] if lines else f"pip a échoué (code {r.returncode})"


_BAD_PWD_CHARS = set("\"'\\\n\r\t")


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Requête refusée")


@app.post("/api/settings/password")
async def set_password(request: Request):
    """Change son propre mot de passe (demande l'actuel s'il y en a un). L'administrateur qui se donne
    un mot de passe active les profils : il faut alors se connecter (cet appareil reste connecté)."""
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "Requête invalide")
    _same_origin(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Requête invalide")
    if not isinstance(body, dict):
        raise HTTPException(400, "Requête invalide")
    new = str(body.get("password") or "")
    current = str(body.get("current") or "")
    me = users.current()
    row = await run_in_threadpool(db.q1, "SELECT pw FROM users WHERE id=?", me["id"])
    await asyncio.sleep(0.3)  # ralentit les essais au hasard
    if row and row["pw"] and not await run_in_threadpool(users.check_password, current, row["pw"]):
        await asyncio.sleep(0.7)
        raise HTTPException(403, "Le mot de passe actuel est incorrect.")
    if not new and me["id"] != users.ADMIN_ID:
        raise HTTPException(400, "Choisis un nouveau mot de passe.")
    if not new and await run_in_threadpool(db.q1, "SELECT 1 FROM users WHERE id<>? AND pw IS NOT NULL", users.ADMIN_ID):
        raise HTTPException(400, "D'autres profils existent : l'administrateur doit garder un mot de passe.")
    try:
        await run_in_threadpool(lambda: users.update(me["id"], password=new))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if config.PASSWORD:  # (l'ancien mot de passe unique ne sert plus)
        await run_in_threadpool(_set_env_var, "ONDE_PASSWORD", "")
        config.PASSWORD = ""
    await run_in_threadpool(users.log_activity, "password", "", client_ip(request))
    resp = JSONResponse({"ok": True, "password": users.accounts_enabled()})
    token = users.cookie_token(request.scope.get("headers", []))
    if new:
        await run_in_threadpool(users.end_all_sessions, me["id"], token)  # les autres appareils se reconnectent
        if not await run_in_threadpool(users.session_user, token):
            resp = _with_session(resp, request, me)  # (cet appareil reste connecté)
    return resp


# ------------------------------------------------------------------ sauvegarde automatique (administrateur)
@app.get("/api/backup")
def backup_info():
    return J(backup.settings_view())


@app.put("/api/backup")
def backup_settings(body: dict = Body(...)):
    try:
        out = backup.configure(directory=body.get("dir"), passphrase=body.get("passphrase"),
                               auto=body.get("auto") if "auto" in body else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("backup", "réglages de sauvegarde")
    return J(out)


@app.post("/api/backup/run")
def backup_now():
    r = backup.run(force=True)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "Sauvegarde impossible")
    users.log_activity("backup", r["file"])
    return r


@app.post("/api/backup/restore")
def backup_restore(body: dict = Body(...)):
    try:
        backup.stage_restore(str(body.get("name") or ""), str(body.get("passphrase") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("backup", f"restauration de {body.get('name')}")
    ok = restart.request()
    return {"ok": True, "restarting": ok}


# ------------------------------------------------------------------ mode soirée (file d'attente commune)
@app.post("/api/jam")
def jam_start():
    me = users.current()
    j = jam.start(me["id"], me["name"])
    return {"token": j["token"], "path": f"/jam/{j['token']}", "expires": j["expires"]}


@app.get("/api/jam")
def jam_host(after: int = 0, now: int = 0):
    """Pour l'hôte : les titres ajoutés par les invités depuis `after` (et le titre en cours, pour eux)."""
    me = users.current()
    j = jam.of_host(me["id"])
    if not j:
        return {"active": False}
    jam.set_now(me["id"], now or None)
    return {"active": True, "token": j["token"], "path": f"/jam/{j['token']}", "expires": j["expires"],
            "items": [i for i in j["items"] if i["n"] > after], "guests": len(j["guests"])}


@app.delete("/api/jam")
def jam_stop():
    jam.stop(users.current()["id"])
    return {"active": False}


def _jam_or_404(token: str) -> dict:
    j = jam.get(token)
    if not j:
        _404("Soirée terminée")
    return j


@app.get("/api/jam/{token}")
def jam_view(token: str):
    return J(jam.public_view(_jam_or_404(token)))


@app.get("/api/jam/{token}/search")
def jam_search(token: str, q: str = ""):
    _jam_or_404(token)
    terms = db.like_terms(q)[:6]
    if not terms:
        return J({"tracks": []})
    rows = M.tracks(" AND ".join("t.search LIKE ? ESCAPE '\\'" for _ in terms), *terms,
                    order="t.plays DESC, t.title COLLATE NOCASE", limit=30)
    return J({"tracks": [{"id": t["id"], "title": t["title"], "artist": t["artist"], "cover": t["cover"],
                          "dur": t.get("dur") or t.get("duration")} for t in rows]})


@app.post("/api/jam/{token}/add")
def jam_add(token: str, request: Request, body: dict = Body(...)):
    _jam_or_404(token)
    try:
        item = jam.add(token, int(body.get("trackId")), str(body.get("by") or ""), client_ip(request) + "|" + str(body.get("guest") or "")[:20])
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) and str(exc) else "Requête invalide")
    except KeyError:
        _404("Soirée terminée")
    return {"ok": True, "n": item["n"]}


@app.get("/api/jam/{token}/cover/{cid}")
def jam_cover(token: str, cid: str):
    _jam_or_404(token)
    path = covers.thumb_file(cid, 192, "webp")
    if not path:
        _404("Pochette introuvable")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/jam/{token}", response_class=HTMLResponse, include_in_schema=False)
def jam_page(token: str):
    if not jam.get(token):
        return _auth_page("Soirée terminée", "Cette soirée est finie (ou le lien a expiré).", "", "Ok", "/login", status=410)
    html = (config.STATIC_DIR / "jam.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


# ------------------------------------------------------------------ mise à jour en un clic (administrateur)
@app.get("/api/update")
def update_info():
    ok, why = updater.available()
    return {"available": ok, "reason": why, "version": config.VERSION, "canRestart": restart.available(),
            "source": autoupdate.source(), "auto": settings.get("auto_update"), "status": autoupdate.status,
            "apk": autoupdate.apk_info()}


@app.post("/api/update/check")
def update_check():
    """« Vérifier maintenant » : cherche (et installe) la dernière version, en arrière-plan."""
    if not autoupdate.source():
        raise HTTPException(400, "Choisis d'abord d'où viennent les mises à jour (dépôt GitHub ou adresse).")
    threading.Thread(target=autoupdate.check, name="onde-update-now", daemon=True).start()
    return {"started": True}


# ------------------------------------------------------------------ appli Android : mise à jour depuis le serveur
@app.get("/api/app/version")
def app_version():
    return {"apk": autoupdate.apk_info(), "server": config.VERSION}


@app.get("/api/app/apk", include_in_schema=False)
def app_apk():
    p = autoupdate.apk_path()
    if not p:
        _404("Pas d'APK sur ce serveur")
    return FileResponse(p, media_type="application/vnd.android.package-archive", filename="Onde.apk",
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/update")
async def update_apply(request: Request):
    _same_origin(request)
    data = await _read_limited(request, updater.MAX_BYTES)
    try:
        res = await run_in_threadpool(updater.apply, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await run_in_threadpool(users.log_activity, "update", f"{res['from']} → {res['to']}")
    res["restarting"] = restart.request(1.0)
    return res


# ------------------------------------------------------------------ nouveautés des artistes suivis
@app.get("/api/follows")
def follows_list():
    return J({"artists": follow.following(), "status": follow.status})


@app.put("/api/follows/{name:path}")
def follows_add(name: str):
    try:
        follow.follow(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"following": True}


@app.delete("/api/follows/{name:path}")
def follows_remove(name: str):
    follow.unfollow(name)
    return {"following": False}


@app.post("/api/follow-check")
def follows_check():
    threading.Thread(target=follow.check_all, name="onde-follow-now", daemon=True).start()
    return {"started": True}


# ------------------------------------------------------------------ reprise sur un autre appareil
@app.post("/api/resume")
def resume_save(body: dict = Body(...)):
    """Où en est la lecture sur cet appareil (envoyé régulièrement) : un autre appareil peut reprendre là."""
    try:
        queue = [int(i) for i in (body.get("queue") or [])][:300]
        tid = int(body.get("trackId"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Requête invalide")
    src = body.get("source") if isinstance(body.get("source"), dict) else None
    state = {
        "trackId": tid, "position": max(0.0, float(body.get("position") or 0)), "queue": queue,
        "index": max(0, min(int(body.get("index") or 0), max(0, len(queue) - 1))),
        "device": str(body.get("device") or "")[:60], "deviceId": str(body.get("deviceId") or "")[:40],
        "source": {k: str(v)[:200] for k, v in (src or {}).items() if k in ("key", "label", "href", "kind")} or None,
        "playing": bool(body.get("playing")), "at": time.time(),
    }
    db.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", f"resume:{users.uid()}", json.dumps(state))
    return {"ok": True}


@app.get("/api/resume")
def resume_get():
    row = db.q1("SELECT v FROM meta WHERE k=?", f"resume:{users.uid()}")
    if not row:
        return {"state": None}
    state = json.loads(row["v"])
    state["track"] = (M.tracks_by_ids([state["trackId"]]) or [None])[0]
    return J({"state": state})


# ------------------------------------------------------------------ espace par profil, demandes à l'admin
@app.get("/api/me/quota")
def me_quota():
    return quota.info(users.uid())


@app.post("/api/me/quota-request")
def me_quota_request(body: dict = Body(default={})):
    try:
        return J(asks.ask_quota(int(body.get("amountMb") or 2000), str(body.get("note") or "")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/asks")
def asks_list():
    if users.is_admin():
        return J({"asks": asks.all_asks(), "autoDelay": config.ASK_AUTO_DELAY})
    return J({"asks": asks.mine(users.uid()), "autoDelay": config.ASK_AUTO_DELAY})


@app.post("/api/asks")
def asks_create(body: dict = Body(...)):
    try:
        return J(asks.ask_track(str(body.get("query") or ""), str(body.get("note") or "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/asks/{ask_id}")
def asks_cancel(ask_id: int):
    asks.cancel(ask_id, users.uid())
    return {"ok": True}


@app.post("/api/asks/{ask_id}/approve")
def asks_approve(ask_id: int):
    try:
        return J(asks.approve(ask_id, by="admin"))
    except KeyError:
        _404("Demande introuvable")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/asks/{ask_id}/refuse")
def asks_refuse(ask_id: int, body: dict = Body(default={})):
    try:
        return J(asks.refuse(ask_id, str(body.get("reason") or ""), by="admin"))
    except KeyError:
        _404("Demande introuvable")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/asks/{ask_id}/ai")
def asks_ai(ask_id: int):
    """L'administrateur demande l'avis de la mini IA tout de suite (sans attendre 5 minutes)."""
    a = asks.get(ask_id)
    if not a:
        _404("Demande introuvable")
    return J(asks.evaluate(a["query"], a["userId"]))


# ------------------------------------------------------------------ tableau de bord (administrateur)
@app.get("/api/dashboard")
def dashboard_view():
    return J(dashboard.compute())


# ------------------------------------------------------------------ partage d'une playlist (lecture seule)
@app.get("/api/playlists/{pid}/share")
def playlist_share_get(pid: int):
    p = db.q1("SELECT owner_id FROM playlists WHERE id=?", pid)
    if not p:
        _404("Playlist introuvable")
    if p["owner_id"] != users.uid() and not users.is_admin():
        return {"share": None, "mine": False}
    return {"share": share.of_playlist(pid), "mine": True}


@app.post("/api/playlists/{pid}/share")
def playlist_share(pid: int):
    try:
        return {"share": share.create(pid)}
    except KeyError:
        _404("Playlist introuvable")
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.delete("/api/playlists/{pid}/share")
def playlist_unshare(pid: int):
    try:
        share.revoke(pid)
    except KeyError:
        _404("Playlist introuvable")
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    return {"share": None}


def _share_or_404(token: str):
    s = share.resolve(token)
    if not s:
        _404("Lien de partage coupé ou inconnu")
    return s


@app.get("/api/share/{token}")
def share_view(token: str):
    v = share.public_view(token)
    if not v:
        _404("Lien de partage coupé ou inconnu")
    return J(v)


@app.get("/api/share/{token}/stream/{tid}")
def share_stream(token: str, tid: int):
    _share_or_404(token)
    if not share.allows(token, tid):
        _404("Titre absent de cette playlist")
    row = _track_row(tid)
    if not os.path.exists(row["path"]):
        _404("Fichier absent")
    return FileResponse(row["path"], media_type=config.AUDIO_TYPES.get(row["ext"], "application/octet-stream"),
                        headers={"Cache-Control": "private, no-cache"})


@app.get("/api/share/{token}/cover/{cid}")
def share_cover(token: str, cid: str):
    if not share.allows_cover(token, cid):
        _404("Pochette introuvable")
    path = covers.thumb_file(cid, 384, "webp")
    if not path:
        _404("Pochette introuvable")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
def share_page(token: str):
    if not share.resolve(token):
        return _auth_page("Lien coupé", "Ce partage n'existe plus (la personne l'a arrêté).", "", "Ok", "/login", status=410)
    share.public_view(token, count_view=True)
    html = (config.STATIC_DIR / "share.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


# ------------------------------------------------------------------ profils (API)
def _user_or_404(user_id: int) -> dict:
    u = users.get(user_id)
    if not u:
        _404("Profil introuvable")
    return u


@app.get("/conditions", response_class=HTMLResponse, include_in_schema=False)
def terms_page():
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onde — conditions d'utilisation</title><link rel="icon" href="/static/icons/icon-192.png"><meta name="theme-color" content="#08080b">
<style>{AUTH_CSS}main{{width:min(720px,100%);background:#131318;border:1px solid #23232b;border-radius:16px;padding:28px}}
h3{{margin:22px 0 6px;font-size:16px}}li{{color:#c9c9d1;font-size:14px;margin:4px 0}}ul{{padding-left:20px;margin:0}}</style></head>
<body><main><h1>{htmllib.escape(terms.TITLE)}</h1><p>Version {terms.VERSION}</p>{terms.html_body()}</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/api/terms")
def terms_get():
    return {"version": terms.VERSION, "title": terms.TITLE, "html": terms.html_body(),
            "accepted": terms.accepted(users.uid()) if users.current_user.get() else None}


@app.post("/api/me/terms")
def terms_accept():
    terms.accept(users.uid())
    users.refresh_cache()
    users.log_activity("terms", f"conditions {terms.VERSION} acceptées")
    return {"ok": True, "version": terms.VERSION}


@app.get("/api/me")
def me_info():
    return {**users.current(), "accounts": users.accounts_enabled(), "mail": users.mail_available(),
            "roles": users.ROLES}


@app.patch("/api/me")
def me_edit(body: dict = Body(...)):
    me = users.current()
    try:
        out = users.update(me["id"], name=body.get("name"), email=body.get("email"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("user", "profil modifié")
    return out


@app.get("/api/users")
def users_list():
    return {"users": users.all_users(), "roles": users.ROLES, "invites": users.invites(),
            "accounts": users.accounts_enabled(), "mail": users.mail_available()}


@app.post("/api/users")
def users_create(body: dict = Body(...)):
    try:
        u = users.create(body.get("name", ""), body.get("password", ""), body.get("role", "listener"), body.get("email", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("user", f"profil « {u['name']} » créé ({users.ROLES[u['role']]})")
    return u


@app.patch("/api/users/{user_id}")
def users_edit(user_id: int, body: dict = Body(...)):
    _user_or_404(user_id)
    try:
        u = users.update(user_id, name=body.get("name"), email=body.get("email"), role=body.get("role"),
                         password=body.get("password") or None)
        if "quotaMb" in body:
            q = body.get("quotaMb")
            quota.set_limit(user_id, None if q in (None, "") else int(q))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.get("password"):
        users.end_all_sessions(user_id)
    what = ", ".join(k for k in ("name", "email", "role", "password", "quotaMb") if body.get(k) is not None)
    users.log_activity("user", f"« {u['name']} » : {what}")
    return u


@app.delete("/api/users/{user_id}")
def users_delete(user_id: int):
    u = _user_or_404(user_id)
    try:
        users.delete(user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("user", f"profil « {u['name']} » supprimé")
    return {"ok": True}


@app.get("/api/pwreqs")
def pwreqs_list():
    return J({"requests": users.pwreqs()})


@app.delete("/api/pwreqs/{req_id}")
def pwreqs_dismiss(req_id: int):
    users.dismiss_pwreq(req_id)
    return {"ok": True}


@app.post("/api/users/{user_id}/reset-link")
def users_reset_link(user_id: int):
    """L'admin obtient un lien (24 h, à usage unique) pour que le pote choisisse un nouveau mot de passe."""
    u = _user_or_404(user_id)
    token, expires = users.admin_reset_link(user_id)
    users.log_activity("reset_link", f"« {u['name']} »")
    return {"path": f"/reset/{token}", "expires": expires, "name": u["name"]}


@app.post("/api/users/{user_id}/logout")
def users_logout(user_id: int):
    _user_or_404(user_id)
    users.end_all_sessions(user_id)
    return {"ok": True}


@app.post("/api/invites")
def invite_create(request: Request, body: dict = Body(...)):
    if not users.accounts_enabled():
        raise HTTPException(400, "Donne-toi d'abord un mot de passe (Mon profil) : sans lui, tout le monde a accès à tout.")
    try:
        token = users.create_invite(body.get("role", "member"), float(body.get("days") or 7),
                                    int(body.get("uses") or 1), str(body.get("note") or ""))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    users.log_activity("invite", f"{users.ROLES[body.get('role', 'member')]} · {body.get('days') or 7} j")
    return {"token": token, "path": f"/invite/{token}"}


@app.delete("/api/invites/{short_id}")
def invite_delete(short_id: str):
    try:
        users.delete_invite(short_id)
    except KeyError:
        _404("Invitation introuvable")
    return {"ok": True}


@app.get("/api/activity")
def activity_log(limit: int = 200, user: int = 0, plays: int = 1):
    return J(users.activity(max(1, min(limit, 1000)), user or None, bool(plays)))


@app.post("/api/settings/https")
def set_https(body: dict = Body(...)):
    """Active ou désactive le HTTPS depuis les Réglages. Installe au besoin ce qu'il faut, prépare
    le certificat, enregistre le choix dans `onde.env`, puis Onde redémarre tout seul."""
    enabled = bool(body.get("enabled"))
    if enabled and not tls.available():
        err = _install_cryptography()
        if err:
            raise HTTPException(400, f"Impossible d'installer le module HTTPS automatiquement ({err}). "
                                     "Vérifie que le serveur a accès à Internet, puis réessaie.")
    if enabled:
        try:
            tls.ensure_cert()  # maintenant : une erreur éventuelle s'affiche ici plutôt qu'au redémarrage
        except Exception as exc:
            raise HTTPException(500, f"Impossible de préparer le certificat : {exc}")
    _set_env_var("ONDE_HTTPS", "1" if enabled else "0")
    restarting = restart.request()
    return J({"ok": True, "https": enabled, "restarting": restarting})


@app.get("/api/qr.svg", include_in_schema=False)
def qr_svg(text: str = ""):
    try:
        import segno
    except ImportError:
        _404("QR code indisponible")
    qr = segno.make(text or lan_urls()[0], error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=8, border=2, dark="#0b0b10", light="#ffffff", xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


# --------------------------------------------------------------------------- #
# Routes rangées dans leurs fichiers : playlists, import, maintenance, Pi, synchronisation
# --------------------------------------------------------------------------- #
for _router in (playlists_router, import_router, maintenance_router, pi_router, sync_router):
    app.include_router(_router)
