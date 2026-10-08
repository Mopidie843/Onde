"""Playlists intelligentes : des règles (genre, année, écoutes, likés…) plutôt qu'une liste figée.

Une playlist intelligente se remplit toute seule et suit la bibliothèque : un titre qui correspond aux
règles y apparaît dès son import, un titre écouté peut en sortir (« jamais écoutés »), etc.

Sécurité : les champs, opérateurs et tris viennent d'une liste blanche, et chaque valeur passe en
paramètre SQL — une règle ne peut jamais injecter de SQL.
"""
from __future__ import annotations

import datetime
import json
import math
import time

from . import api_models as M
from . import db

MAX_RULES = 20
MAX_LIMIT = 2000

# champ -> (type, expression SQL, libellé)
FIELDS: dict[str, tuple[str, str, str]] = {
    "title": ("text", "t.title", "Titre"),
    "artist": ("text", "t.artist", "Artiste"),
    "album": ("text", "a.name", "Album"),
    "genre": ("text", "t.genre", "Genre"),
    "year": ("number", "COALESCE(t.year, a.year)", "Année"),
    "plays": ("number", "t.plays", "Écoutes"),
    "duration": ("number", "t.duration", "Durée"),
    "added": ("date", "t.added_at", "Ajouté"),
    "played": ("date", "t.last_played", "Dernière écoute"),
    "liked": ("bool", "(l.track_id IS NOT NULL)", "Liké"),
    "format": ("enum", "LOWER(LTRIM(COALESCE(t.ext, ''), '.'))", "Format"),
}
OPS: dict[str, set[str]] = {
    "text": {"contains", "not_contains", "is", "is_not", "starts"},
    "number": {"eq", "ne", "gt", "lt", "between"},
    "date": {"in_last", "not_in_last", "never"},
    "bool": {"is_true", "is_false"},
    "enum": {"is", "is_not"},
}
SORTS: dict[str, str | None] = {
    "random": None,  # aléatoire, mais stable toute la semaine
    "plays": "t.plays DESC, t.last_played DESC",
    "added": "t.added_at DESC",
    "played": "t.last_played DESC",
    "title": "t.title COLLATE NOCASE",
    "artist": "t.artist COLLATE NOCASE, a.name COLLATE NOCASE, t.disc_no, t.track_no",
    "year": "COALESCE(t.year, a.year) DESC, a.name COLLATE NOCASE, t.disc_no, t.track_no",
    "year_asc": "COALESCE(t.year, a.year), a.name COLLATE NOCASE, t.disc_no, t.track_no",
    "duration": "t.duration DESC",
}


def _num(v, what: str) -> float:
    try:
        n = float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        raise ValueError(f"« {what} » doit être un nombre")
    if not math.isfinite(n):  # « nan », « inf » : JSON invalide pour les navigateurs
        raise ValueError(f"« {what} » doit être un nombre")
    return n


def validate(spec: dict) -> dict:
    """Vérifie et normalise des règles venant de l'interface. Lève ValueError (message en français)."""
    if not isinstance(spec, dict):
        raise ValueError("Règles invalides")
    match = spec.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError("« match » doit valoir all ou any")
    rules_in = spec.get("rules") or []
    if not isinstance(rules_in, list) or len(rules_in) > MAX_RULES:
        raise ValueError(f"Au plus {MAX_RULES} règles")
    rules = []
    for r in rules_in:
        if not isinstance(r, dict):
            raise ValueError("Règle invalide")
        field, op = r.get("field"), r.get("op")
        if field not in FIELDS:
            raise ValueError(f"Champ inconnu : {field}")
        kind, _, label = FIELDS[field]
        if op not in OPS[kind]:
            raise ValueError(f"Condition inconnue pour « {label} » : {op}")
        value = r.get("value")
        if kind in ("text", "enum"):
            value = str(value or "").strip()[:200]
            if not value:
                raise ValueError(f"Indique une valeur pour « {label} »")
        elif kind == "number":
            if op == "between":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise ValueError(f"« {label} » : indique deux valeurs")
                value = sorted([_num(value[0], label), _num(value[1], label)])
            else:
                value = _num(value, label)
        elif kind == "date":
            value = None if op == "never" else max(0.0, min(_num(value, label), 36500))
        else:
            value = None
        rules.append({"field": field, "op": op, "value": value})
    sort = spec.get("sort", "random")
    if sort not in SORTS:
        raise ValueError(f"Tri inconnu : {sort}")
    limit = spec.get("limit")
    if limit in (None, "", 0, "0"):
        limit = None
    else:
        limit = int(max(1, min(_num(limit, "Nombre de titres"), MAX_LIMIT)))
    return {"match": match, "rules": rules, "sort": sort, "limit": limit}


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clause(r: dict, now: float) -> tuple[str, list]:
    kind, expr, _ = FIELDS[r["field"]]
    op, v = r["op"], r["value"]
    if kind == "text":
        nexpr, nv = f"onde_norm({expr})", db.norm(v)
        return {
            "contains": (f"{nexpr} LIKE ? ESCAPE '\\'", [f"%{_like(nv)}%"]),
            "not_contains": (f"{nexpr} NOT LIKE ? ESCAPE '\\'", [f"%{_like(nv)}%"]),
            "starts": (f"{nexpr} LIKE ? ESCAPE '\\'", [f"{_like(nv)}%"]),
            "is": (f"{nexpr} = ?", [nv]),
            "is_not": (f"{nexpr} <> ?", [nv]),
        }[op]
    if kind == "number":
        if op == "between":
            return f"{expr} BETWEEN ? AND ?", list(v)
        return f"{expr} {({'eq': '=', 'ne': '<>', 'gt': '>', 'lt': '<'})[op]} ?", [v]
    if kind == "date":
        if op == "never":
            return f"{expr} IS NULL", []
        since = now - v * 86400
        if op == "in_last":
            return f"{expr} >= ?", [since]
        return f"({expr} IS NULL OR {expr} < ?)", [since]
    if kind == "bool":
        return (expr if op == "is_true" else f"NOT {expr}"), []
    return ({"is": f"{expr} = ?", "is_not": f"{expr} <> ?"})[op], [v.lower().lstrip(".")]


