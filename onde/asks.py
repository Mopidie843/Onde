"""Demandes à l'administrateur : un titre à ajouter (profil « écoute seule ») ou plus d'espace disque.

Demande de titre : l'administrateur l'accepte ou la refuse d'un clic (tableau de bord). S'il ne répond
pas dans les 5 minutes, une mini IA décide toute seule, sur trois indices :
  * le nom demandé : vide, trop vague, version « slowed / nightcore / 1 heure… » → non ;
  * l'artiste : déjà des titres de lui dans la bibliothèque → bon signe ; le titre déjà là → non (doublon) ;
  * les paroles et les infos en ligne (LRCLIB, Deezer) : elles existent → c'est une vraie chanson ;
    paroles haineuses → non.
C'est une petite IA « à règles », locale : rien n'est envoyé à un service d'IA externe.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from . import clean, config, db, notify, quota, tidy, users

log = logging.getLogger("onde.asks")

MAX_PENDING = 5          # demandes en attente par profil
MAX_AUTO_PER_DAY = 15    # titres acceptés automatiquement par profil et par jour
_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()

# versions retouchées qu'on ne veut pas dans la bibliothèque
JUNK = re.compile(r"\b(nightcore|sped ?up|speed ?up|8d( audio)?|bass ?boost(ed)?|earrape|"
                  r"\d+\s*(h|heures?|hours?)\b|1 hour|10 hours|loop|type beat|karaoke|instrumental version)\b", re.I)
# paroles clairement haineuses (appel à la haine) : la demande est refusée
HATE = re.compile(r"\b(sieg heil|heil hitler|white power|mort aux (juifs|arabes|noirs|musulmans|chretiens)|"
                  r"gas the|kill all (jews|blacks|muslims))\b", re.I)


# ------------------------------------------------------------------ lecture
def _row(r) -> dict:
    return {"id": r["id"], "userId": r["user_id"], "name": r["name"] if "name" in r.keys() else "",
            "kind": r["kind"], "query": r["query"], "amountMb": r["amount_mb"], "note": r["note"],
            "status": r["status"], "created": r["created"], "decided": r["decided"], "decidedBy": r["decided_by"],
            "reason": r["reason"], "trackId": r["track_id"],
            "autoAt": r["created"] + config.ASK_AUTO_DELAY if r["kind"] == "track" and r["status"] == "pending" else None}


def get(ask_id: int) -> dict | None:
    r = db.q1("SELECT a.*, u.name FROM asks a LEFT JOIN users u ON u.id = a.user_id WHERE a.id=?", ask_id)
    return _row(r) if r else None


def mine(user_id: int, limit: int = 30) -> list[dict]:
    return [_row(r) for r in db.q("""SELECT a.*, u.name FROM asks a LEFT JOIN users u ON u.id = a.user_id
                                     WHERE a.user_id=? ORDER BY a.id DESC LIMIT ?""", user_id, limit)]


def all_asks(limit: int = 60) -> list[dict]:
    return [_row(r) for r in db.q("""SELECT a.*, u.name FROM asks a LEFT JOIN users u ON u.id = a.user_id
                                     ORDER BY (a.status = 'pending') DESC, a.id DESC LIMIT ?""", limit)]


def pending_count() -> int:
    return db.q1("SELECT COUNT(*) n FROM asks WHERE status='pending'")["n"]


# ------------------------------------------------------------------ création
def _check_pending(user_id: int) -> None:
    if db.q1("SELECT COUNT(*) n FROM asks WHERE user_id=? AND status='pending'", user_id)["n"] >= MAX_PENDING:
        raise ValueError(f"Tu as déjà {MAX_PENDING} demandes en attente : attends une réponse.")


def ask_track(query: str, note: str = "") -> dict:
    me = users.current()
    query = " ".join((query or "").split())[:200]
    if len(query) < 3:
        raise ValueError("Écris le titre et l'artiste (ex. « Bigflo et Oli - Alors alors »).")
    _check_pending(me["id"])
    if db.q1("SELECT 1 FROM asks WHERE user_id=? AND kind='track' AND status IN ('pending','downloading') "
             "AND query=? COLLATE NOCASE", me["id"], query):
        raise ValueError("Tu as déjà demandé ce titre : la réponse arrive.")
    cur = db.x("INSERT INTO asks(user_id, kind, query, note, created) VALUES(?,?,?,?,?)",
               me["id"], "track", query, (note or "")[:200], time.time())
    notify.add("ask", f"Demande de {me['name']}", f"« {query} » — réponds avant 5 min, sinon la mini IA décide",
               "#/dashboard")
    users.log_activity("ask", f"titre demandé : {query}")
    return get(cur.lastrowid)


def ask_quota(amount_mb: int, note: str = "") -> dict:
    me = users.current()
    if quota.limit_mb(me["id"]) is None:
        raise ValueError("Tu n'as pas de limite d'espace.")
    amount_mb = max(100, min(int(amount_mb or 0), 200_000))
    _check_pending(me["id"])
    if db.q1("SELECT 1 FROM asks WHERE user_id=? AND kind='quota' AND status='pending'", me["id"]):
        raise ValueError("Ta demande d'espace est déjà envoyée.")
    cur = db.x("INSERT INTO asks(user_id, kind, amount_mb, note, created) VALUES(?,?,?,?,?)",
               me["id"], "quota", amount_mb, (note or "")[:200], time.time())
    notify.add("ask", f"{me['name']} demande plus d'espace", f"+{amount_mb} Mo", "#/dashboard")
    users.log_activity("ask", f"plus d'espace demandé : +{amount_mb} Mo")
    return get(cur.lastrowid)


def cancel(ask_id: int, user_id: int) -> None:
    db.x("DELETE FROM asks WHERE id=? AND user_id=? AND status='pending'", ask_id, user_id)


# ------------------------------------------------------------------ décisions
def _decide(ask_id: int, status: str, by: str, reason: str = "") -> bool:
    """Passe la demande de « pending » à `status` (une seule fois : l'admin et l'IA ne décident pas deux fois)."""
    with _lock:
        cur = db.x("UPDATE asks SET status=?, decided=?, decided_by=?, reason=? WHERE id=? AND status='pending'",
                   status, time.time(), by, reason[:400], ask_id)
        return cur.rowcount > 0


def refuse(ask_id: int, reason: str = "", by: str = "admin") -> dict:
    a = get(ask_id)
    if not a:
        raise KeyError(ask_id)
    if not _decide(ask_id, "refused", by, reason or "Refusé par l'administrateur"):
        raise ValueError("Cette demande a déjà une réponse.")
    return get(ask_id)


def approve(ask_id: int, by: str = "admin", reason: str = "", wait: bool = False) -> dict:
    a = get(ask_id)
    if not a:
        raise KeyError(ask_id)
    if a["kind"] == "quota":
        if not _decide(ask_id, "done", by, reason or f"+{a['amountMb']} Mo accordés"):
            raise ValueError("Cette demande a déjà une réponse.")
        cur = quota.limit_mb(a["userId"])
        if cur is not None:
            quota.set_limit(a["userId"], cur + a["amountMb"])
        return get(ask_id)
    if not _decide(ask_id, "downloading", by, reason or "Accepté : téléchargement en cours"):
        raise ValueError("Cette demande a déjà une réponse.")
    t = threading.Thread(target=_download, args=(ask_id,), name="onde-ask", daemon=True)
    t.start()
    if wait:
        t.join(600)
    return get(ask_id)


def _download(ask_id: int) -> None:
    from . import downloader  # (import tardif : downloader est lourd)
    a = get(ask_id)
    user = users.get(a["userId"]) if a else None
    if not a or not user:
        return
    with users.use(user):
        try:
            res = downloader.search_download(a["query"])
            quota.mark(res, user["id"])
            ids = quota.track_ids(res)
            ok = res.get("status") in ("added", "duplicate")
            db.x("UPDATE asks SET status=?, track_id=?, reason=? WHERE id=?", "done" if ok else "failed",
                 ids[0] if ids else None,
                 (a["reason"] + (" — ajouté" if res.get("status") == "added" else " — déjà dans la bibliothèque"))
                 if ok else f"{a['reason']} — import impossible : {res.get('error') or 'introuvable'}", ask_id)
            if ok:
                users.log_activity("download", f"demande acceptée : {a['query']}")
            else:
                users.log_activity("import_error", f"{a['query']} : {res.get('error') or 'introuvable'}")
        except Exception as exc:
            db.x("UPDATE asks SET status='failed', reason=? WHERE id=?", f"{a['reason']} — import impossible : {exc}"[-400:], ask_id)
            users.log_activity("import_error", f"{a['query']} : {exc}")
            log.info("demande %s : %s", ask_id, exc)


# ------------------------------------------------------------------ la mini IA
def _artist_count(name: str) -> int:
    n = db.norm(name)
    if not n:
        return 0
    return db.q1("SELECT COUNT(*) n FROM tracks WHERE onde_norm(artist) = ? OR onde_norm(artist) LIKE ? "
                 "OR onde_norm(artist) LIKE ?", n, n + " %", "% " + n)["n"]


def _known_artist_in(query: str) -> tuple[str, int]:
    """Un artiste de la bibliothèque écrit dans la demande (« jul bande organisée » → Jul)."""
    q = f" {db.norm(query)} "
    best = ("", 0)
    for r in db.q("SELECT artist, COUNT(*) n FROM tracks GROUP BY onde_norm(artist)"):
        a = db.norm(r["artist"])
        if len(a) >= 2 and f" {a} " in q and (len(a), r["n"]) > (len(best[0]), best[1]):
            best = (r["artist"], r["n"])
    return best


def evaluate(query: str, user_id: int, online: bool = True) -> dict:
    """Avis de la mini IA : {"ok": bool, "score": int, "reasons": [...]}."""
    from . import first_run
    reasons: list[str] = []
    parsed = clean.parse(query)
    parts = parsed.parts
    if len(" ".join(parts)) < 3:
        return {"ok": False, "score": 0, "reasons": ["demande trop vague"]}
    bad = tidy.unwanted(query, "") or (JUNK.search(clean.fold(query)) and JUNK.search(clean.fold(query)).group(0))
    if bad:
        return {"ok": False, "score": 0, "reasons": [f"version retouchée (« {bad} ») : pas dans la bibliothèque"]}
    q = quota.info(user_id)
    if q["full"]:
        return {"ok": False, "score": 0, "reasons": ["l'espace du profil est plein"]}
    since = time.time() - 86400
    if db.q1("SELECT COUNT(*) n FROM asks WHERE user_id=? AND decided_by='auto' AND status IN ('downloading','done') "
             "AND decided > ?", user_id, since)["n"] >= MAX_AUTO_PER_DAY:
        return {"ok": False, "score": 0, "reasons": [f"déjà {MAX_AUTO_PER_DAY} titres acceptés aujourd'hui"]}

    # artiste et titre probables
    guesses: list[tuple[str, str]] = []
    if len(parts) >= 2:
        a, b = parts[0], " ".join(parts[1:])
        guesses = [(a, b), (b, a)] if parsed.artist_first else [(b, a), (a, b)]
    meta = None
    if online:
        try:
            from . import downloader
            meta = downloader._lookup_meta(" ".join(parts), parts)
        except Exception as exc:
            log.info("mini IA : infos en ligne indisponibles (%s)", exc)
    if meta and meta.get("artist") and meta.get("title"):
        guesses.insert(0, (meta["artist"], meta["title"]))

    for artist, title in guesses:
        if first_run.in_library(artist, title):
            return {"ok": False, "score": 0, "reasons": [f"« {title} » de {artist} est déjà dans la bibliothèque"]}

    score = 0
    artist_name, n = "", 0
    for artist, _ in guesses:
        c = _artist_count(artist)
        if c > n:
            artist_name, n = artist, c
    if not n:
        artist_name, n = _known_artist_in(query)
    if n:
        score += 2
        reasons.append(f"{artist_name} a déjà {n} titre{'s' if n > 1 else ''} dans la bibliothèque")
    else:
        reasons.append("artiste pas encore dans la bibliothèque")
    if meta:
        score += 1
        reasons.append(f"trouvé en ligne : {meta.get('title')} — {meta.get('artist')}")

    if online and guesses:
        try:
            from . import lyrics
            artist, title = guesses[0]
            got = lyrics.from_lrclib(title, artist, "", float((meta or {}).get("duration") or 0))
            text = (got or {}).get("plain") or (got or {}).get("synced") or ""
            if got and (text or got.get("instrumental")):
                if HATE.search(clean.fold(text)):
                    return {"ok": False, "score": score, "reasons": reasons + ["paroles haineuses"]}
                score += 1
                reasons.append("instrumental connu" if got.get("instrumental") and not text else "paroles trouvées : vraie chanson")
            else:
                reasons.append("pas de paroles trouvées")
        except Exception as exc:
            reasons.append("paroles pas vérifiables pour l'instant")
            log.info("mini IA : paroles indisponibles (%s)", exc)
    ok = score >= 1
    if not ok:
        reasons.append("rien ne confirme que c'est une vraie chanson")
    return {"ok": ok, "score": score, "reasons": reasons}


def auto_decide(ask_id: int, online: bool = True, wait: bool = False) -> dict | None:
    a = get(ask_id)
    if not a or a["status"] != "pending" or a["kind"] != "track":
        return a
    verdict = evaluate(a["query"], a["userId"], online=online)
    why = "Mini IA : " + " · ".join(verdict["reasons"])
    try:
        if verdict["ok"]:
            return approve(ask_id, by="auto", reason=why, wait=wait)
        return refuse(ask_id, reason=why, by="auto")
    except ValueError:  # l'admin a répondu entre-temps
        return get(ask_id)


def tick(now: float | None = None, online: bool = True, wait: bool = False) -> int:
    """Décide les demandes de titres restées sans réponse depuis 5 minutes. Renvoie leur nombre."""
    now = time.time() if now is None else now
    rows = db.q("SELECT id FROM asks WHERE kind='track' AND status='pending' AND created <= ? ORDER BY id",
                now - config.ASK_AUTO_DELAY)
    for r in rows:
        try:
            auto_decide(r["id"], online=online, wait=wait)
        except Exception:
            log.exception("décision automatique de la demande %s", r["id"])
    return len(rows)


def _loop() -> None:
    while not _stop.wait(20):
        try:
            tick()
        except Exception:
            log.exception("demandes")


def start_background() -> None:
    global _thread
    _stop.clear()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="onde-asks", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
