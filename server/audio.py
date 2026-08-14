"""WAV framing and format conversion.

WAV headers are written by hand rather than through soundfile so the streaming
endpoint can emit a header before it knows the final length.
"""
from __future__ import annotations

import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

import numpy as np

from .config import SAMPLE_RATE

# Formats ffmpeg encodes for us: (muxer, codec args, content type, needs_seek).
# The MP4 family cannot be muxed to a pipe - it has to rewind to write the moov
# atom - so those go through a temp file rather than being emitted fragmented,
# which keeps the result playable by the Music app and iOS.
_FFMPEG_FORMATS = {
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"], "audio/mpeg", False),
    "opus": ("ogg", ["-c:a", "libopus", "-b:a", "64k"], "audio/ogg", False),
    "aac": ("adts", ["-c:a", "aac", "-b:a", "128k"], "audio/aac", False),
    "m4a": ("ipod", ["-c:a", "aac", "-b:a", "128k"], "audio/mp4", True),
    "flac": ("flac", [], "audio/flac", False),
}
CONTENT_TYPES = {
    "wav": "audio/wav",
    "pcm": "audio/L16",
    **{fmt: spec[2] for fmt, spec in _FFMPEG_FORMATS.items()},
}

# A streaming WAV cannot know its length up front. Players accept a header that
# overstates the size and simply stop at end-of-stream.
_STREAM_SIZE = 0x7FFFFFFF - 36


def to_pcm16(audio: np.ndarray) -> bytes:
    """Clip to [-1, 1] and quantise to signed 16-bit little-endian."""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def wav_header(data_len: int, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    byte_rate = sample_rate * channels * 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_len)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, channels * 2, 16)
        + b"data"
        + struct.pack("<I", data_len)
    )


def to_wav(audio: np.ndarray) -> bytes:
    pcm = to_pcm16(audio)
    return wav_header(len(pcm)) + pcm


def streaming_wav_header() -> bytes:
    return wav_header(_STREAM_SIZE)


def wav_payload(wav: bytes) -> bytes:
    """Extract the PCM payload from a WAV produced by `to_wav`."""
    return wav[44:]


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def join(segments: Iterable[np.ndarray], gap_s: float) -> np.ndarray:
    """Concatenate audio segments with a short pause between them."""
    parts: list[np.ndarray] = []
    gap = silence(gap_s)
    for i, seg in enumerate(segments):
        if i:
            parts.append(gap)
        parts.append(seg)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def encode(audio: np.ndarray, fmt: str) -> tuple[bytes, str]:
    """Encode to `fmt`, returning (bytes, content type)."""
    fmt = fmt.lower()
    if fmt == "wav":
        return to_wav(audio), CONTENT_TYPES["wav"]
    if fmt == "pcm":
        return to_pcm16(audio), CONTENT_TYPES["pcm"]
    if fmt not in _FFMPEG_FORMATS:
        raise ValueError(f"unsupported response_format: {fmt}")

    container, codec_args, content_type, needs_seek = _FFMPEG_FORMATS[fmt]
    pcm = to_pcm16(audio)
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            *codec_args, "-f", container]

    if not needs_seek:
        proc = subprocess.run(base + ["pipe:1"], input=pcm, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
        return proc.stdout, content_type

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / f"out.{fmt}"
        proc = subprocess.run(base + [str(out)], input=pcm, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
        return out.read_bytes(), content_type
