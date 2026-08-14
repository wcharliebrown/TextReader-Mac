"""Configuration, all overridable by TEXTREADER_* environment variables."""
from __future__ import annotations

import os
from pathlib import Path

SAMPLE_RATE = 24000

ENGINE = os.environ.get("TEXTREADER_ENGINE", "mlx")
MODEL_ID = os.environ.get("TEXTREADER_MODEL", "mlx-community/Kokoro-82M-bf16")

DEFAULT_VOICE = os.environ.get("TEXTREADER_VOICE", "af_heart")
DEFAULT_SPEED = float(os.environ.get("TEXTREADER_SPEED", "1.0"))
DEFAULT_LANG = os.environ.get("TEXTREADER_LANG", "a")

HOST = os.environ.get("TEXTREADER_HOST", "127.0.0.1")
PORT = int(os.environ.get("TEXTREADER_PORT", "8842"))

CACHE_DIR = Path(
    os.environ.get("TEXTREADER_CACHE_DIR", Path.home() / "Library/Caches/TextReaderAPI")
)
CACHE_MAX_BYTES = int(os.environ.get("TEXTREADER_CACHE_MAX_BYTES", 2 * 1024**3))
CACHE_ENABLED = os.environ.get("TEXTREADER_CACHE", "1") not in ("0", "false", "no")

# Sentence chunking. Short enough that the first chunk lands fast, long enough
# that Kokoro's prosody spans a natural clause.
MIN_CHUNK_CHARS = int(os.environ.get("TEXTREADER_MIN_CHUNK", "40"))
MAX_CHUNK_CHARS = int(os.environ.get("TEXTREADER_MAX_CHUNK", "300"))

# Silence inserted between sentences when concatenating a single audio file.
SENTENCE_GAP_S = 0.12
PARAGRAPH_GAP_S = 0.35

# How many prepared jobs to remember (for /v1/audio/{key} lookups).
JOB_HISTORY = 32
KEY_REGISTRY_MAX = 20000

MAX_INPUT_CHARS = int(os.environ.get("TEXTREADER_MAX_INPUT", "200000"))
