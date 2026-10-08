"""Paroles : recherche plus large sur LRCLIB, nouveaux essais automatiques tant qu'elles ne sont pas
trouvées, et recherche en arrière-plan sur toute la bibliothèque. Le réseau est simulé."""
from __future__ import annotations

import time

import httpx
import pytest

from onde import db, lyrics, settings

GET = "https://lrclib.net/api/get"
SEARCH = "https://lrclib.net/api/search"
LRC = "[00:01.00]Première ligne\n[00:05.00]Deuxième ligne"


@pytest.fixture
def lib(fresh_db, tmp_path, monkeypatch):
    """Petite bibliothèque : 3 titres sans paroles (fichiers vides, donc pas de tags)."""
    assert settings.get("online_lyrics")  # activé par défaut
    lyrics._stop.clear()  # (un autre test a pu arrêter la recherche en fermant l'appli)
    lyrics._priority.clear()
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste A')")
    tracks = [(1, "Titre A (Remastered 2011)", "Artiste A feat. Invité", 200.0, 5),
              (2, "Titre B", "Artiste B, Artiste C", 180.0, 0),
              (3, "Titre C", "Artiste D", 150.0, 9)]
    for tid, title, artist, dur, plays in tracks:
        f = tmp_path / f"{tid}.mp3"
        f.write_bytes(b"")
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration, plays) VALUES(?,?,?,?,1,?,?)",
             tid, str(f), title, artist, dur, plays)
    return tmp_path


def result(track, artist, duration, synced=LRC, plain=None):
    return {"trackName": track, "artistName": artist, "duration": duration,
            "syncedLyrics": synced, "plainLyrics": plain, "instrumental": False}


def test_exact_match(lib, http):
    http.routes[GET] = httpx.Response(200, json=result("Titre A", "Artiste A", 200))
    d = lyrics.get(1)
    assert d["found"] and d["synced"] == LRC and d["source"] == "LRCLIB"
    params = http.requests[0].url.params
    assert params["track_name"] == "Titre A" and params["artist_name"] == "Artiste A"  # sans « Remastered », « feat. »


def test_search_variants_first_artist_and_wider_duration(lib, http):
    """Pas de correspondance exacte : recherche avec le premier artiste seul, durée un peu différente."""
    def search(request):
        if request.url.params.get("artist_name") == "Artiste B":
            return httpx.Response(200, json=[result("Autre chanson", "Artiste B", 180),
                                             result("Titre B", "Artiste B", 189)])
        return httpx.Response(200, json=[])
    http.routes[SEARCH] = search
    d = lyrics.get(2)
    assert d["found"] and d["synced"] == LRC


def test_never_takes_lyrics_of_another_song(lib, http):
    http.routes[SEARCH] = httpx.Response(200, json=[result("Une autre", "Artiste D", 150)])
    d = lyrics.get(3)
    assert not d["found"] and d["tries"] == 1


def test_retries_with_growing_delays(lib, http):
    before = time.time()
    d = lyrics.get(3)  # rien nulle part (404 / listes vides)
    assert not d["found"] and d["tries"] == 1
    assert d["next_try"] == pytest.approx(before + lyrics.RETRY_DELAYS[0], abs=5)
    n = len(http.requests)
    assert not lyrics.get(3)["found"] and len(http.requests) == n  # pas encore l'heure : aucune requête
    db.x("UPDATE lyrics SET next_try=0 WHERE track_id=3")
    d = lyrics.get(3)
    assert d["tries"] == 2 and d["next_try"] == pytest.approx(time.time() + lyrics.RETRY_DELAYS[1], abs=5)
    assert lyrics.next_delay(50) == lyrics.RETRY_AFTER  # ensuite une fois par semaine, sans fin


def test_found_later(lib, http):
    lyrics.get(3)
    db.x("UPDATE lyrics SET next_try=0 WHERE track_id=3")
    http.routes[GET] = httpx.Response(200, json=result("Titre C", "Artiste D", 150, synced=None, plain="la la"))
    d = lyrics.get(3)
    assert d["found"] and d["plain"] == "la la"
    # paroles brutes : une version synchronisée sera recherchée dans un mois
    assert db.q1("SELECT next_try FROM lyrics WHERE track_id=3")["next_try"] == pytest.approx(time.time() + lyrics.UPGRADE_EVERY, abs=5)


def test_offline_is_not_a_failure(lib, http):
    def down(request):
        raise httpx.ConnectError("Network is unreachable", request=request)
    http.routes[GET] = down
    d = lyrics.get(3)
    assert d["offline"] and not d["found"]
    assert db.q1("SELECT 1 FROM lyrics WHERE track_id=3") is None  # rien d'enregistré : on réessaiera


