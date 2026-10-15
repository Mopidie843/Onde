"""Conversion à la volée : conversion trop longue (fichier partiel retiré) et cache vidé entre-temps."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from onde import config, transcode


@pytest.fixture
def tc(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(config, "TRANSCODE_DIR", tmp_path / "tc")
    runs: list[list] = []

    def run(cmd, **kw):  # faux ffmpeg : écrit le fichier de sortie (dernier argument)
        runs.append(cmd)
        Path(cmd[-1]).write_bytes(b"fLaC" + b"\0" * 100)

    fake = SimpleNamespace(run=run, CalledProcessError=subprocess.CalledProcessError,
                           TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(transcode, "subprocess", fake)
    return SimpleNamespace(dir=tmp_path / "tc", runs=runs, fake=fake)


def test_timeout_removes_partial_file(tc):
    def slow(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"fLaC...")  # ffmpeg avait commencé à écrire
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    tc.fake.run = slow
    with pytest.raises(RuntimeError, match="Conversion trop longue"):
        transcode.cached(1, "/musique/a.wma", 0)
    assert list(tc.dir.iterdir()) == []  # pas de .part.flac oublié


def test_cache_file_pruned_meanwhile_is_rebuilt(tc, monkeypatch):
    out = transcode.cached(1, "/musique/a.wma", 0)
    assert out.exists() and len(tc.runs) == 1
    real_utime = os.utime

    def pruned(path, *a, **k):  # le ménage du cache passe entre exists() et utime()
        Path(path).unlink()
        raise FileNotFoundError(path)

    monkeypatch.setattr(transcode.os, "utime", pruned)
    again = transcode.cached(1, "/musique/a.wma", 0)
    monkeypatch.setattr(transcode.os, "utime", real_utime)
    assert again == out and out.exists() and len(tc.runs) == 2
