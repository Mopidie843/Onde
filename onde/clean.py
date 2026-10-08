"""Noms allégés pour la recherche (paroles sur LRCLIB, musique en ligne).

Les titres venus d'internet traînent des décorations qui gênent les recherches : émojis, symboles,
séparateurs, « [AMV] », « Sub. Español », « 4K », « (Official Video) »…  Ici on ne garde que
l'essentiel :

    "MOONLIGHT🌙~ Kali Uchis || [AMV] COLORs ;; Sub. Español 4K"   →   "MOONLIGHT — Kali Uchis"

Rien n'est modifié dans tes fichiers ni dans la bibliothèque : ce nom ne sert qu'à chercher.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------- vocabulaire
# Mots qui ne désignent jamais la chanson : annotations de vidéo, de qualité, de sous-titres…
CERTAIN = frozenset("""
official officiel oficial oficiale ufficiale offiziell hd hq uhd fhd 4k 8k 2k 1080p 720p 480p 360p 60fps 30fps
amv mv pv lyric lyrics lyrical letra letras paroles parole testo songtext visualizer visualiser
sub subs subtitle subtitles subtitled subtitulado subtitulada subtitulos subtitulo vostfr vost
traduccion traducida traducido traduction traduzione traduzido translated translation
remaster remastered remasterizado remasterise headphones headphone earphones
""".split())
# Écritures sans espaces (japonais, chinois, coréen) : mots d'annotation repérés dans le texte
_CJK_JUNK = re.compile("字幕|中字|歌詞|歌词|和訳|日本語|한글|가사|자막|번역|中文|翻译|翻譯|音声|MV")
# Mots qui peuvent faire partie d'un vrai titre : retirés seulement quand le reste est clair
MAYBE = frozenset("""
video videoclip videoclipe vid clip audio color colors colour colours coded vibes vibe aesthetic edit edits
slowed reverb sped up nightcore 8d bass boosted tiktok shorts viral
espanol spanish english ingles francais french portugues portuguese japanese romaji kanji hangul korean chinese
deutsch german italiano italian eng esp fra ita por spa jpn kor rom
sous titres
""".split())
# Fin de texte : « Yellow Official Video 4K » → « Yellow » (la suite doit contenir un mot de CERTAIN)
TRAILING = CERTAIN | {"video", "videoclip", "videoclipe", "audio", "clip", "vid"}

UNKNOWN = frozenset({"artiste inconnu", "unknown artist", "unknown", "inconnu", "various artists",
                     "artistes varies", "va", "divers", "<unknown>"})
_feat = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|avec|with)\s+.*$", re.I)
_vevo = re.compile(r"(?:\s*-\s*topic|vevo|\s+official|\s+officiel)$", re.I)

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|【[^】]*】|「[^」]*」|『[^』]*』|《[^》]*》|〈[^〉]*〉|<[^>]*>|（[^）]*）")
_SEP = re.compile(
    r"\s*(?:\|{1,2}|¦|‖|;{1,2}|~+|〜|/{2,}|\\+|•|·)\s*"   # symboles séparateurs
    r"|\s+[-–—―]+\s+"                                       # tiret entouré d'espaces
    r"|\s+/\s+"                                              # barre entourée d'espaces
    r"|[-–—]{2,}")                                           # « -- »
_DASH = re.compile(r"\s+[-–—―]+\s+")
_TRACKNO = re.compile(r"^\s*\d{1,3}\s*[-.)]\s+")
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.U)
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_EDGES = " \t\r\n-–—―~|/\\,;:·•"
_KEEP_FORMAT = {"‌"}  # (espace sans jonction : fait partie de certains mots persans)


def fold(text: str | None) -> str:
    """Minuscules, sans accents, espaces normalisés."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return " ".join(s.split())


def _tokens(text: str) -> list[str]:
    return [fold(m.group()).replace("'", "") for m in _WORD.finditer(text)]


def _decorative(ch: str) -> bool:
    if ch in _KEEP_FORMAT:
        return False
    cat = unicodedata.category(ch)
    if cat in ("So", "Sk", "Cf", "Cs", "Co", "Cn", "Cc"):
        return ch not in "\t\r\n"
    cp = ord(ch)
    return cp in (0x20E3, 0xFE0E, 0xFE0F) or 0xE0100 <= cp <= 0xE01EF


def strip_symbols(text: str | None) -> str:
    """Retire émojis, symboles et caractères invisibles ; les lettres de toutes les écritures restent."""
    s = unicodedata.normalize("NFKC", text or "")
    return "".join(" " if ch == "_" else ch for ch in s if not _decorative(ch))


