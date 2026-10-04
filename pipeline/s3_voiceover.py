"""Stage 3: voiceover (Gemini voice by default, Chirp 3 HD optional).

Gemini: Google allows only about 100 voice requests per model per day, so the script goes out in
chunks of a few paragraphs (about 7 requests for a video) and each sentence's timing is found by
looking for the pauses in the returned audio. Chirp: one request per sentence, exact timings.
Either way an edit only re-records the chunk (or sentence) it touches.
Writes audio/*.wav (cached), voiceover.wav and timeline.json.
"""

from __future__ import annotations

import array
import base64
import hashlib
import re
import struct
import sys
import time
import wave

import requests

from . import align, config, s2_factcheck
from .common import StageError, Video, log, sentences_with_paragraphs

RATE = 24000
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


def run(video: Video, stub: bool = False, force: bool = False) -> None:
    s2_factcheck.require_pass(video, force)
    provider = config.TTS_PROVIDER
    if provider not in ("gemini", "chirp"):
        raise StageError(f"TTS_PROVIDER must be 'gemini' or 'chirp', not '{provider}'.")
    key = config.GOOGLE_API_KEY if provider == "gemini" else config.GOOGLE_TTS_API_KEY
    if not stub and not key:
        raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file (see README), or run with --stub.")

    rows = sentences_with_paragraphs(video.read_text("script.txt"))
    voice = "stub" if stub else (config.GEMINI_TTS_VOICE if provider == "gemini" else config.TTS_VOICE)
    voice_id = f"{provider}:{config.GEMINI_TTS_MODEL}:{voice}" if provider == "gemini" else f"{provider}:{voice}"
    chars = sum(len(r["text"]) for r in rows)
    chunks = _chunk_rows(rows, config.GEMINI_TTS_CHUNK_CHARS) if provider == "gemini" else [[r] for r in rows]
    log(f"  voiceover: {len(rows)} sentences, {chars:,} characters, voice {voice}, {len(chunks)} requests at most")

    t, pcm_all, new_chars, requests_made, prev_para = 0.0, bytearray(), 0, 0, None
    for chunk in chunks:
        text = _chunk_text(chunk)
        key = hashlib.sha1(f"{voice_id}|{config.TTS_PACE}|{config.GEMINI_TTS_STYLE}|{text}".encode()).hexdigest()[:12]
        wav = video.path("audio", f"{key}.wav")
        if not wav.exists():
            if stub:
                pcm = _stub_pcm(text)
            elif provider == "gemini":
                pcm = _gemini_checked(text)
            else:
                pcm = _synthesize(text)
            _write_wav(wav, pcm)
            new_chars += len(text)
            requests_made += 1
        with wave.open(str(wav), "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise StageError(f"Unexpected audio format in {wav.name}")
            frames = w.readframes(w.getnframes())

        if prev_para is not None:
            gap = config.SENTENCE_GAP_S + (config.PARAGRAPH_GAP_S if chunk[0]["paragraph"] != prev_para else 0)
            pcm_all += _silence(gap)
            t += gap
        if len(chunk) == 1:
            start, end, bounds = 0.0, len(frames) / 2 / RATE, []
        else:
            start, end, bounds = _locate_sentences(frames, [len(r["text"]) for r in chunk])
            frames = frames[int(start * RATE) * 2:int(end * RATE) * 2]
            bounds = [b - start for b in bounds]
            end -= start
        edges = [0.0] + bounds + [len(frames) / 2 / RATE]
        for r, a, b in zip(chunk, edges, edges[1:]):
            r["start"], r["end"] = round(t + a, 3), round(t + b, 3)
        pcm_all += frames
        t += len(frames) / 2 / RATE
        prev_para = chunk[-1]["paragraph"]
    pcm_all += _silence(1.0)  # breathing room before the video ends
    _write_wav(video.path("voiceover.wav"), bytes(pcm_all))

    total = len(pcm_all) / 2 / RATE
    tl = {"voice": voice, "provider": "stub" if stub else provider, "duration": round(total, 3), "characters": chars,
          "new_characters": new_chars, "requests": requests_made, "sentences": rows}
    tl["timing"] = "pauses" if stub else align.align(video, tl)
    video.write_json("timeline.json", tl)
    log(f"  voiceover: {total / 60:.1f} minutes ({requests_made} requests, {new_chars:,} characters newly synthesized)")


def _chunk_text(chunk: list) -> str:
    paras, last = [], None
    for r in chunk:
        if r["paragraph"] != last:
            paras.append([])
            last = r["paragraph"]
        paras[-1].append(r["text"])
    return "\n\n".join(" ".join(p) for p in paras)


def _chunk_rows(rows: list, limit: int) -> list:
    """Pack whole paragraphs into chunks of at most `limit` characters (a longer paragraph is split by sentence)."""
    paras = []
    for r in rows:
        if not paras or paras[-1][0]["paragraph"] != r["paragraph"]:
            paras.append([])
        paras[-1].append(r)

    def size(g):
        return sum(len(r["text"]) + 1 for r in g)

    chunks, cur, cur_len = [], [], 0
    for g in paras:
        pieces = [g]
        if size(g) > limit:
            pieces, piece, plen = [], [], 0
            for r in g:
                if piece and plen + len(r["text"]) + 1 > limit:
                    pieces.append(piece)
                    piece, plen = [], 0
                piece.append(r)
                plen += len(r["text"]) + 1
            pieces.append(piece)
        for pc in pieces:
            if cur and cur_len + size(pc) > limit:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur += pc
            cur_len += size(pc)
    if cur:
        chunks.append(cur)
    return chunks


FRAME = 480            # 20 ms of audio
MIN_PAUSE_S = 0.16     # a silence at least this long can separate two sentences
MIN_CHARS_PER_S = 30   # real narration runs about 14 characters a second; far below this means the model cut the text short


def _gemini_checked(text: str) -> bytes:
    """A chunk through the voice model, retried if the audio is far too short for the text (it was cut off)."""
    for attempt in range(3):
        pcm = _gemini(text)
        if len(pcm) / 2 / RATE >= len(text) / MIN_CHARS_PER_S:
            return pcm
        log(f"    the voice cut a passage short ({len(pcm) / 2 / RATE:.0f}s for {len(text)} characters); trying again")
    raise StageError(f"The voice model keeps cutting this passage short: \"{text[:60]}...\". "
                     "Lower GEMINI_TTS_CHUNK_CHARS in .env (for example 800) and re-run.")


def _levels(pcm: bytes) -> list:
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return [max(map(abs, a[i:i + FRAME])) for i in range(0, len(a), FRAME)]


def _locate_sentences(pcm: bytes, sizes: list) -> tuple:
    """Where speech starts and ends in a chunk, and the time of each boundary between its sentences.

    Boundaries go at the pauses in the audio, matched to where the sentence lengths say they should
    fall. Pauses within a sentence (commas) cost a little, longer pauses are favoured, and with no
    usable pause the proportional position is used. Returns (speech_start, speech_end, [boundaries]).
    """
    dur, fl = len(pcm) / 2 / RATE, FRAME / RATE
    levels = _levels(pcm)
    peak = max(levels, default=0)
    gaps = []
    if peak < 300:
        s0, s1 = 0.0, dur  # silence (stub audio): proportional timing only
    else:
        thr = peak * 0.03
        loud = [i for i, l in enumerate(levels) if l >= thr]
        s0, s1 = max(0.0, loud[0] * fl - 0.03), min(dur, (loud[-1] + 1) * fl + 0.12)
        run = None
        for i in range(loud[0], loud[-1] + 1):
            if levels[i] < thr:
                run = i if run is None else run
            elif run is not None:
                if (i - run) * fl >= MIN_PAUSE_S:
                    gaps.append((run * fl, i * fl))
                run = None
    total, cum, expected = sum(sizes), 0, []
    for n in sizes[:-1]:
        cum += n
        expected.append(s0 + (s1 - s0) * cum / total)
    need = len(expected)
    if need == 0:
        return s0, s1, []
    if len(gaps) < need:
        return s0, s1, expected  # can't tell the pauses apart; proportional timing

    def cost(k, g):
        a, b = gaps[g]
        return ((((a + b) / 2) - expected[k]) / 3.0) ** 2 - 2.0 * min(b - a, 0.8)

    inf = float("inf")
    best = [[inf] * len(gaps) for _ in range(need)]
    back = [[-1] * len(gaps) for _ in range(need)]
    for g in range(len(gaps)):
        best[0][g] = cost(0, g)
    for k in range(1, need):
        run_best, run_arg = inf, -1
        for g in range(len(gaps)):
            if g > 0 and best[k - 1][g - 1] < run_best:
                run_best, run_arg = best[k - 1][g - 1], g - 1
            if run_best < inf:
                best[k][g] = run_best + cost(k, g)
                back[k][g] = run_arg
    g = min(range(len(gaps)), key=lambda x: best[need - 1][x])
    picks = []
    for k in range(need - 1, -1, -1):
        picks.append(g)
        g = back[k][g]
    picks.reverse()
    return s0, s1, [(gaps[g][0] + gaps[g][1]) / 2 for g in picks]


_last_call = [0.0]


def _retry_delay(res) -> float:
    """How long Google says to wait before retrying ('retryDelay': '23s'), or 0 if it doesn't say."""
    try:
        for d in res.json().get("error", {}).get("details", []):
            m = re.fullmatch(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    except (ValueError, AttributeError):
        pass
    m = re.search(r"retry in ([\d.]+)s", res.text)
    return float(m.group(1)) if m else 0.0


def _gemini(text: str, voice: str | None = None) -> bytes:
    """One sentence through the Gemini API's voice model; returns raw 24 kHz 16-bit mono PCM."""
    body = {
        "contents": [{"parts": [{"text": f"{config.GEMINI_TTS_STYLE}: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or config.GEMINI_TTS_VOICE}}},
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_TTS_MODEL}:generateContent"
    last = ""
    for attempt in range(8):
        wait = config.GEMINI_TTS_MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        res = requests.post(url, json=body, headers={"x-goog-api-key": config.GOOGLE_API_KEY}, timeout=120)
        delay = 2 ** (attempt + 1)
        if res.status_code == 200:
            for cand in res.json().get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    blob = part.get("inlineData") or part.get("inline_data")
                    if blob and blob.get("data"):
                        return base64.b64decode(blob["data"])
            last = "the model returned no audio"  # happens occasionally; retrying usually works
        elif res.status_code == 429:
            low = res.text.lower()
            if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low or "limit: 0" in low:
                raise StageError(
                    "Gemini voice says the quota for this key is used up or not available "
                    "(a daily limit, or no paid billing on this key's project). Details from Google:\n"
                    f"{res.text[:900]}\n"
                    "Check the credit balance and billing status at https://aistudio.google.com/billing, then re-run. "
                    "Sentences already recorded are kept.")
            # A per-minute limit: wait as long as Google asks (at least 15s, growing), then try again.
            delay = max(_retry_delay(res) + 1, 15 * (attempt + 1))
            last = f"429 rate limit, waiting {delay:.0f}s: {res.text[:300]}"
            log(f"    rate limit from Gemini; waiting {delay:.0f}s (attempt {attempt + 1} of 8)")
        elif res.status_code in (500, 503) or res.status_code == 400 and "audio" in res.text.lower():
            last = f"{res.status_code} {res.text[:300]}"
        else:
            raise StageError(f"Gemini voice returned {res.status_code}: {res.text[:600]}")
        time.sleep(delay)
    raise StageError(f"Gemini voice failed after 8 tries for: \"{text[:60]}...\" ({last}). Re-run to retry just the missing sentences.")


def _synthesize(text: str) -> bytes:
    body = {
        "input": {"text": text},
        "voice": {"languageCode": config.TTS_LANGUAGE, "name": config.TTS_VOICE},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": RATE},
    }
    if config.TTS_PACE != 1.0:
        body["audioConfig"]["speakingRate"] = config.TTS_PACE
    for attempt in range(4):
        res = requests.post(TTS_URL, json=body, headers={"x-goog-api-key": config.GOOGLE_TTS_API_KEY}, timeout=60)
        if res.status_code == 200:
            break
        if res.status_code in (429, 500, 503) and attempt < 3:
            time.sleep(2 ** (attempt + 1))
            continue
        hint = ""
        if res.status_code == 403:
            hint = (" Turn on the Cloud Text-to-Speech API for the same Google Cloud project as your key, "
                    "and make sure the key isn't restricted to the Gemini API only (README, step 2).")
        raise StageError(f"Text-to-Speech returned {res.status_code}: {res.text[:400]}{hint}")
    audio = base64.b64decode(res.json()["audioContent"])
    return _strip_wav_header(audio)


def _strip_wav_header(data: bytes) -> bytes:
    if data[:4] != b"RIFF":
        return data
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"data":
            return data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    raise StageError("Couldn't read the audio returned by Text-to-Speech.")


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def _stub_pcm(text: str) -> bytes:
    return _silence(max(1.2, len(text.split()) / 160 * 60))


def _write_wav(path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


SAMPLE_TEXT = (
    "A vending machine in a busy hospital hallway can quietly earn more per square foot than the gift shop "
    "next to it. A used machine runs roughly $1,500 to $3,000, and the building usually takes 10% to 20% of "
    "sales. So, who pays whom?"
)
SAMPLE_VOICES = ["Charon", "Orus", "Iapetus", "Algieba", "Kore"]


def voice_test(voices: list[str] | None = None, stub: bool = False) -> list:
    """Short samples in several voices (about a cent in total) so Tejas can pick one by ear."""
    if config.TTS_PROVIDER != "gemini":
        raise StageError("voice-test only covers the Gemini voices. Set TTS_PROVIDER=gemini (the default).")
    if not stub and not config.GOOGLE_API_KEY:
        raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file (see README).")
    out = config.OUT / "voice-samples"
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for v in voices or SAMPLE_VOICES:
        path = out / f"{v}.wav"
        _write_wav(path, _stub_pcm(SAMPLE_TEXT) if stub else _gemini(SAMPLE_TEXT, voice=v))
        log(f"  {v}: {path.relative_to(config.ROOT)}")
        made.append(path)
    return made
