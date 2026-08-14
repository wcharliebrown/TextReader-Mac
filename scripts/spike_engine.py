"""Milestone 1 gate: synthesize a paragraph with Kokoro on MLX, report real-time factor.

Run:  .venv/bin/python scripts/spike_engine.py
Then: afplay /tmp/spike.wav
"""
import time
import numpy as np
import soundfile as sf

MODEL_ID = "mlx-community/Kokoro-82M-bf16"
SAMPLE_RATE = 24000
OUT = "/tmp/spike.wav"

TEXT = (
    "The quality gap between local and cloud text to speech has narrowed sharply. "
    "A model small enough to run on a laptop can now narrate a long article with "
    "natural pacing and believable emphasis, without sending a single byte over the "
    "network. For anyone who reads a lot online, that changes the economics entirely: "
    "there is no per-word cost, no monthly credit balance to watch, and no reason to "
    "think twice before listening to something."
)


def main() -> None:
    from mlx_audio.tts.utils import load_model

    t0 = time.perf_counter()
    model = load_model(MODEL_ID)
    load_s = time.perf_counter() - t0
    print(f"model load: {load_s:.2f}s")

    # Warm-up: first call pays Metal kernel compilation, so don't time it.
    t0 = time.perf_counter()
    list(model.generate(text="Warming up.", voice="af_heart", speed=1.0, lang_code="a"))
    print(f"warm-up:    {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    chunks = []
    first_chunk_s = None
    for result in model.generate(text=TEXT, voice="af_heart", speed=1.0, lang_code="a"):
        if first_chunk_s is None:
            first_chunk_s = time.perf_counter() - t0
        chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
    gen_s = time.perf_counter() - t0

    audio = np.concatenate(chunks)
    dur_s = len(audio) / SAMPLE_RATE
    sf.write(OUT, audio, SAMPLE_RATE)

    print(f"chunks:     {len(chunks)}")
    print(f"first audio:{first_chunk_s:.3f}s")
    print(f"generated:  {gen_s:.2f}s of compute for {dur_s:.2f}s of audio")
    print(f"RTF:        {gen_s / dur_s:.4f}  ({dur_s / gen_s:.1f}x faster than real-time)")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
