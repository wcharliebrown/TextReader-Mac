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

# Sentence chunking. Only the first chunk needs to be short - it decides how
# soon audio starts. After that the prefetch is already running ahead, so
# chunks are packed with whole sentences to give Kokoro room to carry intonation
# across a sentence boundary instead of restarting at every full stop.
MIN_CHUNK_CHARS = int(os.environ.get("TEXTREADER_MIN_CHUNK", "40"))
FIRST_CHUNK_CHARS = int(os.environ.get("TEXTREADER_FIRST_CHUNK", "150"))
TARGET_CHUNK_CHARS = int(os.environ.get("TEXTREADER_TARGET_CHUNK", "300"))
# Hard ceiling. Measured: Kokoro renders one segment up to ~493 characters and
# splits internally at ~521, and an internal seam carries 0.5s of silence that
# trim_edges cannot reach. Staying under that keeps every seam ours to control.
MAX_CHUNK_CHARS = int(os.environ.get("TEXTREADER_MAX_CHUNK", "450"))

# Kokoro renders ~0.30s of lead-in and ~0.47s of tail silence around every
# chunk. Left in place they stack with the gap below, so a sentence boundary
# ran to ~0.86s - roughly double the 0.30-0.52s Kokoro renders between
# sentences it is given together. Trim the chunk, then insert one deliberate
# pause, so the seam sounds like the ones the model produces itself.
TRIM_THRESHOLD = 0.01
TRIM_PAD_S = 0.02
SENTENCE_GAP_S = 0.34
PARAGRAPH_GAP_S = 0.60

# Bumped whenever the audio pipeline changes what a given text sounds like.
# It is part of the cache key, so old renders are not served after a change.
AUDIO_REV = 2

# How many prepared jobs to remember (for /v1/audio/{key} lookups).
JOB_HISTORY = 32
KEY_REGISTRY_MAX = 20000

MAX_INPUT_CHARS = int(os.environ.get("TEXTREADER_MAX_INPUT", "200000"))
