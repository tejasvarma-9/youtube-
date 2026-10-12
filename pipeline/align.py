"""Word timings for the captions, from the finished voiceover.

The voice model returns audio only, no timestamps, so sentence times were estimated from pauses
and caption lines inside a sentence were spread by character count. That drifts in places
(numbers take longer to say than to write). Here a small speech recognizer (faster-whisper,
run locally on the Mac, free) hears the voiceover and gives each recognized word its time. The
script's own words are then matched to those words, so captions use the script's exact text
with the times the narrator actually said them.

Optional: if faster-whisper isn't installed, or the match is poor, timings stay as they were.
"""

from __future__ import annotations

import difflib
import re

from . import config
from .common import log

MIN_MATCH = 0.6  # share of script words that must be matched, or the alignment is not trusted


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def transcribe(wav_path) -> list[tuple[str, float, float]]:
    from faster_whisper import WhisperModel

    model = WhisperModel(config.ALIGN_MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav_path), language="en", word_timestamps=True, beam_size=1)
    return [(w.word, w.start, w.end) for seg in segments for w in (seg.words or [])]


def match(sentences: list[dict], heard: list[tuple[str, float, float]], duration: float) -> list[list] | None:
    """Per sentence, a [start, end] for each of its words (split on spaces), or None if unreliable.

    Script words the recognizer heard differently ("$297.2" vs "297.2", "FY2026") are matched by
    their normalized form; any still unmatched get times interpolated between matched neighbours.
    """
    words = [(si, w) for si, s in enumerate(sentences) for w in s["text"].split()]
    if not words or not heard:
        return None
    a = [_norm(w) for _, w in words]
    b = [_norm(w) for w, _, _ in heard]
    times = [None] * len(words)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            _, s, e = heard[blk.b + k]
            times[blk.a + k] = [float(s), float(e)]
    matched = sum(t is not None for t in times)
    if matched / len(words) < MIN_MATCH:
        return None

    # Fill gaps: spread unmatched words between the matched words around them, by length.
    n = len(words)
    k = 0
    while k < n:
        if times[k] is not None:
            k += 1
            continue
        j = k
        while j < n and times[j] is None:
            j += 1
        left = times[k - 1][1] if k > 0 else 0.0
        right = times[j][0] if j < n else duration
        right = max(right, left)
        lengths = [max(len(words[m][1]), 1) for m in range(k, j)]
        total, t = sum(lengths), left
        for m, L in zip(range(k, j), lengths):
            d = (right - left) * L / total
            times[m] = [t, t + d]
            t += d
        k = j

    # Keep times in order (the recognizer occasionally overlaps neighbouring words).
    for m in range(1, n):
        if times[m][0] < times[m - 1][0]:
            times[m][0] = times[m - 1][0]
        times[m][1] = max(times[m][1], times[m][0])

    out: list[list] = [[] for _ in sentences]
    for (si, _), t in zip(words, times):
        out[si].append([round(t[0], 3), round(t[1], 3)])
    return out


def apply(sentences: list[dict], word_times: list[list], duration: float) -> None:
    """Sentence start = its first word; it lasts until the next sentence starts."""
    for s, wt in zip(sentences, word_times):
        s["words"] = wt
        s["start"] = wt[0][0] if wt else s["start"]
    for s, nxt in zip(sentences, sentences[1:]):
        s["end"] = round(max(nxt["start"], s["start"]), 3)
    last = sentences[-1]
    last["end"] = round(min(duration, (last["words"][-1][1] if last.get("words") else last["end"]) + 0.3), 3)


def align(video, timeline: dict) -> str:
    """Re-time timeline sentences from voiceover.wav. Returns how the timings were made."""
    if not available():
        return "pauses"
    log(f"  listening to the voiceover for word timings ({config.ALIGN_MODEL}, on this Mac)...")
    try:
        heard = transcribe(video.path("voiceover.wav"))
    except Exception as e:  # a model download or decode problem must not stop the video
        log(f"  WARNING: word timing failed ({e}); captions use the pause-based timings.")
        return "pauses"
    word_times = match(timeline["sentences"], heard, timeline["duration"])
    if word_times is None:
        log("  WARNING: the recognized words didn't match the script well; captions use the pause-based timings.")
        return "pauses"
    apply(timeline["sentences"], word_times, timeline["duration"])
    return f"words:{config.ALIGN_MODEL}"
