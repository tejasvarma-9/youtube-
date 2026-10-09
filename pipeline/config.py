"""Settings for the pipeline. Secrets come from .env; everything else lives here."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
CHANNEL_NAME = "Who Pays Who"
PROMPTS = ROOT / "prompts"
ASSETS = ROOT / "assets"
STYLE_REFS = ASSETS / "style-refs"
LOGO = ASSETS / "brand" / "logo.png"
VOICE_FILE = ROOT / "voice.md"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # dotenv is optional when running fully on stubs
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


GOOGLE_API_KEY = env("GOOGLE_API_KEY")
GOOGLE_TTS_API_KEY = env("GOOGLE_TTS_API_KEY") or GOOGLE_API_KEY

# Script writing, fact-checking, shot lists and metadata run through the
# Claude Code CLI on your Mac, so they use your Claude plan instead of API money.
CLAUDE_BIN = env("CLAUDE_BIN", "claude")
CLAUDE_MODEL = env("CLAUDE_MODEL")  # empty = Claude Code's default model
CLAUDE_TIMEOUT_S = int(env("CLAUDE_TIMEOUT_S", "1800"))

# Voiceover. Default is the Gemini API's voice model: same key and same prepaid credits as images.
# Set TTS_PROVIDER=chirp to use Google Cloud Text-to-Speech (Chirp 3 HD) instead; that needs
# Cloud billing, which prepaid Gemini credits do not cover.
TTS_PROVIDER = env("TTS_PROVIDER", "gemini").lower()
GEMINI_TTS_MODEL = env("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts")
GEMINI_TTS_VOICE = env("GEMINI_TTS_VOICE", "Charon")
GEMINI_TTS_STYLE = env("GEMINI_TTS_STYLE", "Say like a friendly, upbeat YouTube explainer who finds this story fascinating: lively, "
                       "expressive, with real excitement on the surprising parts, and a quick, natural pace")
GEMINI_TTS_CHUNK_CHARS = int(env("GEMINI_TTS_CHUNK_CHARS", "1500"))  # text per voice request; Google caps requests per day
GEMINI_TTS_MIN_INTERVAL_S = float(env("GEMINI_TTS_MIN_INTERVAL_S", "0"))  # raise if you keep hitting rate limits
TTS_VOICE = env("TTS_VOICE", "en-US-Chirp3-HD-Charon")  # chirp only
TTS_LANGUAGE = "en-US"
TTS_PACE = float(env("TTS_PACE", "1.0"))  # chirp only: 0.25 to 2.0
SENTENCE_GAP_S = 0.18  # silence between sentences
PARAGRAPH_GAP_S = 0.45  # extra silence between paragraphs

# Images: Gemini 3.1 Flash Lite Image ("Nano Banana 2 Lite"), batch mode.
IMAGE_MODEL = env("IMAGE_MODEL", "gemini-3.1-flash-lite-image")
IMAGE_BATCH_SIZE = 10  # requests per batch job, keeps inline results under 20 MB
IMAGE_STYLE_SUFFIX = (
    "Style: simple 2D hand-drawn explainer illustration, round-headed stick figures, "
    "thick black outlines, flat colors, light shading, white or light gray-blue background, "
    "16:9 aspect ratio. No photorealism, no 3D, no real logos, no real people. "
    "No text of any kind: no letters, words, numbers, labels, signs, banners or symbols anywhere in the picture; "
    "papers, scrolls, signs, bags and clothing stay plain and blank."
)
# Each image stays on screen for at least this long. Lower = more images = more cost.
# 8 s after a fast first minute is roughly 85 to 95 images per 10-minute video, about $1.55.
MIN_SHOT_SECONDS = float(env("MIN_SHOT_SECONDS", "8"))
# Logo end card after the voice ends (seconds). Every video has one: at least 3 s. YouTube end-screen elements sit on top of it.
END_CARD_SECONDS = max(3.0, float(env("END_CARD_SECONDS", "6")))
# The first minute is where viewers leave, so pictures change faster there: a new picture
# about every sentence (at least FAST_START_SHOT_SECONDS each) for the first FAST_START_SECONDS.
# Set FAST_START_SECONDS=0 to turn it off.
FAST_START_SECONDS = float(env("FAST_START_SECONDS", "60"))
FAST_START_SHOT_SECONDS = float(env("FAST_START_SHOT_SECONDS", "3"))
MAX_SHOT_SECONDS = 18.0

# Prices used for the cost line in the review package (USD, checked 2026-10-03).
PRICE_PER_IMAGE_BATCH = 0.0168
PRICE_PER_IMAGE_SYNC = 0.0336
TTS_FREE_CHARS_PER_MONTH = 1_000_000  # chirp
PRICE_PER_TTS_CHAR = 0.00003  # chirp
GEMINI_TTS_USD_PER_M_AUDIO_TOKENS = 6.00  # flash-lite-tts; doubles from 2027-01-01
GEMINI_AUDIO_TOKENS_PER_SECOND = 25


def voice_cost(provider: str, seconds: float, chars: int) -> float:
    if provider == "chirp":
        return chars * PRICE_PER_TTS_CHAR  # free under 1M characters a month
    if provider == "gemini":
        return seconds * GEMINI_AUDIO_TOKENS_PER_SECOND * GEMINI_TTS_USD_PER_M_AUDIO_TOKENS / 1e6
    return 0.0

# Video.
WIDTH, HEIGHT, FPS = 1920, 1080, 30
# The image model makes 1K pictures (about 1376x768), smaller than the 1920x1080 video. If the free
# Real-ESRGAN upscaler is installed (README: "Sharper images"), each picture is upscaled 2x first.
UPSCALER_BIN = env("UPSCALER_BIN") or str(ROOT / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan")
UPSCALE_MODEL = env("UPSCALE_MODEL", "realesr-animevideov3")
# Caption timing: a local speech recognizer (faster-whisper) finds when each word is said.
ALIGN_MODEL = env("ALIGN_MODEL", "base.en")
TARGET_WORDS = 2200
WORDS_MIN, WORDS_MAX = 1800, 2500

DISCLAIMER = (
    "Sources are listed above. Figures marked as estimates are our own calculations "
    "or industry estimates.\n"
    "For education and entertainment only. Not financial, investment, legal or tax advice."
)