def test_lrc_file_added_later_is_used(lib, http):
    lyrics.get(3)
    (lib / "3.lrc").write_text(LRC, encoding="utf-8")
    d = lyrics.get(3)  # même avant l'heure du prochain essai en ligne
    assert d["found"] and d["source"] == "fichier .lrc"


def test_cleared_by_hand_is_not_searched_again(lib, http):
    lyrics.save(3, "")
    assert 3 not in lyrics.due(now=time.time() + 10 * 365 * 86400)
    assert lyrics.get(3)["next_try"] is None


def test_background_round_most_played_first(lib, http, monkeypatch):
    http.routes[GET] = lambda request: httpx.Response(
        200, json=result(request.url.params["track_name"], request.url.params["artist_name"], 0))
    assert lyrics.due() == [3, 1, 2]  # jamais cherchés, les plus écoutés d'abord
    assert lyrics.run_once(pause=0) == "done"
    assert lyrics.due() == []
    assert lyrics.run_once(pause=0) == "idle"


def test_background_round_stops_when_offline(lib, http):
    def down(request):
        raise httpx.ConnectError("Network is unreachable", request=request)
    http.routes[GET] = down
    assert lyrics.run_once(pause=0) == "offline"
    # un seul titre essayé (le client réessaie une fois une erreur passagère), pas toute la tournée
    assert {r.url.params["track_name"] for r in http.requests} == {"Titre C"}


# ------------------------------------------------------------------ recherche améliorée
def add_track(tid, title, artist, duration, path, album="Album", album_artist="Artiste A"):
    db.x("INSERT OR IGNORE INTO albums(id, key, name, artist) VALUES(?, ?, ?, ?)", 10 + tid, f"k{tid}", album, album_artist)
    path.write_bytes(b"")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration) VALUES(?,?,?,?,?,?)",
         tid, str(path), title, artist, 10 + tid, duration)


def test_key_like_lrclib():
    assert lyrics.key("Don’t Stop Me Now!") == lyrics.key("dont stop me now")
    assert lyrics.key("Café  Déjà-vu") == "cafe deja vu"


def test_titles_variants():
    t = lyrics._titles("03 - Bohemian Rhapsody (Official Video) [HD]")
    assert "Bohemian Rhapsody" in t
    assert lyrics._titles("Queen - Bohemian Rhapsody", {"queen"})[0] == "Bohemian Rhapsody"
    assert lyrics._artists("ColdplayVEVO 🌙 feat. X") == ["Coldplay"]
    assert lyrics._artists("Artiste inconnu", "Various Artists") == []


def test_plain_exact_then_synced_found_by_search(lib, http):
    """La correspondance exacte n'a que des paroles brutes : la recherche trouve la version synchronisée."""
    http.routes[GET] = httpx.Response(200, json=result("Titre C", "Artiste D", 150, synced=None, plain="la la"))
    http.routes[SEARCH] = httpx.Response(200, json=[result("Titre C", "Artiste D", 151)])
    d = lyrics.get(3)
    assert d["synced"] == LRC


def test_long_track_duration_not_sent_and_400_is_not_offline(lib, http, tmp_path):
    add_track(9, "Mix géant", "DJ Test", 4000, tmp_path / "mix.mp3")

    def get(request):
        assert "duration" not in request.url.params  # LRCLIB refuse plus d'une heure (400)
        return httpx.Response(400, json={"message": "bad"})
    http.routes[GET] = get
    d = lyrics.get(9)
    assert not d.get("offline") and d["tries"] == 1  # compté comme « rien trouvé », la file continue


def test_server_error_is_temporary(lib, http):
    http.routes[GET] = httpx.Response(503)
    d = lyrics.get(3)
    assert d.get("offline") and db.q1("SELECT 1 FROM lyrics WHERE track_id=3") is None


def test_wrong_version_keeps_only_text(lib, http):
    """Durée différente de 10 s et paroles qui vont jusqu'à la fin : le texte sert, pas les dates."""
    long_lrc = "[00:10.00]Une\n[02:29.50]Deux"
    http.routes[SEARCH] = httpx.Response(200, json=[result("Titre C", "Artiste D", 160, synced=long_lrc)])
    d = lyrics.get(3)
    assert d["found"] and not d["synced"] and d["plain"] == "Une\nDeux"


def test_synced_beyond_end_rejected(lib, http):
    http.routes[GET] = httpx.Response(200, json=result("Titre C", "Artiste D", 150,
                                                       synced="[00:10.00]A\n[09:00.00]B", plain="A\nB"))
    d = lyrics.get(3)
    assert not d["synced"] and d["plain"] == "A\nB"


