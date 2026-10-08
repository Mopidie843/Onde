"""Résolution des playlists en ligne, surtout Spotify et ses méthodes de secours.

Spotify change régulièrement ses pages : si la méthode principale (le JSON __NEXT_DATA__ de la page
« embed ») disparaît, Onde doit quand même retrouver les titres par une autre voie, ou au moins
le nom de la playlist pour un message clair. Le réseau est simulé (fixture `http`, conftest.py).
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from onde import downloader

PID = "37i9dQZF1DXcBWIGoYBM5M"
EMBED = f"https://open.spotify.com/embed/playlist/{PID}"
PAGE = f"https://open.spotify.com/playlist/{PID}"
OEMBED = "https://open.spotify.com/oembed"
URL = f"https://open.spotify.com/intl-fr/playlist/{PID}?si=abc123"

EXPECTED = ["Artiste A - Titre A", "Artiste B, Artiste C - Titre B", "Artiste D - Été à Paris"]


def html(body: str, head: str = "") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          content=f"<!doctype html><html><head>{head}</head><body>{body}</body></html>".encode())


def embed_tracks() -> list[dict]:
    return [
        {"uri": "spotify:track:1", "uid": "u1", "title": "Titre A", "subtitle": "Artiste A", "duration": 200000},
        {"uri": "spotify:track:2", "uid": "u2", "title": "Titre B", "subtitle": "Artiste B, Artiste C", "duration": 180000},
        {"uri": "spotify:track:3", "uid": "u3", "title": "Été à Paris", "subtitle": "Artiste D", "duration": 210000},
    ]


def next_data(entity: dict) -> str:
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


OG_TITLE = '<meta property="og:title" content="Road Trip &amp; Co"/>'


# --------------------------------------------------------------------------- détection des liens
@pytest.mark.parametrize("url, kind", [
    (URL, "spotify"),
    (f"https://open.spotify.com/embed/playlist/{PID}", "spotify"),
    (f"spotify:playlist:{PID}", "spotify"),
    ("https://www.deezer.com/fr/playlist/908622995", "deezer"),
    ("https://www.youtube.com/playlist?list=PL123", "youtube"),
    ("https://music.youtube.com/watch?v=x&list=RD123", "youtube"),
    ("https://open.spotify.com/track/abc", None),
    ("Daft Punk - One More Time", None),
])
def test_playlist_kind(url, kind):
    assert downloader.playlist_kind(url) == kind


# --------------------------------------------------------------------------- méthode principale
def test_spotify_main_method_next_data(http):
    http.routes[EMBED] = html(next_data({"type": "playlist", "name": "Road Trip", "trackList": embed_tracks()}))
    out = downloader.resolve_playlist(URL)
    assert out == {"name": "Road Trip", "kind": "spotify", "queries": EXPECTED}
    assert [str(r.url) for r in http.requests] == [EMBED]  # une seule requête suffit


# --------------------------------------------------------------------------- méthodes de secours
def test_fallback_when_next_data_missing_uses_flight_data(http):
    """Page passée à l'App Router de Next.js : plus de __NEXT_DATA__, les données sont dans
    self.__next_f.push([1, "…"]) (texte JSON échappé dans une chaîne JavaScript)."""
    row = "5:" + json.dumps({"entity": {"name": "Road Trip", "trackList": embed_tracks()}}) + "\n"
    script = f"<script>self.__next_f.push([1,{json.dumps(row)}])</script>"
    http.routes[EMBED] = html(script, head=OG_TITLE)
    name, queries = downloader._resolve_spotify_playlist(URL)
    assert queries == EXPECTED
    assert name == "Road Trip & Co"  # pas de nom dans le JSON : balise og:title (entités décodées)


def test_fallback_when_next_data_is_unreadable(http):
    """__NEXT_DATA__ présent mais cassé, et les titres seulement dans un autre bloc JSON."""
    ld = {"@type": "MusicPlaylist", "name": "Road Trip", "track": [
        {"@type": "MusicRecording", "name": "Titre A", "byArtist": {"@type": "MusicGroup", "name": "Artiste A"}},
        {"@type": "MusicRecording", "name": "Titre B", "byArtist": [{"name": "Artiste B"}, {"name": "Artiste C"}]},
        {"@type": "MusicRecording", "name": "Été à Paris", "byArtist": {"name": "Artiste D"}},
    ]}
    body = ('<script id="__NEXT_DATA__" type="application/json">{pas du json</script>'
            f'<script type="application/ld+json">{json.dumps(ld)}</script>')
    http.routes[EMBED] = html(body, head=OG_TITLE)
    assert downloader._resolve_spotify_playlist(URL) == ("Road Trip & Co", EXPECTED)


def test_fallback_public_page_initial_state_base64(http):
    """Page « embed » vide : on passe à la page publique, dont l'état est un JSON en base64."""
    state = {"entities": {"items": {f"spotify:playlist:{PID}": {"name": "Road Trip", "content": {"items": [
        {"itemV2": {"data": {"name": "Titre A", "artists": {"items": [{"profile": {"name": "Artiste A"}}]}}}},
        {"itemV2": {"data": {"name": "Titre B", "artists": {"items": [{"profile": {"name": "Artiste B"}},
                                                                     {"profile": {"name": "Artiste C"}}]}}}},
        {"itemV2": {"data": {"name": "Été à Paris", "artists": {"items": [{"profile": {"name": "Artiste D"}}]}}}},
    ]}}}}}
    b64 = base64.b64encode(json.dumps(state).encode()).decode()
    http.routes[EMBED] = html("<div>chargement…</div>")
    http.routes[PAGE] = html(f'<script id="initial-state" type="text/plain">{b64}</script>', head=OG_TITLE)
    assert downloader._resolve_spotify_playlist(URL) == ("Road Trip & Co", EXPECTED)
    assert [str(r.url) for r in http.requests] == [EMBED, PAGE]


