"""Calage des paroles sur la voix (stable-whisper) — tourne dans un processus séparé.

Lancé par `onde.align` (`python -m onde.align_worker`), une tâche à la fois :
  - la tâche (JSON) arrive sur l'entrée standard ;
  - l'avancement et le résultat repartent sur la sortie standard, une ligne JSON par message.

Pourquoi un processus à part : le modèle d'IA pèse plusieurs centaines de Mo. S'il manque de
mémoire (le noyau tue alors le processus), seul ce travailleur disparaît, Onde continue de jouer
la musique ; et toute la mémoire est rendue au système à la fin de chaque morceau.

Messages envoyés :
  {"phase": "audio" | "model" | "language" | "align"}      étape en cours
  {"progress": 0.42}                                        avancement de l'alignement (0 à 1)
  {"ok": true, "segments": [...], "language": "fr"}         résultat
  {"ok": false, "error": "audio" | "memory" | "model" | "missing" | "mismatch" | "engine", "message": "…"}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SAMPLE_RATE = 16000


class AudioError(Exception):
    pass


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(kind: str, message: str) -> None:
    emit({"ok": False, "error": kind, "message": message[:500]})


def decode(ffmpeg: str, path: str):
    """Audio → échantillons 16 kHz mono (float32), décodés par ffmpeg (celui du système ou celui
    fourni par imageio-ffmpeg). Lève AudioError si le fichier est illisible."""
    import numpy as np

    if not ffmpeg:
        raise AudioError("ffmpeg introuvable (ni installé sur le système, ni fourni par imageio-ffmpeg)")
    if not os.path.isfile(path):
        raise AudioError("fichier audio introuvable")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-vn",
           "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise AudioError("décodage trop long (fichier abîmé ?)")
    except OSError as exc:
        raise AudioError(f"ffmpeg ne démarre pas : {exc}")
    err = r.stderr.decode("utf-8", "replace").strip().splitlines()
    if r.returncode != 0 or len(r.stdout) < SAMPLE_RATE * 2:  # moins d'une seconde de son
        raise AudioError(err[-1] if err else "aucun son décodé")
    return np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768.0


def _is_memory_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, MemoryError) or any(
        k in text for k in ("out of memory", "can't allocate memory", "cannot allocate memory",
                            "not enough memory", "defaultcpuallocator", "std::bad_alloc",
                            "failed to map segment"))


def _detect_language(model, audio) -> str | None:
    """Langue chantée, devinée par Whisper sur 30 s prises au milieu du morceau (là où on chante)."""
    import whisper

    mid = max(0, len(audio) // 2 - 15 * SAMPLE_RATE)
    chunk = whisper.pad_or_trim(audio[mid: mid + 30 * SAMPLE_RATE])
    mel = whisper.log_mel_spectrogram(chunk, n_mels=model.dims.n_mels).to(model.device)
    _, probs = model.detect_language(mel)
    if isinstance(probs, list):
        probs = probs[0]
    return max(probs, key=probs.get) if probs else None


def run(job: dict) -> None:
    try:
        os.nice(int(job.get("nice", 10)))  # la musique et Onde restent prioritaires
    except (AttributeError, OSError, ValueError):
        pass
    threads = max(1, int(job.get("threads") or 1))
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = str(threads)

    try:
        import stable_whisper
        import torch
    except ImportError as exc:
        if _is_memory_error(exc):  # la bibliothèque est là, mais pas la mémoire pour la charger
            return fail("memory", "mémoire insuffisante pour charger le moteur d'IA")
        return fail("missing", f"module d'IA absent ({exc.name or exc})")
    torch.set_num_threads(threads)

    audio = None
    if not job.get("download_only"):
        emit({"phase": "audio"})
        try:
            audio = decode(job.get("ffmpeg") or "", job["audio"])
        except AudioError as exc:
            return fail("audio", str(exc))
        except MemoryError:
            return fail("memory", "mémoire insuffisante pour décoder le fichier")

    emit({"phase": "model"})
    name = os.path.basename(str(job.get("model") or "base"))
    try:
        model = stable_whisper.load_model(job.get("model") or "base", device="cpu",
                                          download_root=job.get("download_root") or None)
    except Exception as exc:  # téléchargement impossible, fichier du modèle abîmé, mémoire…
        if _is_memory_error(exc):
            return fail("memory", f"mémoire insuffisante pour charger le modèle « {name} »")
        return fail("model", f"modèle « {name} » indisponible : {exc}")
    if job.get("download_only"):
        return emit({"ok": True, "segments": [], "language": None})

    try:
        language = job.get("language")
        if not language:
            emit({"phase": "language"})
            language = _detect_language(model, audio) or "fr"
        emit({"phase": "align"})
        total = len(audio) / SAMPLE_RATE
        last = [-1.0]

        def progress(seek, duration=None):
            p = min(1.0, float(seek) / float(duration or total or 1))
            if p - last[0] >= 0.05:
                last[0] = p
                emit({"progress": round(p, 3)})

        # original_split=True : un segment par ligne du texte fourni. Sans cela, stable-whisper
        # regroupe les lignes à sa façon (et entasse souvent la fin du texte sur la fin du morceau).
        result = model.align(audio, "\n".join(job["lines"]), language=language, original_split=True,
                             failure_threshold=job.get("failure_threshold"), fast_mode=False,
                             progress_callback=progress, verbose=None)
    except Exception as exc:
        if _is_memory_error(exc):
            return fail("memory", f"mémoire insuffisante pendant le calage (modèle « {name} »)")
        return fail("engine", f"{type(exc).__name__}: {exc}")
    if result is None:  # trop de mots sans durée : le texte ne correspond pas à ce qui est chanté
        return fail("mismatch", "les paroles ne correspondent pas à ce qui est chanté")

    segments = []
    for seg in result.segments:
        words = list(getattr(seg, "words", None) or [])
        probs = [float(w.probability) for w in words if getattr(w, "probability", None) is not None]
        segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip(),
                         "prob": sum(probs) / len(probs) if probs else 0.0,
                         "zero": sum(1 for w in words if float(w.end) - float(w.start) <= 0.0),
                         "words": len(words)})
    emit({"ok": True, "segments": segments, "language": language})


def main() -> None:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return fail("engine", "tâche illisible")
    try:
        run(job)
    except MemoryError:
        fail("memory", "mémoire insuffisante")
    except Exception as exc:  # jamais de trace brute : le message doit rester lisible dans Onde
        fail("engine", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