def build(spec: dict) -> tuple[str, list, str, int | None]:
    """(where, paramètres, tri, limite) pour M.tracks()."""
    now = time.time()
    parts, args = [], []
    for r in spec["rules"]:
        sql, a = _clause(r, now)
        parts.append(f"({sql})")
        args.extend(a)
    where = (" AND " if spec["match"] == "all" else " OR ").join(parts)
    order = SORTS[spec["sort"]]
    if order is None:  # « aléatoire » : même ordre toute la semaine (téléchargements hors connexion stables)
        year, week, _ = datetime.date.today().isocalendar()
        seed = year * 100 + week
        order = f"((t.id * 2654435761 + {seed}) % 4294967296)"
    return where, args, order, spec["limit"]


def tracks(spec: dict) -> list[dict]:
    where, args, order, limit = build(spec)
    return M.tracks(where, *args, order=order, limit=limit or MAX_LIMIT)


# ------------------------------------------------------------------ enregistrement
def _row(r, with_tracks: bool = False) -> dict:
    spec = json.loads(r["rules"])
    items = tracks(spec)
    covers = []
    for t in items:
        if t["cover"] and t["cover"] not in covers:
            covers.append(t["cover"])
        if len(covers) >= 4:
            break
    out = {
        "id": r["id"], "name": r["name"], "spec": spec, "count": len(items),
        "dur": round(sum(t["dur"] for t in items)), "mosaic": covers, "cover": None,
        "created": r["created_at"], "updated": r["updated_at"], "smart": True,
    }
    if with_tracks:
        out["tracks"] = items
    return out


def all_smart() -> list[dict]:
    return [_row(r) for r in db.q("SELECT * FROM smart_playlists WHERE owner_id=onde_uid() ORDER BY created_at")]


def get(sid: int) -> dict | None:
    r = db.q1("SELECT * FROM smart_playlists WHERE id=? AND owner_id=onde_uid()", sid)
    return _row(r, with_tracks=True) if r else None


def create(name: str, spec: dict) -> dict:
    spec = validate(spec)
    name = (name or "").strip()[:120] or "Playlist intelligente"
    now = time.time()
    cur = db.x("INSERT INTO smart_playlists(name, rules, created_at, updated_at, owner_id) VALUES(?,?,?,?,onde_uid())",
               name, json.dumps(spec), now, now)
    return get(cur.lastrowid)


def update(sid: int, name: str | None, spec: dict | None) -> dict | None:
    r = db.q1("SELECT * FROM smart_playlists WHERE id=? AND owner_id=onde_uid()", sid)
    if not r:
        return None
    new_name = r["name"] if name is None else ((name or "").strip()[:120] or r["name"])
    new_spec = r["rules"] if spec is None else json.dumps(validate(spec))
    db.x("UPDATE smart_playlists SET name=?, rules=?, updated_at=? WHERE id=? AND owner_id=onde_uid()", new_name, new_spec, time.time(), sid)
    return get(sid)


def delete(sid: int) -> bool:
    return db.x("DELETE FROM smart_playlists WHERE id=? AND owner_id=onde_uid()", sid).rowcount > 0
