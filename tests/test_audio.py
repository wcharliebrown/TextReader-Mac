"""Encoder coverage. The m4a path regressed once because the MP4 muxer cannot
write to a pipe, so every advertised format gets exercised for real here."""
from __future__ import annotations

import numpy as np
import pytest

from server import audio as au
from server.config import SAMPLE_RATE


@pytest.fixture()
def tone():
    t = np.linspace(0, 1, SAMPLE_RATE, dtype=np.float32)
    return (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)


def test_wav_header_is_wellformed(tone):
    wav = au.to_wav(tone)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + len(tone) * 2
    # The declared payload size must match what actually follows the header.
    declared = int.from_bytes(wav[40:44], "little")
    assert declared == len(wav) - 44


def test_pcm_roundtrip_preserves_signal(tone):
    back = np.frombuffer(au.to_pcm16(tone), dtype="<i2").astype(np.float32) / 32767.0
    assert np.max(np.abs(back - tone)) < 1e-3


def test_clipping_is_bounded():
    loud = np.array([5.0, -5.0], dtype=np.float32)
    samples = np.frombuffer(au.to_pcm16(loud), dtype="<i2")
    assert samples.tolist() == [32767, -32767]


@pytest.mark.parametrize(
    "fmt, magic",
    [
        ("wav", b"RIFF"),
        ("mp3", None),
        ("flac", b"fLaC"),
        ("opus", b"OggS"),
        ("aac", None),
        ("m4a", None),   # regression: the MP4 muxer needs seekable output
    ],
)
def test_encode_produces_real_audio(tone, fmt, magic):
    data, content_type = au.encode(tone, fmt)
    assert len(data) > 500, f"{fmt} output is suspiciously small"
    assert content_type == au.CONTENT_TYPES[fmt]
    if magic:
        assert data[:4] == magic


def test_m4a_is_an_iso_container(tone):
    data, _ = au.encode(tone, "m4a")
    assert data[4:8] == b"ftyp", "m4a must be a real MP4 container"


def test_unknown_format_raises(tone):
    with pytest.raises(ValueError):
        au.encode(tone, "midi")


def test_join_inserts_gaps(tone):
    joined = au.join([tone, tone], gap_s=0.5)
    assert len(joined) == 2 * len(tone) + int(0.5 * SAMPLE_RATE)


def test_join_of_nothing_is_empty():
    assert len(au.join([], 0.1)) == 0


def test_streaming_header_declares_open_ended_length():
    header = au.streaming_wav_header()
    assert header[:4] == b"RIFF"
    assert int.from_bytes(header[40:44], "little") > 10**9
