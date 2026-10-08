"""Noms allégés pour la recherche : émojis, symboles, annotations de vidéo, séparateurs."""
from __future__ import annotations

import pytest

from onde import clean


@pytest.mark.parametrize("raw, parts", [
    ("MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K", ["MOONLIGHT", "Kali Uchis"]),
    ("Kali Uchis - Moonlight (Official Video) [HD]", ["Kali Uchis", "Moonlight"]),
    ("03 - Bohemian Rhapsody (Official Video) [HD]", ["Bohemian Rhapsody"]),
    ("Queen - Bohemian Rhapsody (Remastered 2011)", ["Queen", "Bohemian Rhapsody"]),
    ("Yellow Official Video 4K", ["Yellow"]),
    ("Yellow || Official Video || HD", ["Yellow"]),
    ("Video Games", ["Video Games"]),                       # « Video » fait partie du titre
    ("Lana Del Rey - Video", ["Lana Del Rey", "Video"]),
    ("Prince - 1999", ["Prince", "1999"]),
    ("Blink-182 - All the Small Things", ["Blink-182", "All the Small Things"]),
    ("Halsey - Colors", ["Halsey", "Colors"]),             # « Colors » est le vrai titre
    ("Colors || Halsey", ["Colors", "Halsey"]),
    ("ＹＯＡＳＯＢＩ｜夜に駆ける ✨🎵 [Lyrics] 【日本語字幕】", ["YOASOBI", "夜に駆ける"]),
    ("♪ Song Title ♪ — Some Artist // 8D audio 🎧 (use headphones)", ["Song Title", "Some Artist"]),
    ("Title - 2011 Remaster", ["Title"]),
    ("Song 1999 Remaster", ["Song"]),
    ("AC/DC - Back In Black", ["AC/DC", "Back In Black"]),
    ("Mr. Brightside", ["Mr. Brightside"]),
    ("می‌خواهم - Googoosh", ["می‌خواهم", "Googoosh"]),     # l'espace sans jonction persane est gardée
    ("", []),
    ("🌙🌙", []),
])
def test_parse(raw, parts):
    assert clean.parse(raw).parts == parts


def test_order_hint():
    assert clean.parse("Artiste - Titre").artist_first
    assert not clean.parse("Titre ~ Artiste").artist_first


def test_search_name():
    messy = "MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K"
    assert clean.search_name(messy, "Artiste inconnu") == "MOONLIGHT — Kali Uchis"
    assert clean.search_name(messy, "Kali Uchis") == "MOONLIGHT — Kali Uchis"
    assert clean.search_name("Yellow (Official Video)", "ColdplayVEVO") == "Yellow — Coldplay"
    assert clean.search_name("Yellow", "Artiste inconnu") == "Yellow"
    assert clean.query("Kali Uchis ~ Moonlight || 4K [AMV]") == "Kali Uchis Moonlight"


def test_artist_name():
    assert clean.artist_name("Coldplay - Topic") == "Coldplay"
    assert clean.artist_name("Artist feat. Someone 🎤") == "Artist"
    assert clean.artist_name("Unknown Artist") == ""


def test_downloader_titles_are_cleaned():
    from onde import downloader
    assert downloader._clean_title("Kali Uchis - Moonlight 🌙 (Official Video) [4K]") == "Kali Uchis - Moonlight"
    # « Titre ~ Artiste » : jamais pris pour « Artiste - Titre » (pas de tiret simple)
    out = downloader._clean_title("MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K")
    assert out == "MOONLIGHT — Kali Uchis" and " - " not in out


# ---------------------------------------------------------------- rangement automatique
@pytest.mark.parametrize("raw,expected", [
    ("MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K", "MOONLIGHT — Kali Uchis"),
    ("Mr. Brightside", "Mr. Brightside"),
    ("Hello (feat. Someone)", "Hello (feat. Someone)"),
    ("Video Games", "Video Games"),
    ("Yellow (Official Video) 4K", "Yellow"),
    ("", ""),
])
def test_title_short(raw, expected):
    assert clean.title(raw) == expected
    assert clean.title(clean.title(raw)) == clean.title(raw)  # idempotent


def test_query_short():
    assert clean.query("MOONLIGHT🌙~ Kali Uchis || [AMV] 4K") == "MOONLIGHT Kali Uchis"
