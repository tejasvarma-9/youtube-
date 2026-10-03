"""Stage 3: voiceover with Google Chirp 3 HD, one request per sentence.

Synthesizing sentence by sentence gives exact timings for images and captions,
and means an edit to one sentence only re-records that sentence.
Writes audio/*.wav (cached), voiceover.wav and timeline.json.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import wave

import requests

from . import config, s2_factcheck
from .common import StageError, Video, log, sentences_with_paragraphs

RATE = 24000
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


def run(video: Video, stub: bool = False, force: bool = False) -> None:
    s2_factcheck.require_pass(video, force)
    if not stub and not config.GOOGLE_TTS_API_KEY:
        raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file (see README), or run with --stub.")

    rows = sentences_with_paragraphs(video.read_text("script.txt"))
    voice = "stub" if stub else config.TTS_VOICE
    chars = sum(len(r["text"]) for r in rows)
    log(f"  voiceover: {len(rows)} sentences, {chars:,} characters, voice {voice}")

    new_chars = 0
    for r in rows:
        key = hashlib.sha1(f"{voice}|{config.TTS_PACE}|{r['text']}".encode()).hexdigest()[:12]
        wav = video.path("audio", f"{key}.wav")
        if not wav.exists():
            pcm = _stub_pcm(r["text"]) if stub else _synthesize(r["text"])
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
    video.write_json("timeline.json", {"voice": voice, "duration": round(total, 3), "characters": chars,
                                       "new_characters": new_chars, "sentences": rows})
    log(f"  voiceover: {total / 60:.1f} minutes ({new_chars:,} characters newly synthesized)")


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
            import time

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
