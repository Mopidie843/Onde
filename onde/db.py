"""Base SQLite (WAL, une connexion par thread)."""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar

from . import config

_local = threading.local()
# Profil qui fait la requête en cours (1 = l'administrateur). Les requêtes SQL le lisent avec
# onde_uid() : likes, playlists, historique et statistiques sont propres à chaque profil.
current_uid: ContextVar[int] = ContextVar("onde_uid", default=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS covers(
    id TEXT PRIMARY KEY, color TEXT, w INT, h INT, created REAL
);
CREATE TABLE IF NOT EXISTS albums(
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    year INT,
    cover_id TEXT,
    cover_locked INT NOT NULL DEFAULT 0,
    cover_tried INT NOT NULL DEFAULT 0,
    added_at REAL,
    search TEXT
);
CREATE TABLE IF NOT EXISTS tracks(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    fp TEXT,
    ofp TEXT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album_id INT NOT NULL REFERENCES albums(id),
    track_no INT,
    disc_no INT,
    year INT,
    genre TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    bitrate INT,
    size INT,
    mtime REAL,
    ext TEXT,
    cover_id TEXT,
    added_at REAL,
    plays INT NOT NULL DEFAULT 0,
    last_played REAL,
    search TEXT
);
CREATE INDEX IF NOT EXISTS ix_tracks_album ON tracks(album_id, disc_no, track_no);
CREATE INDEX IF NOT EXISTS ix_tracks_fp ON tracks(fp);
CREATE INDEX IF NOT EXISTS ix_tracks_added ON tracks(added_at);
CREATE INDEX IF NOT EXISTS ix_tracks_played ON tracks(last_played);
CREATE INDEX IF NOT EXISTS ix_tracks_artist ON tracks(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_albums_artist ON albums(artist COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS likes(
    user_id INT NOT NULL DEFAULT 1,
    track_id INT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    liked_at REAL,
    PRIMARY KEY(user_id, track_id)
);
CREATE TABLE IF NOT EXISTS playlists(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover_id TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS playlist_tracks(
    id INTEGER PRIMARY KEY,
    playlist_id INT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INT NOT NULL,
    added_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pt ON playlist_tracks(playlist_id, position);
CREATE INDEX IF NOT EXISTS ix_pt_track ON playlist_tracks(track_id);
CREATE TABLE IF NOT EXISTS plays(
    id INTEGER PRIMARY KEY,
    track_id INT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at REAL NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'web'
);
CREATE INDEX IF NOT EXISTS ix_plays_time ON plays(played_at);
CREATE INDEX IF NOT EXISTS ix_plays_track ON plays(track_id);
CREATE TABLE IF NOT EXISTS lyrics(
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    synced TEXT,
    plain TEXT,
    source TEXT,
    instrumental INT NOT NULL DEFAULT 0,
    fetched REAL
);
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    n INT NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS smart_playlists(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rules TEXT NOT NULL,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw TEXT,
    email TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'listener',
    created REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL,
    last_used REAL,
    device TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invites(
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    created REAL,
    expires REAL,
    uses INT NOT NULL DEFAULT 0,
    max_uses INT NOT NULL DEFAULT 1,
    created_by INT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS resets(
    id TEXT PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL
);
CREATE TABLE IF NOT EXISTS pwreqs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS activity(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id INT,
    name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(ts);
CREATE TABLE IF NOT EXISTS asks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INT NOT NULL,
    kind TEXT NOT NULL,              -- 'track' (demande de titre) ou 'quota' (plus d'espace)
    query TEXT NOT NULL DEFAULT '',
    amount_mb INT NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, downloading, done, refused, failed
    created REAL NOT NULL,
    decided REAL,
    decided_by TEXT NOT NULL DEFAULT '',      -- 'admin' ou 'auto'
    reason TEXT NOT NULL DEFAULT '',
    track_id INT
);
CREATE INDEX IF NOT EXISTS ix_asks_status ON asks(status, created);
CREATE TABLE IF NOT EXISTS shares(
    token TEXT PRIMARY KEY,
    playlist_id INT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    owner_id INT NOT NULL,
    created REAL NOT NULL,
    views INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artists(
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    image_id TEXT,
    locked INT NOT NULL DEFAULT 0,
    tried REAL
);
"""

# colonnes ajoutées après la 1re version : (table, colonne, déclaration)
MIGRATIONS = [
    ("tracks", "ofp", "TEXT"),
    ("tracks", "lufs", "REAL"),
    ("tracks", "peak", "REAL"),
    ("tracks", "lufs_tried", "INT NOT NULL DEFAULT 0"),
    ("lyrics", "tries", "INT NOT NULL DEFAULT 0"),   # recherches en ligne restées sans résultat
    ("lyrics", "next_try", "REAL"),                   # date du prochain essai automatique
    ("lyrics", "align_state", "TEXT"),                # calage sur la voix : done, retry, mismatch, audio…
    ("lyrics", "align_tries", "INT NOT NULL DEFAULT 0"),
    ("lyrics", "align_next", "REAL"),
    ("lyrics", "align_error", "TEXT"),
    ("playlists", "owner_id", "INT NOT NULL DEFAULT 1"),      # profil propriétaire
    ("smart_playlists", "owner_id", "INT NOT NULL DEFAULT 1"),
    ("plays", "user_id", "INT NOT NULL DEFAULT 1"),           # qui a écouté
    ("tracks", "added_by", "INT"),                             # profil qui a importé le titre (espace utilisé)
    ("users", "quota_mb", "INT"),
    ("users", "terms_version", "TEXT"),                        # conditions d'utilisation acceptées
    ("users", "terms_at", "REAL"),                              # espace d'import (Mo) ; vide = valeur par défaut
]


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            config.DB_PATH, timeout=15, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        # comparaisons sans accents ni majuscules (règles des playlists intelligentes)
        conn.create_function("onde_norm", 1, norm, deterministic=True)
        conn.create_function("onde_uid", 0, current_uid.get)
        for pragma in (
            "journal_mode=WAL",
            "synchronous=NORMAL",
            "foreign_keys=ON",
            "temp_store=MEMORY",
            "cache_size=-16000",
            "mmap_size=134217728",
            "busy_timeout=15000",
        ):
            conn.execute("PRAGMA " + pragma)
        _local.conn = conn
    return conn


def _migrate_likes(conn) -> None:
    """Ancienne table des likes (un seul profil) → likes par profil (ceux d'avant vont à l'administrateur)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(likes)")}
    if not cols or "user_id" in cols:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE likes RENAME TO likes_old")
        conn.execute("""CREATE TABLE likes(user_id INT NOT NULL DEFAULT 1,
                        track_id INT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                        liked_at REAL, PRIMARY KEY(user_id, track_id))""")
        conn.execute("INSERT OR IGNORE INTO likes(user_id, track_id, liked_at) SELECT 1, track_id, liked_at FROM likes_old "
                     "WHERE track_id IN (SELECT id FROM tracks)")
        conn.execute("DROP TABLE likes_old")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init() -> None:
    config.ensure_dirs()
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate_likes(conn)
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tracks_ofp ON tracks(ofp)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tracks_lufs ON tracks(lufs_tried) WHERE lufs IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_likes_track ON likes(track_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_plays_user ON plays(user_id, played_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tracks_added_by ON tracks(added_by)")
    from . import users  # (import ici : users utilise db)
    users.ensure_admin()


def optimize() -> None:
    try:
        connect().execute("PRAGMA optimize")
    except sqlite3.Error:
        pass


def q(sql: str, *args) -> list[sqlite3.Row]:
    return connect().execute(sql, args).fetchall()


def q1(sql: str, *args) -> sqlite3.Row | None:
    return connect().execute(sql, args).fetchone()


def x(sql: str, *args) -> sqlite3.Cursor:
    return connect().execute(sql, args)


def many(sql: str, rows) -> sqlite3.Cursor:
    return connect().executemany(sql, rows)


@contextmanager
def tx():
    """Transaction explicite (réentrante)."""
    conn = connect()
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


_ws = re.compile(r"\s+")


def norm(text: str | None) -> str:
    """Minuscules, sans accents, espaces normalisés (pour recherche et regroupement)."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _ws.sub(" ", s.casefold()).strip()


def like_terms(query: str) -> list[str]:
    out = []
    for word in norm(query).split(" "):
        if word:
            word = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            out.append(f"%{word}%")
    return out
