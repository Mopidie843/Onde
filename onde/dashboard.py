"""Tableau de bord de l'administrateur : qui est connecté, ce qui s'écoute, l'espace disque, les
erreurs d'import et les demandes en attente — tout sur une page."""
from __future__ import annotations

import json
import shutil
import time

from . import api_models as M
from . import asks, config, db, quota, users

ONLINE = 660        # vu dans les ~10 dernières minutes = connecté (la session est notée toutes les 5 min)
PLAYING = 150       # position envoyée il y a moins de 2 min 30 = en train d'écouter


def compute() -> dict:
    now = time.time()
    online = []
    for r in db.q("""SELECT s.user_id, u.name, u.role, s.device, s.ip, MAX(s.last_used) last
                     FROM sessions s JOIN users u ON u.id = s.user_id
                     WHERE s.last_used > ? GROUP BY s.user_id, s.device ORDER BY last DESC""", now - ONLINE):
        online.append({"userId": r["user_id"], "name": r["name"], "role": r["role"], "device": r["device"],
                       "ip": r["ip"], "last": r["last"]})
    if not users.accounts_enabled():  # accès libre : pas de sessions, on montre l'administrateur
        a = users.admin()
        online = [{"userId": a["id"], "name": a["name"], "role": "admin", "device": "", "ip": "", "last": now}]

    playing = []
    for r in db.q("SELECT k, v FROM meta WHERE k LIKE 'resume:%'"):
        try:
            st = json.loads(r["v"])
            uid = int(r["k"].split(":", 1)[1])
        except (ValueError, TypeError):
            continue
        if not st.get("playing") or now - (st.get("at") or 0) > PLAYING:
            continue
        u = users.get(uid)
        t = (M.tracks_by_ids([st.get("trackId")]) or [None])[0]
        if u and t:
            playing.append({"userId": uid, "name": u["name"], "device": st.get("device") or "", "at": st.get("at"),
                            "position": st.get("position") or 0,
                            "track": {k: t.get(k) for k in ("id", "title", "artist", "cover", "dur", "albumId")}})

    lib = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size), 0) s FROM tracks")
    try:
        du = shutil.disk_usage(config.MUSIC_DIR)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    per_user = []
    for u in users.all_users():
        q = quota.info(u["id"])
        per_user.append({"id": u["id"], "name": u["name"], "role": u["role"], **q, "lastSeen": u["lastSeen"],
                         "plays": u["plays"]})

    errors = [{"ts": r["ts"], "name": r["name"], "detail": r["detail"]}
              for r in db.q("SELECT ts, name, detail FROM activity WHERE action='import_error' ORDER BY id DESC LIMIT 30")]
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    plays_today = db.q1("SELECT COUNT(*) n FROM plays WHERE played_at >= ?", today)["n"]
    added_week = db.q1("SELECT COUNT(*) n FROM tracks WHERE added_at >= ?", now - 7 * 86400)["n"]
    return {"now": now, "online": online, "playing": playing,
            "library": {"tracks": lib["n"], "size": lib["s"]}, "disk": disk, "users": per_user,
            "errors": errors, "pwreqs": users.pwreqs(), "asks": asks.all_asks(40), "pendingAsks": asks.pending_count(),
            "playsToday": plays_today, "addedWeek": added_week, "autoDelay": config.ASK_AUTO_DELAY}
