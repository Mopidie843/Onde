"""Paroles calées sur la voix (IA) : préparation du texte, format LRC, contrôle du résultat, processus
de calage (succès, fichier abîmé, mémoire, plantage, délai, annulation), et calage automatique de la
bibliothèque. Le vrai modèle d'IA n'est pas nécessaire : le travailleur est simulé."""
from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from onde import align, db, lyrics, settings

PLAIN = "Première ligne\nDeuxième ligne\n\n[Refrain]\nTroisième ligne"
LINES = ["Première ligne", "Deuxième ligne", "Troisième ligne"]


def seg(start, end, text, prob=0.8, words=2, zero=0):
    return {"start": start, "end": end, "text": text, "prob": prob, "words": words, "zero": zero}


# ------------------------------------------------------------------ texte et format
def test_prepare_lines_keeps_only_sung_lines():
    text = "[ar:Artiste]\n[00:01.00]Déjà horodatée\n\n  Couplet 2 :\n(Pont)\nUne   ligne\nrefrain x2\nChorus\nLe refrain revient"
    assert align.prepare_lines(text) == ["Déjà horodatée", "Une ligne", "Le refrain revient"]


@pytest.mark.parametrize("sec, out", [(0, "00:00.00"), (12.345, "00:12.35"), (59.999, "01:00.00"),
                                      (61.5, "01:01.50"), (3725.2, "62:05.20"), (-3, "00:00.00")])
def test_fmt_ts(sec, out):
    assert align.fmt_ts(sec) == out


def test_build_lrc_format_order_and_gaps():
    segs = [seg(5.0, 8.0, "Une"), seg(4.0, 9.0, "Deux (mal placée avant)"), seg(30.0, 33.0, "Trois")]
    lrc = align.build_lrc(segs, duration=60, title="Titre [live]", artist="Artiste")
    lines = lrc.strip().splitlines()
    assert lines[:2] == ["[ti:Titre live]", "[ar:Artiste]"] and align.LRC_TAG in lines
    body = [line for line in lines if line[1:3].isdigit()]
    assert body[0] == "[00:05.00] Une"
    assert body[1] == "[00:05.01] Deux (mal placée avant)"  # heures toujours croissantes
    assert body[2] == "[00:09.50]"                          # 21 s sans chant : ligne vide
    assert body[3] == "[00:30.00] Trois"
    assert body[4] == "[00:33.50]"                          # fin instrumentale
    assert lyrics.is_synced(lrc)


def test_guess_language():
    fr = ["je ne sais pas pourquoi tu es là", "mais moi je reste avec toi dans la nuit", "et tout le monde danse"]
    en = ["i don't know why you are here", "but i stay with you in the night", "and all my love is yours"]
    assert align.guess_language(fr) == "fr"
    assert align.guess_language(en) == "en"
    assert align.guess_language(["la la la", "oh oh"]) is None


def test_match_segments_same_count_uses_original_text():
    out = align.match_segments(LINES, [seg(1, 2, "première ligne"), seg(3, 4, "x"), seg(5, 6, "y")])
    assert [s["text"] for s in out] == LINES and out[1]["start"] == 3


def test_match_segments_regroups_split_lines():
    segs = [seg(1, 2, "Première"), seg(2, 3, "ligne"), seg(4, 5, "Deuxième ligne"), seg(6, 7, "Troisième ligne")]
    out = align.match_segments(LINES, segs)
    assert [(s["start"], s["end"]) for s in out] == [(1, 3), (4, 5), (6, 7)]


def test_match_segments_refuses_other_text():
    with pytest.raises(align.AlignError) as e:
        align.match_segments(LINES, [seg(1, 2, "autre chose"), seg(3, 4, "encore")])
    assert e.value.kind == "mismatch"


def test_assess_rejects_doubtful_alignment():
    align.assess([seg(1, 2, "a", prob=0.6), seg(2, 3, "b", prob=0.5)])
    for bad in ([seg(1, 2, "a", prob=0.01)], [seg(1, 2, "a", words=4, zero=3)], [seg(1, 2, "a", words=0)]):
        with pytest.raises(align.AlignError) as e:
            align.assess(bad)
        assert e.value.kind == "mismatch"