def test_vevo_artist_found_by_title_only(lib, http, tmp_path):
    add_track(5, "Yellow (Official Video)", "ColdplayVEVO", 266, tmp_path / "y.mp3", album_artist="ColdplayVEVO")

    def search(request):
        if request.url.params.get("track_name") == "Yellow" and "artist_name" not in request.url.params:
            return httpx.Response(200, json=[result("Yellow", "Someone Else", 266), result("Yellow", "Coldplay", 267)])
        return httpx.Response(200, json=[])
    http.routes[SEARCH] = search
    d = lyrics.get(5)
    assert d["synced"] == LRC


def test_unknown_artist_unique_match_only(lib, http, tmp_path):
    add_track(6, "Chanson rare", "Artiste inconnu", 200, tmp_path / "c.mp3", album_artist="Artiste inconnu")
    add_track(7, "Hello", "Artiste inconnu", 200, tmp_path / "h.mp3", album_artist="Artiste inconnu")

    def search(request):
        name = request.url.params.get("track_name")
        if name == "Chanson rare":
            return httpx.Response(200, json=[result("Chanson rare", "X", 200.5)])
        if name == "Hello":
            return httpx.Response(200, json=[result("Hello", "A", 200), result("Hello", "B", 201)])
        return httpx.Response(200, json=[])
    http.routes[SEARCH] = search
    assert lyrics.get(6)["synced"] == LRC
    assert not lyrics.get(7)["found"]  # deux artistes possibles : rien plutôt qu'une erreur


def test_artist_dash_title_in_title_field(lib, http, tmp_path):
    add_track(8, "Queen - Bohemian Rhapsody", "Artiste inconnu", 355, tmp_path / "q.mp3", album_artist="")

    def search(request):
        p = request.url.params
        if p.get("track_name") == "Bohemian Rhapsody" and p.get("artist_name") == "Queen":
            return httpx.Response(200, json=[result("Bohemian Rhapsody", "Queen", 355)])
        return httpx.Response(200, json=[])
    http.routes[SEARCH] = search
    assert lyrics.get(8)["synced"] == LRC


def test_request_budget(lib, http):
    lyrics.get(3)
    assert len(http.requests) <= lyrics.REQUEST_BUDGET


def test_tags_plain_upgraded_to_synced_in_background(lib, http):
    (lib / "3.txt").write_text("Ligne un\nLigne deux", encoding="utf-8")
    d = lyrics.get(3)
    assert d["plain"] == "Ligne un\nLigne deux" and d["source"] == "fichier .txt" and not http.requests
    assert 3 in lyrics.due()  # version synchronisée cherchée tout de suite en arrière-plan
    http.routes[GET] = httpx.Response(200, json=result("Titre C", "Artiste D", 150))
    lyrics.run_once(pause=0)
    d = lyrics.get(3)
    assert d["synced"] == LRC and d["source"] == "LRCLIB"


def test_upgrade_not_found_waits_a_month(lib, http):
    (lib / "3.txt").write_text("Ligne un\nLigne deux", encoding="utf-8")
    lyrics.get(3)
    lyrics.run_once(pause=0)
    row = db.q1("SELECT next_try, plain FROM lyrics WHERE track_id=3")
    assert row["plain"] and row["next_try"] == pytest.approx(time.time() + lyrics.UPGRADE_EVERY, abs=5)
    assert 3 not in lyrics.due()


def test_manual_plain_never_replaced(lib, http):
    lyrics.save(3, "Mes paroles\nà moi")
    assert 3 not in lyrics.due(now=time.time() + 400 * 86400)


def test_priority_first(lib, http, monkeypatch):
    seen = []

    def get(request):
        seen.append(request.url.params["track_name"])
        return httpx.Response(200, json=result(request.url.params["track_name"], request.url.params["artist_name"], 0))
    http.routes[GET] = get
    lyrics.prioritize(2)
    lyrics.run_once(pause=0)
    assert seen[0] == "Titre B"


def test_status_and_retry_all(lib, http):
    lyrics.get(3)
    st = lyrics.status()
    assert st["counts"]["missing"] == 1 and st["counts"]["never"] == 2 and st["recent"][0]["result"] == "missing"
    assert lyrics.retry_all() == 1 and 3 in lyrics.due()


def test_lrc_metadata_and_sylt(tmp_path):
    from mutagen.id3 import ID3, SYLT, USLT
    p = tmp_path / "s.mp3"
    p.write_bytes(b"")
    (tmp_path / "s.lrc").write_text("[ar:Moi]\n[ti:Titre]\nPremière\nDeuxième", encoding="utf-8")
    assert lyrics.from_file(str(p)) == {"synced": None, "plain": "Première\nDeuxième", "source": "fichier .lrc"}
    (tmp_path / "s.lrc").unlink()
    tags = ID3()
    tags.add(SYLT(encoding=3, lang="fra", format=2, type=1, text=[("Un", 1000), ("Deux", 5000)]))
    tags.add(USLT(encoding=3, lang="fra", desc="", text="Un\nDeux"))
    tags.save(str(p))
    d = lyrics.from_file(str(p))
    assert d["synced"].startswith("[00:01.00]Un") and d["plain"] == "Un\nDeux" and d["source"] == "tags"