def _row(tid: str, title: str, artists: list[str]) -> str:
    links = ", ".join('<a href="/artist/%s%d">%s</a>' % (tid, i, a) for i, a in enumerate(artists))
    return ('<div role="row"><a href="/track/%s"><img alt=""></a><a href="/track/%s"><span>%s</span></a>'
            "<span>%s</span></div>" % (tid, tid, title, links))


def test_fallback_server_rendered_links(http):
    """Aucun JSON exploitable : les liens /track/… et /artist/… du HTML rendu côté serveur."""
    rows = (_row("A1", "Titre A", ["Artiste A"]) + _row("B2", "Titre B", ["Artiste B", "Artiste C"])
            + _row("C3", "&Eacute;t&eacute; &agrave; Paris", ["Artiste D"]))
    http.routes[EMBED] = html("")
    http.routes[PAGE] = html(rows, head="<title>Road Trip - playlist by Moi | Spotify</title>")
    assert downloader._resolve_spotify_playlist(URL) == ("Road Trip", EXPECTED)


def test_name_only_gives_a_clear_message(http):
    """Si aucun titre n'est lisible, on récupère au moins le nom de la playlist (og:title) pour le
    message d'erreur, sans inventer de titres."""
    http.routes[EMBED] = html("<div></div>", head=OG_TITLE)
    http.routes[PAGE] = html("<div></div>", head=OG_TITLE)
    with pytest.raises(RuntimeError, match="« Road Trip & Co » a bien été trouvée"):
        downloader._resolve_spotify_playlist(URL)


def test_name_from_oembed_when_pages_have_none(http):
    http.routes[EMBED] = html("<div></div>")
    http.routes[PAGE] = html("<div></div>", head="<title>Spotify – Web Player</title>")
    http.routes[OEMBED] = httpx.Response(200, json={"title": "Road Trip", "type": "rich"})
    with pytest.raises(RuntimeError, match="« Road Trip »"):
        downloader._resolve_spotify_playlist(URL)
    oembed = [r for r in http.requests if str(r.url).startswith(OEMBED)]
    assert oembed and oembed[0].url.params["url"] == PAGE


# --------------------------------------------------------------------------- erreurs réseau et HTTP
def test_unreachable_spotify_stops_early(http):
    def refused(request):
        raise httpx.ConnectError("Name or service not known", request=request)
    http.routes[EMBED] = refused
    with pytest.raises(RuntimeError, match="Spotify injoignable"):
        downloader._resolve_spotify_playlist(URL)
    assert {str(r.url) for r in http.requests} == {EMBED}  # pas d'autres requêtes inutiles


def test_missing_playlist(http):
    with pytest.raises(RuntimeError, match="introuvable"):  # 404 partout (route non prévue)
        downloader._resolve_spotify_playlist(URL)


def test_rate_limited(http):
    http.routes[EMBED] = httpx.Response(429, headers={"retry-after": "0"})
    with pytest.raises(RuntimeError, match="trop de demandes"):
        downloader._resolve_spotify_playlist(URL)