# ------------------------------------------------------------------ processus de calage
@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Faux travailleur : `worker("code python")` ; le code lit la tâche dans `job`."""
    def make(body: str):
        script = tmp_path / "fake_worker.py"
        script.write_text(textwrap.dedent("""
            import json, os, sys, time
            job = json.loads(sys.stdin.read())
            def emit(o):
                print(json.dumps(o), flush=True)
        """) + textwrap.dedent(body), encoding="utf-8")
        monkeypatch.setattr(align, "WORKER_CMD", [sys.executable, str(script)])
    monkeypatch.setattr(align, "mem_available", lambda: 8.0)
    return make


def test_run_job_success_and_progress(worker):
    worker("""
        emit({"phase": "align"}); emit({"progress": 0.5})
        emit({"ok": True, "language": job["language"],
              "segments": [{"start": i * 2.0, "end": i * 2.0 + 1, "text": l, "prob": 0.9, "words": 2, "zero": 0}
                           for i, l in enumerate(job["lines"])]})
    """)
    seen = []
    segments, lang = align.align_text("song.mp3", PLAIN, language="fr", on_message=seen.append)
    assert [s["text"] for s in segments] == LINES and lang == "fr"
    assert {"progress": 0.5} in seen


@pytest.mark.parametrize("body, kind", [
    ('emit({"ok": False, "error": "audio", "message": "Invalid data found when processing input"})', "audio"),
    ('emit({"ok": False, "error": "memory", "message": "x"})', "memory"),
    ("os.kill(os.getpid(), 9)", "memory"),              # tué par le système (manque de mémoire)
    ('raise SystemExit("boom")', "engine"),             # plantage sans résultat
    ("time.sleep(30)", "cancelled"),
])
def test_run_job_errors(worker, body, kind):
    worker(body)
    cancel = threading.Event()
    if kind == "cancelled":
        threading.Timer(0.5, cancel.set).start()
    with pytest.raises(align.AlignError) as e:
        align.align_text("song.mp3", PLAIN, language="fr", cancel=cancel)
    assert e.value.kind == kind
    if kind == "audio":
        assert "illisible" in str(e.value) and "Invalid data" in str(e.value)


def test_run_job_timeout(worker):
    worker("time.sleep(30)")
    t = time.monotonic()
    with pytest.raises(align.AlignError) as e:
        align.run_job({"lines": LINES}, timeout=0.5)
    assert e.value.kind == "timeout" and time.monotonic() - t < 10


def test_not_enough_memory_is_checked_before_starting(worker, monkeypatch):
    worker('emit({"ok": True, "segments": []})')
    monkeypatch.setattr(align, "mem_available", lambda: 0.7)
    with pytest.raises(align.AlignError) as e:
        align.align_text("song.mp3", PLAIN, model="small")
    assert e.value.kind == "memory" and "« tiny »" in str(e.value)


def test_too_few_lines():
    with pytest.raises(align.AlignError) as e:
        align.align_text("song.mp3", "Une seule ligne\n\n")
    assert e.value.kind == "skip"


# ------------------------------------------------------------------ bibliothèque
@pytest.fixture
def lib(fresh_db, tmp_path, monkeypatch, http):
    """Un titre avec des paroles brutes trouvées sur LRCLIB ; calage simulé (réussi par défaut)."""
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(align, "unavailable_reason", lambda: None)
    monkeypatch.setattr(align, "mem_available", lambda: 8.0)
    monkeypatch.setattr(align, "_priority", __import__("collections").deque())
    align._stop.clear()
    align._cancel.clear()
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    song = tmp_path / "chanson.mp3"
    song.write_bytes(b"")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration) VALUES(1, ?, 'Titre', 'Artiste', 1, 60)",
         str(song))
    http.routes["https://lrclib.net/api/get"] = httpx.Response(200, json={
        "trackName": "Titre", "artistName": "Artiste", "duration": 60, "syncedLyrics": None,
        "plainLyrics": PLAIN, "instrumental": False})
    state = SimpleNamespace(result=None, calls=[], song=song)

    def fake_align(audio, text, **kw):
        state.calls.append(text)
        if isinstance(state.result, Exception):
            raise state.result
        return [seg(10.0 + 5 * i, 12.0 + 5 * i, line) for i, line in enumerate(align.prepare_lines(text))], "fr"

    monkeypatch.setattr(align, "align_text", fake_align)
    monkeypatch.setattr(lyrics, "listeners", [align.on_lyrics])
    yield state
    settings._cache = None


def test_plain_lyrics_found_are_aligned_and_saved(lib):
    d = lyrics.get(1)
    assert d["plain"] and not d["synced"]
    assert align.due() == [1]
    assert align.decorate(1, d)["align"]["queued"]
    assert align.run_once() == "done"
    d = lyrics.get(1)
    assert d["synced"].count("] ") == 3 and "[00:10.00] Première ligne" in d["synced"]
    assert d["source"] == "LRCLIB" + align.SOURCE_SUFFIX
    lrc = lib.song.with_suffix(".lrc").read_text(encoding="utf-8")
    assert lrc == d["synced"] and align.LRC_TAG in lrc
    assert align.due() == [] and align.run_once() == "idle"
    assert align.status()["done"] == 1
    from onde import notify  # une notification « paroles synchronisées » est prévue pour l'appli
    ev = [e for e in notify.since(0) if e["kind"] == "lyrics"]
    assert ev and ev[-1]["text"] == "Artiste — Titre" and ev[-1]["link"] == "#/album/1"
    # le .lrc écrit est reconnu comme le nôtre
    assert lyrics.from_file(str(lib.song))["source"] == "fichier .lrc (calé par IA)"


def test_new_search_keeps_alignment(lib):
    lyrics.get(1)
    align.run_once()
    lib.song.with_suffix(".lrc").unlink()
    d = lyrics.get(1, force=True)  # LRCLIB renvoie les mêmes paroles brutes
    assert d["synced"] and d["source"].endswith(align.SOURCE_SUFFIX)
    assert align.due() == []


def test_existing_synced_lrc_is_never_replaced(lib):
    other = "[00:01.00]Synchro faite à la main\n"
    assert align.write_lrc(str(lib.song), "x") is not None  # dossier modifiable
    lib.song.with_suffix(".lrc").write_text(other, encoding="utf-8")
    assert align.write_lrc(str(lib.song), "[00:02.00] nouveau\n") is None
    assert lib.song.with_suffix(".lrc").read_text(encoding="utf-8") == other


def test_plain_lrc_file_is_kept_as_txt(lib):
    lib.song.with_suffix(".lrc").write_text(PLAIN, encoding="utf-8")
    d = lyrics.get(1)
    assert d["source"] == "fichier .lrc" and d["plain"]
    align.run_once()
    assert lib.song.with_suffix(".txt").read_text(encoding="utf-8") == PLAIN
    assert lyrics.is_synced(lib.song.with_suffix(".lrc").read_text(encoding="utf-8"))


def test_no_lrc_file_when_disabled(lib):
    settings.update({"align_write_lrc": False})
    lyrics.get(1)
    align.run_once()
    assert lyrics.get(1)["synced"] and not lib.song.with_suffix(".lrc").exists()


def test_wrong_lyrics_are_not_retried(lib):
    lib.result = align.AlignError("mismatch", "confiance 3 %")
    lyrics.get(1)
    align.run_once()
    st = align.track_status(1)
    assert st["state"] == "mismatch" and "ne correspondent pas" in st["error"] and not st["queued"]
    assert align.due(now=time.time() + 365 * 86400) == []
    # corrigées à la main : nouvel essai
    lib.result = None
    lyrics.save(1, PLAIN + "\nQuatrième ligne")
    assert align.due() == [1]


def test_corrupt_audio_is_not_retried(lib):
    lib.result = align.AlignError("audio", "Invalid data found when processing input")
    lyrics.get(1)
    res = align.align_track(1)
    assert not res["ok"] and "illisible" in res["message"]
    assert align.due(now=time.time() + 365 * 86400) == []


def test_memory_errors_retry_then_give_up(lib):
    lib.result = align.AlignError("memory", "3 Go libres")
    lyrics.get(1)
    for n in range(1, align.MAX_TRIES + 1):
        assert align.due(now=time.time() + n * align.RETRY_DELAY + 10) == [1]
        db.x("UPDATE lyrics SET align_next=0 WHERE track_id=1")
        align.run_once()
    row = db.q1("SELECT * FROM lyrics WHERE track_id=1")
    assert row["align_state"] == "gaveup" and row["align_tries"] == align.MAX_TRIES
    assert align.due(now=time.time() + 365 * 86400) == []


def test_model_download_offline_waits_without_counting(lib):
    lib.result = align.AlignError("model", "urlopen error")
    lyrics.get(1)
    align.run_once()
    row = db.q1("SELECT * FROM lyrics WHERE track_id=1")
    assert row["align_state"] == "retry" and row["align_tries"] == 0
    assert row["align_next"] == pytest.approx(time.time() + align.OFFLINE_DELAY, abs=5)


def test_manual_request_goes_first_even_after_failure(lib, monkeypatch):
    monkeypatch.setattr(align, "start_background", lambda delay=0: None)
    lib.result = align.AlignError("mismatch")
    lyrics.get(1)
    align.run_once()
    settings.update({"auto_align": False})
    assert align.run_once() == "idle"
    lib.result = None
    st = align.request(1)
    assert st["queued"]
    assert align.run_once() == "done" and lyrics.get(1)["synced"]


def test_auto_align_disabled(lib):
    settings.update({"auto_align": False})
    lyrics.get(1)
    assert align.run_once() == "idle" and lib.calls == []


def test_edited_meanwhile_is_not_overwritten(lib, monkeypatch):
    lyrics.get(1)
    real = align.align_text

    def slow(*a, **kw):
        out = real(*a, **kw)
        lyrics.save(1, "Tout autre chose\nVraiment autre")  # modifiées pendant le calage
        return out

    monkeypatch.setattr(align, "align_text", slow)
    res = align.align_track(1)
    assert not res["ok"]
    assert lyrics.get(1)["plain"].startswith("Tout autre chose")


def test_api(lib, monkeypatch):
    from fastapi.testclient import TestClient

    from onde import main

    monkeypatch.setattr(align, "start_background", lambda delay=0: None)
    c = TestClient(main.app)  # (sans « with » : pas de tâches de fond)
    r = c.post("/api/tracks/1/lyrics/align", json={"text": "[00:01.00]déjà synchro"})
    assert r.status_code == 400 and "déjà synchronisées" in r.json()["detail"]
    r = c.post("/api/tracks/1/lyrics/align", json={"text": "Ligne A\nLigne B"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["plain"] == "Ligne A\nLigne B" and d["align"]["queued"] and d["source"] == "manuel"
    assert c.get("/api/align/status").json()["available"]
    align.run_once()
    d = c.get("/api/tracks/1/lyrics").json()
    assert "[00:15.00] Ligne B" in d["synced"] and "align" not in d
    monkeypatch.setattr(align, "unavailable_reason", lambda: "module d'IA non installé")
    r = c.post("/api/tracks/1/lyrics/align", json={})
    assert r.status_code == 400 and "Réglages" in r.json()["detail"]


def test_worker_reports_bad_audio_without_ai_modules(tmp_path):
    """Le vrai travailleur : sans le module d'IA, il le dit clairement (pas de trace Python)."""
    import subprocess

    job = {"audio": str(tmp_path / "absent.mp3"), "lines": LINES, "language": "fr", "model": "tiny"}
    r = subprocess.run([sys.executable, "-m", "onde.align_worker"], input=json.dumps(job),
                       capture_output=True, text=True, timeout=120, cwd=str(align.config.ROOT))
    msg = json.loads(r.stdout.strip().splitlines()[-1])
    assert msg["ok"] is False and msg["error"] in ("missing", "audio")


def test_manual_request_does_not_wait_for_startup_delay(lib, monkeypatch):
    monkeypatch.setattr(align, "AUTO_START_DELAY", 3600.0)
    monkeypatch.setattr(align, "AUTO_IDLE", 3600.0)
    lyrics.save(1, PLAIN)
    settings.update({"auto_align": False})
    align.start_background()
    try:
        align.request(1)
        for _ in range(50):
            if lyrics.get(1)["synced"]:
                break
            time.sleep(0.1)
        assert lyrics.get(1)["synced"]
    finally:
        align.stop()
        align._thread.join(5)