def _junky(token: str) -> bool:
    return token in CERTAIN or token in MAYBE or bool(_YEAR.match(token))


def _bracket(match: re.Match) -> str:
    inner = match.group()[1:-1]
    toks = _tokens(inner)
    return " " if _CJK_JUNK.search(inner) or any(t in CERTAIN for t in toks) else match.group()


def _trim_trailing(segment: str) -> str:
    """« Yellow Official Video 4K » → « Yellow » (si la fin contient une vraie annotation)."""
    words = list(_WORD.finditer(segment))
    cut, certain = len(words), False
    while cut > 0:
        tok = fold(words[cut - 1].group()).replace("'", "")
        if tok not in TRAILING and not _YEAR.match(tok):
            break
        certain = certain or tok in CERTAIN
        cut -= 1
    if cut == len(words) or cut == 0 or not certain:
        return segment
    return segment[: words[cut - 1].end()].rstrip(_EDGES + " ")


@dataclass
class Parsed:
    parts: list[str] = field(default_factory=list)  # morceaux utiles, dans l'ordre d'origine
    artist_first: bool = False                       # séparés par « - » : « Artiste - Titre » en général

    @property
    def text(self) -> str:
        return " — ".join(self.parts)


def parse(text: str | None) -> Parsed:
    """Découpe un titre en morceaux utiles (titre, artiste…), sans décorations."""
    raw = " ".join(strip_symbols(text).split())
    if not raw:
        return Parsed()
    s = _TRACKNO.sub("", raw)
    for _ in range(3):  # (crochets imbriqués)
        t = _BRACKETS.sub(_bracket, s)
        if t == s:
            break
        s = t
    s = " ".join(s.split())
    first = _SEP.search(s)
    artist_first = bool(first and _DASH.fullmatch(first.group()))
    segments = [_trim_trailing(seg.strip(_EDGES)) for seg in _SEP.split(s)]
    segments = [seg for seg in segments if re.search(r"\w", seg)]
    if not segments:
        return Parsed([raw.strip(_EDGES) or raw], False)

    def junk_only(seg: str) -> bool:
        toks = _tokens(seg)
        return bool(toks) and all(_junky(t) for t in toks)

    plain = [seg for seg in segments if not junk_only(seg)]
    kept = []
    for seg in segments:
        if not junk_only(seg):
            kept.append(seg)
        elif any(t in CERTAIN for t in _tokens(seg)) or len(plain) >= 2:
            continue  # annotation : « Sub. Español 4K », ou « COLORs » quand titre et artiste sont là
        else:
            kept.append(seg)
    if not kept:
        kept = segments[:1]
    return Parsed(kept, artist_first)


def artist_name(artist: str | None) -> str:
    """L'artiste sans « feat. », « VEVO », « - Topic » ; vide si inconnu."""
    s = " ".join(strip_symbols(artist).split())
    s = _feat.sub("", s).strip()
    s = _vevo.sub("", s).strip(_EDGES)
    return "" if (not s or fold(s) in UNKNOWN) else s


def same_artist(a: str, b: str) -> bool:
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    short, long_ = sorted((fa.replace(" ", ""), fb.replace(" ", "")), key=len)
    return len(short) >= 4 and short in long_


def search_name(title: str | None, artist: str | None = "") -> str:
    """Le nom allégé d'un morceau : « Titre — Artiste » (ou ce qu'on en sait)."""
    parts = parse(title).parts
    art = artist_name(artist)
    if art:
        rest = [p for p in parts if not same_artist(p, art)] or parts
        return f"{rest[0]} — {art}" if rest and not same_artist(rest[0], art) else art
    return " — ".join(parts[:2])


def query(text: str | None) -> str:
    """Texte à envoyer à un moteur de recherche (musique en ligne) : les morceaux utiles, séparés par une espace."""
    return " ".join(parse(text).parts) or " ".join((text or "").split())


def title(text: str | None) -> str:
    """Titre allégé pour la bibliothèque : « MOONLIGHT🌙~ Kali Uchis || [AMV] 4K » → « MOONLIGHT — Kali Uchis ».
    Un titre normal (« Mr. Brightside », « Hello (feat. X) », « Video Games ») reste tel quel."""
    original = " ".join((text or "").split())
    short = " — ".join(parse(original).parts).strip()  # (toujours « — » : le résultat ne change plus au 2e passage)
    return short or original
