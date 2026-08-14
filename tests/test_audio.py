"""Encoder coverage. The m4a path regressed once because the MP4 muxer cannot
write to a pipe, so every advertised format gets exercised for real here."""
from __future__ import annotations

import numpy as np
import pytest

from server import audio as au
from server import config as au_cfg
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


# --------------------------------------------------------------------------
# Chunk edges. Kokoro renders ~0.30s of lead-in and ~0.47s of tail on every
# chunk; left in, they stack with the programmed gap at every seam.
# --------------------------------------------------------------------------


def _padded(lead_s: float, body_s: float, tail_s: float) -> np.ndarray:
    return np.concatenate([
        np.zeros(int(lead_s * SAMPLE_RATE), dtype=np.float32),
        np.full(int(body_s * SAMPLE_RATE), 0.5, dtype=np.float32),
        np.zeros(int(tail_s * SAMPLE_RATE), dtype=np.float32),
    ])


def test_trim_edges_removes_lead_and_tail_silence():
    trimmed = au.trim_edges(_padded(0.30, 1.0, 0.47))
    # The pad on each side is deliberate, so the attack and decay survive.
    expected = 1.0 + 2 * au_cfg.TRIM_PAD_S
    assert abs(trimmed.size / SAMPLE_RATE - expected) < 0.01


def test_trim_edges_keeps_silence_in_the_middle():
    with_pause = np.concatenate([_padded(0.3, 0.5, 0.0), _padded(0.4, 0.5, 0.47)])
    trimmed = au.trim_edges(with_pause)
    assert trimmed.size / SAMPLE_RATE > 1.3, "an internal pause was eaten"


@pytest.mark.parametrize("audio", [np.zeros(0, dtype=np.float32), np.zeros(4800, dtype=np.float32)])
def test_trim_edges_handles_empty_and_silent_input(audio):
    assert au.trim_edges(audio).size == 0


def test_join_accepts_one_gap_per_seam():
    seg = np.ones(SAMPLE_RATE, dtype=np.float32)
    joined = au.join([seg, seg, seg], [0.1, 0.5])
    assert abs(joined.size / SAMPLE_RATE - (3 + 0.6)) < 0.001


def test_join_still_accepts_a_single_gap():
    seg = np.ones(SAMPLE_RATE, dtype=np.float32)
    assert abs(au.join([seg, seg], 0.25).size / SAMPLE_RATE - 2.25) < 0.001
