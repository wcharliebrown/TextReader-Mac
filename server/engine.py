"""TTS engine abstraction.

Everything above this layer speaks float32 mono at SAMPLE_RATE, so swapping
Kokoro for a heavier model (Chatterbox, Orpheus) later means writing one new
class here and changing nothing else.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import DEFAULT_VOICE, MODEL_ID, SAMPLE_RATE

log = logging.getLogger(__name__)

# Kokoro voice ids encode language and gender: <lang><gender>_<name>.
_LANGS = {
    "a": ("en-US", "American English"),
    "b": ("en-GB", "British English"),
    "e": ("es", "Spanish"),
    "f": ("fr", "French"),
    "h": ("hi", "Hindi"),
    "i": ("it", "Italian"),
    "j": ("ja", "Japanese"),
    "p": ("pt-BR", "Portuguese"),
    "z": ("zh", "Mandarin"),
}

# Used when the voice directory cannot be listed for some reason.
_FALLBACK_VOICES = [
    "af_heart", "af_bella", "af_nova", "af_sarah", "af_sky", "af_alloy",
    "am_adam", "am_michael", "am_echo", "am_onyx", "am_puck", "am_fenrir",
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily",
    "bm_george", "bm_daniel", "bm_fable", "bm_lewis",
]


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    lang_code: str
    language: str
    locale: str
    gender: str
    name: str

    @classmethod
    def parse(cls, voice_id: str) -> "VoiceInfo":
        lang_code = voice_id[0]
        locale, language = _LANGS.get(lang_code, ("en-US", "English"))
        gender = {"f": "female", "m": "male"}.get(voice_id[1:2], "unknown")
        name = voice_id.split("_", 1)[-1].capitalize()
        return cls(voice_id, lang_code, language, locale, gender, name)


class TTSEngine(Protocol):
    id: str

    def synth(self, text: str, voice: str, speed: float) -> np.ndarray:
        """Return float32 mono audio at SAMPLE_RATE."""

    def voices(self) -> list[VoiceInfo]:
        ...


def lang_for_voice(voice: str) -> str:
    """Kokoro's G2P must match the voice's language; derive it, don't trust input."""
    return voice[0] if voice and voice[0] in _LANGS else "a"


class MLXKokoroEngine:
    """Kokoro-82M on Apple MLX (Metal). ~0.04 RTF on an M1."""

    id = "kokoro-mlx"

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from mlx_audio.tts.utils import load_model

        self.model_id = model_id
        self._model = load_model(model_id)
        self._voices = self._discover_voices(model_id)

    @staticmethod
    def _discover_voices(model_id: str) -> list[VoiceInfo]:
        try:
            from huggingface_hub import snapshot_download

            root = Path(snapshot_download(model_id, allow_patterns=["voices/*"]))
            ids = sorted(p.stem for p in (root / "voices").glob("*.safetensors"))
        except Exception:  # noqa: BLE001 - offline or layout change; degrade gracefully
            log.warning("could not enumerate voices, using built-in list", exc_info=True)
            ids = list(_FALLBACK_VOICES)
        return [VoiceInfo.parse(v) for v in ids]

    def synth(self, text: str, voice: str, speed: float) -> np.ndarray:
        parts = [
            np.asarray(r.audio, dtype=np.float32).reshape(-1)
            for r in self._model.generate(
                text=text, voice=voice, speed=speed, lang_code=lang_for_voice(voice)
            )
        ]
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def voices(self) -> list[VoiceInfo]:
        return list(self._voices)


class TorchKokoroEngine:
    """PyTorch/MPS fallback via the reference `kokoro` package."""

    id = "kokoro-torch"

    def __init__(self, model_id: str = "hexgrad/Kokoro-82M") -> None:
        import os

        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        from kokoro import KPipeline

        self.model_id = model_id
        self._KPipeline = KPipeline
        self._pipelines: dict[str, object] = {}
        self._voices = [VoiceInfo.parse(v) for v in _FALLBACK_VOICES]

    def _pipeline(self, lang_code: str):
        if lang_code not in self._pipelines:
            self._pipelines[lang_code] = self._KPipeline(lang_code=lang_code)
        return self._pipelines[lang_code]

    def synth(self, text: str, voice: str, speed: float) -> np.ndarray:
        pipeline = self._pipeline(lang_for_voice(voice))
        parts = [
            np.asarray(audio, dtype=np.float32).reshape(-1)
            for _, _, audio in pipeline(text, voice=voice, speed=speed)
        ]
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def voices(self) -> list[VoiceInfo]:
        return list(self._voices)


def create_engine(name: str) -> TTSEngine:
    engine: TTSEngine = MLXKokoroEngine() if name == "mlx" else TorchKokoroEngine()
    # First call compiles Metal kernels and builds the G2P pipeline; pay it now
    # rather than on the user's first sentence.
    engine.synth("Ready.", DEFAULT_VOICE, 1.0)
    log.info("engine %s ready (%d voices, %d Hz)", engine.id, len(engine.voices()), SAMPLE_RATE)
    return engine
