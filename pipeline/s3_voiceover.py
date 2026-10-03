"""Stage 3: voiceover, one request per sentence (Gemini voice by default, Chirp 3 HD optional).

Synthesizing sentence by sentence gives exact timings for images and captions,
and means an edit to one sentence only re-records that sentence.
Writes audio/*.wav (cached), voiceover.wav and timeline.json.
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import time
import wave

import requests

from . import config, s2_factcheck
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
    log(f"  voiceover: {len(rows)} sentences, {chars:,} characters, voice {voice}")

    new_chars = 0
    for r in rows:
        key = hashlib.sha1(f"{voice_id}|{config.TTS_PACE}|{config.GEMINI_TTS_STYLE}|{r['text']}".encode()).hexdigest()[:12]
        wav = video.path("audio", f"{key}.wav")
        if not wav.exists():
            pcm = _stub_pcm(r["text"]) if stub else (_gemini(r["text"]) if provider == "gemini" else _synthesize(r["text"]))
            _write_wav(wav, pcm)
            new_chars += len(r["text"])
        r["audio"] = str(wav.relative_to(video.dir))

    # Stitch sentences together with short pauses, recording where each one starts.
    t, pcm_all, prev_para = 0.0, bytearray(), None
    for r in rows:
        if prev_para is not None:
            gap = config.SENTENCE_GAP_S + (config.PARAGRAPH_GAP_S if r["paragraph"] != prev_para else 0)
            pcm_all += _silence(gap)
            t += gap
        with wave.open(str(video.dir / r["audio"]), "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise StageError(f"Unexpected audio format in {r['audio']}")
            frames = w.readframes(w.getnframes())
        dur = len(frames) / 2 / RATE
        r["start"], r["end"] = round(t, 3), round(t + dur, 3)
        pcm_all += frames
        t += dur
        prev_para = r["paragraph"]
    pcm_all += _silence(1.0)  # breathing room before the video ends
    _write_wav(video.path("voiceover.wav"), bytes(pcm_all))

    total = len(pcm_all) / 2 / RATE
    video.write_json("timeline.json", {"voice": voice, "provider": "stub" if stub else provider, "duration": round(total, 3), "characters": chars,
                                       "new_characters": new_chars, "sentences": rows})
    log(f"  voiceover: {total / 60:.1f} minutes ({new_chars:,} characters newly synthesized)")


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