def test_legacy_plain_lyrics_get_upgraded(lib, http):
    """Paroles brutes enregistrées avant cette version (next_try vide) : une version synchronisée est cherchée."""
    db.x("INSERT INTO lyrics(track_id, plain, source, fetched, tries) VALUES(3, 'la la', 'LRCLIB', 1, 0)")
    db.x("INSERT INTO lyrics(track_id, plain, source, fetched, tries) VALUES(2, 'à moi', 'manuel', 1, 0)")
    assert lyrics.due() == [1] or 3 not in lyrics.due()
    assert lyrics.migrate() == 1  # seule la ligne LRCLIB : les paroles tapées à la main restent intactes
    assert 3 in lyrics.due() and 2 not in lyrics.due()
    http.routes[GET] = httpx.Response(200, json=result("Titre C", "Artiste D", 150))
    lyrics.run_once(pause=0)
    assert lyrics.get(3)["synced"] == LRC
    assert lyrics.migrate() == 0


# ------------------------------------------------------------------ noms allégés (émojis, [AMV], 4K…)
MESSY = "MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K"


def test_search_name_example():
    from onde import clean
    assert clean.search_name(MESSY, "Artiste inconnu") == "MOONLIGHT — Kali Uchis"
    assert clean.search_name(MESSY, "Kali Uchis") == "MOONLIGHT — Kali Uchis"
    assert clean.parse(MESSY).parts == ["MOONLIGHT", "Kali Uchis"]


def test_messy_title_unknown_artist_found_in_either_order(lib, http, tmp_path):
    add_track(20, MESSY, "Artiste inconnu", 190, tmp_path / "m.mp3", album_artist="Artiste inconnu")
    seen = []

    def get(request):
        p = request.url.params
        seen.append((p.get("track_name"), p.get("artist_name")))
        if p.get("track_name") == "MOONLIGHT" and p.get("artist_name") == "Kali Uchis":
            return httpx.Response(200, json=result("MOONLIGHT", "Kali Uchis", 190))
        return httpx.Response(404)
    http.routes[GET] = get
    d = lyrics.get(20)
    assert d["synced"] == LRC and d["source"] == "LRCLIB"
    assert seen[0] == ("MOONLIGHT", "Kali Uchis")  # envoyé propre, sans émoji ni [AMV] ni 4K
    row = lyrics.status()["recent"][0]
    assert row["query"] == "MOONLIGHT — Kali Uchis" and row["result"] == "synced"


def test_messy_title_artist_first_order_is_found(lib, http, tmp_path):
    add_track(21, "Kali Uchis - MOONLIGHT🌙 (Official Video) [4K]", "Artiste inconnu", 190, tmp_path / "n.mp3",
              album_artist="Artiste inconnu")
    http.routes[SEARCH] = lambda r: httpx.Response(200, json=[result("MOONLIGHT", "Kali Uchis", 190)])
    assert lyrics.get(21)["synced"] == LRC


def test_messy_title_with_known_artist(lib, http, tmp_path):
    add_track(22, MESSY, "Kali Uchis", 190, tmp_path / "o.mp3", album_artist="Kali Uchis")
    seen = []

    def get(request):
        seen.append(request.url.params.get("track_name"))
        return httpx.Response(200, json=result("MOONLIGHT", "Kali Uchis", 190))
    http.routes[GET] = get
    assert lyrics.get(22)["synced"] == LRC and seen == ["MOONLIGHT"]


def test_uploader_as_artist_found_by_title_only(lib, http, tmp_path):
    """L'artiste des tags est une chaîne YouTube ; le vrai artiste est dans le titre."""
    add_track(23, MESSY, "Chaîne Lyrics HD", 190, tmp_path / "p.mp3", album_artist="Chaîne Lyrics HD")

    def search(request):
        p = request.url.params
        if p.get("track_name") == "MOONLIGHT" and "artist_name" not in p:
            return httpx.Response(200, json=[result("MOONLIGHT", "Autre", 190), result("MOONLIGHT", "Kali Uchis", 190)])
        return httpx.Response(200, json=[])
    http.routes[SEARCH] = search
    assert lyrics.get(23)["synced"] == LRC
    assert len(http.requests) <= lyrics.REQUEST_BUDGET