def test_deezer_playlist_retries_quota(http, monkeypatch):
    """L'API Deezer renvoie son dépassement de quota en HTTP 200 : on patiente puis on réessaie."""
    from onde import covers
    monkeypatch.setattr(covers, "DEEZER_QUOTA_WAIT", 0.0)
    answers = iter([
        httpx.Response(200, json={"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}),
        httpx.Response(200, json={"title": "Ma playlist", "tracks": {"data": [
            {"title": "Titre A", "artist": {"name": "Artiste A"}}]}}),
    ])
    http.routes["https://api.deezer.com/playlist/908622995"] = lambda request: next(answers)
    out = downloader.resolve_playlist("https://www.deezer.com/fr/playlist/908622995")
    assert out == {"name": "Ma playlist", "kind": "deezer", "queries": ["Artiste A - Titre A"]}


# ---------------------------------------------------------------- recherche par nom (YouTube + SoundCloud)
@pytest.mark.parametrize("url, kind", [
    ("https://soundcloud.com/bigflo-oli/sets/la-vraie-vie", "soundcloud"),
    ("https://soundcloud.com/bigflo-oli", "soundcloud"),
    ("https://soundcloud.com/bigflo-oli/likes", "soundcloud"),
    ("https://soundcloud.com/bigflo-oli/alors-alors", None),  # un seul titre
    ("https://soundcloud.com/discover", None),
])
def test_soundcloud_playlist_kind(url, kind):
    assert downloader.playlist_kind(url) == kind


def test_fold_ignores_links_between_artists():
    assert downloader._fold("Bigflo et Oli") == downloader._fold("Bigflo & Oli") == "bigflo oli"
    assert downloader._fold("Balade à deux") == "balade a deux"


BIGFLO = {"title": "Alors alors", "artist": "Bigflo & Oli", "album": "La vraie vie", "duration": 200}
STROMAE = {"title": "Alors on danse", "artist": "Stromae", "album": "Cheese", "duration": 206}


def test_meta_must_match_what_was_typed(monkeypatch):
    calls = []

    def search_meta(q, limit=10):
        calls.append(q)
        return {"results": [STROMAE] if "et" in q.split() else [STROMAE, BIGFLO]}

    monkeypatch.setattr(downloader.covers, "search_meta", search_meta)
    parts = downloader.clean.parse("bigflo et oli - alors alors").parts
    assert downloader._lookup_meta(" ".join(parts), parts) == BIGFLO
    assert calls[0] == "bigflo et oli alors alors" and len(calls) == 2  # 2e essai sans « et »


def test_wrong_meta_is_never_used(monkeypatch):
    monkeypatch.setattr(downloader.covers, "search_meta", lambda q, limit=10: {"results": [STROMAE]})
    parts = ["bigflo et oli", "alors alors"]
    assert downloader._lookup_meta("bigflo et oli alors alors", parts) is None


def test_best_of_youtube_and_soundcloud(monkeypatch):
    results = {
        "ytsearch": [
            {"title": "Stromae - Alors On Danse (Official Video)", "uploader": "Stromae", "duration": 206, "url": "yt1"},
            {"title": "Bigflo & Oli - Alors alors (Live)", "uploader": "Fan", "duration": 230, "url": "yt2"},
        ],
        "scsearch": [
            {"title": "Alors alors", "uploader": "Bigflo & Oli", "duration": 30, "url": "sc-preview"},
            {"title": "Alors Alors", "uploader": "Bigflo & Oli", "duration": 201, "url": "sc-full"},
        ],
    }
    monkeypatch.setattr(downloader, "_search_candidates", lambda prefix, q, n: results[prefix])
    best, src = downloader._pick_best("Bigflo & Oli Alors alors", "Alors alors", "Bigflo & Oli", 200)
    assert best["url"] == "sc-full" and src == "scsearch"
    # YouTube officiel préféré quand il existe
    results["ytsearch"].append({"title": "Bigflo & Oli - Alors alors (Clip officiel)", "uploader": "Bigflo & Oli",
                                "duration": 200, "url": "yt-official"})
    best, src = downloader._pick_best("Bigflo & Oli Alors alors", "Alors alors", "Bigflo & Oli", 200)
    assert best["url"] in ("yt-official", "sc-full")
    assert best["url"] != "yt1"


def test_search_download_uses_parts_without_meta(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "_need_ytdlp", lambda: None)
    monkeypatch.setattr(downloader.covers, "search_meta", lambda q, limit=10: {"results": [STROMAE]})
    seen = {}

    def pick(probe, title, artist, dur):
        seen.update(probe=probe, title=title, artist=artist)
        return None

    monkeypatch.setattr(downloader, "_pick_best", pick)
    monkeypatch.setattr(downloader, "_download", lambda u, base: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        downloader.search_download("bigflo et oli - alors alors")
    assert seen == {"probe": "bigflo et oli alors alors", "title": "alors alors", "artist": "bigflo et oli"}
