"""Espace disque par profil : chaque titre importé est compté pour le profil qui l'a ajouté.

L'administrateur n'a pas de limite. Les autres ont une valeur par défaut (réglable avec ONDE_QUOTA_MB
et ONDE_QUOTA_LISTENER_MB) que l'administrateur peut changer profil par profil. Quand c'est plein,
l'import est refusé avec un message clair, et le profil peut demander plus d'espace (voir asks.py).
"""
from __future__ import annotations

from . import config, db, users

MB = 1024 * 1024


class QuotaFull(Exception):
    """Plus de place pour ce profil."""


def limit_mb(user_id: int) -> int | None:
    """Limite en Mo (None : illimité)."""
    row = db.q1("SELECT role, quota_mb FROM users WHERE id=?", user_id)
    if not row or row["role"] == "admin":
        return None
    if row["quota_mb"] is not None:
        return max(0, int(row["quota_mb"]))
    return config.QUOTA_MEMBER_MB if row["role"] == "member" else config.QUOTA_LISTENER_MB


def used_bytes(user_id: int) -> int:
    row = db.q1("SELECT COALESCE(SUM(size), 0) s FROM tracks WHERE added_by=?", user_id)
    return int(row["s"] or 0)


def info(user_id: int) -> dict:
    lim = limit_mb(user_id)
    used = used_bytes(user_id)
    n = db.q1("SELECT COUNT(*) n FROM tracks WHERE added_by=?", user_id)["n"]
    return {"used": used, "limit": None if lim is None else lim * MB, "limitMb": lim, "tracks": n,
            "full": lim is not None and used >= lim * MB,
            "pct": None if not lim else min(100, round(used * 100 / (lim * MB)))}


def _fmt(n: int) -> str:
    return f"{n / (1024 * MB):.1f} Go".replace(".", ",") if n >= 1024 * MB else f"{round(n / MB)} Mo"


def check(user_id: int | None = None, extra: int = 0) -> None:
    """Lève QuotaFull si `extra` octets de plus dépassent la limite du profil."""
    user_id = users.uid() if user_id is None else user_id
    lim = limit_mb(user_id)
    if lim is None:
        return
    used = used_bytes(user_id)
    if used + max(0, extra) > lim * MB or used >= lim * MB:
        raise QuotaFull(f"Ton espace est plein ({_fmt(used)} sur {_fmt(lim * MB)}). "
                        "Demande plus de place à l'administrateur (Réglages → Mon profil).")


def track_ids(res: dict) -> list[int]:
    tracks = res.get("tracks") or ([res["track"]] if res.get("track") else [])
    return [int(t["id"]) for t in tracks if t and t.get("id")]


def mark(res: dict, user_id: int | None = None) -> dict:
    """Compte les titres ajoutés par cet import pour le profil (les doublons déjà là ne comptent pas)."""
    if res.get("status") != "added":
        return res
    user_id = users.uid() if user_id is None else user_id
    for tid in track_ids(res):
        db.x("UPDATE tracks SET added_by=? WHERE id=? AND added_by IS NULL", user_id, tid)
    return res


def set_limit(user_id: int, mb: int | None) -> None:
    db.x("UPDATE users SET quota_mb=? WHERE id=?", None if mb is None else max(0, int(mb)), user_id)
