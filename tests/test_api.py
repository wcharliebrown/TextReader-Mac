"""API-level tests against a stub engine.

Running the real model here would make the suite slow and machine-dependent, so
a fake engine stands in. It is deliberately slow enough that background
rendering is still in flight when cancellation is tested.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server.engine import VoiceInfo

VOICES = ["af_heart", "af_bella", "bm_george"]


class FakeEngine:
    id = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def synth(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.calls.append((text, voice, speed))
        time.sleep(0.05)          # slow enough for cancellation to be observable
        return np.zeros(2400, dtype=np.float32)

    def voices(self) -> list[VoiceInfo]:
        return [VoiceInfo.parse(v) for v in VOICES]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    engine = FakeEngine()
    monkeypatch.setattr(app_module, "create_engine", lambda _name: engine)
    monkeypatch.setattr(app_module.cache, "root", tmp_path)
    monkeypatch.setattr(app_module.cache, "enabled", True)
    with TestClient(app_module.app) as c:
        c.engine = engine
        yield c


# Every sentence is distinct: repeated text would legitimately collapse to one
# cache key and make the chunk count depend on deduplication rather than length.
LONG_TEXT = "\n\n".join(
    f"Sentence number {i} in an article long enough that rendering takes a while. "
    f"Clause {i} carries the paragraph on so the chunk is a realistic length."
    for i in range(1, 25)
)


def test_healthz_reports_ready(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["engine"] == "fake"


def test_voices_listed_with_metadata(client):
    body = client.get("/v1/voices").json()
    assert [v["id"] for v in body["voices"]] == VOICES
    assert body["default"] == "af_heart"
    george = next(v for v in body["voices"] if v["id"] == "bm_george")
    assert george["gender"] == "male" and george["locale"] == "en-GB"


def test_prepare_returns_ordered_chunks_with_keys(client):
    body = client.post("/v1/speak/prepare", json={"text": LONG_TEXT}).json()
    keys = [s["key"] for s in body["sentences"]]
    assert [s["idx"] for s in body["sentences"]] == list(range(len(keys)))
    assert len(set(keys)) == len(keys), "distinct sentences must hash distinctly"


def test_identical_sentences_share_a_key(client):
    """Content addressing: a repeated sentence is rendered once, not twice."""
    repeated = "The very same sentence appears twice in this passage. "
    body = client.post(
        "/v1/speak/prepare",
        json={"text": repeated + "A different sentence separates them here. " + repeated},
    ).json()
    keys = [s["key"] for s in body["sentences"]]
    assert len(keys) > len(set(keys)), "repeated text should reuse a cache key"


def test_voice_and_speed_change_the_key(client):
    text = "One consistent sentence used for every one of these requests."
    base = client.post("/v1/speak/prepare", json={"text": text}).json()["sentences"][0]["key"]
    other_voice = client.post(
        "/v1/speak/prepare", json={"text": text, "voice": "bm_george"}
    ).json()["sentences"][0]["key"]
    other_speed = client.post(
        "/v1/speak/prepare", json={"text": text, "speed": 1.4}
    ).json()["sentences"][0]["key"]
    assert len({base, other_voice, other_speed}) == 3


def test_audio_is_served_for_a_prepared_key(client):
    job = client.post("/v1/speak/prepare", json={"text": "A short but adequate test sentence here."}).json()
    res = client.get(f"/v1/audio/{job['sentences'][0]['key']}.wav")
    assert res.status_code == 200
    assert res.content[:4] == b"RIFF"


def test_unknown_key_is_404(client):
    assert client.get("/v1/audio/" + "0" * 64 + ".wav").status_code == 404


def test_unknown_voice_is_rejected(client):
    res = client.post("/v1/speak/prepare", json={"text": "hello there", "voice": "zz_nobody"})
    assert res.status_code == 400


def test_openai_endpoint_returns_audio(client):
    res = client.post("/v1/audio/speech", json={"input": "Hello there.", "response_format": "wav"})
    assert res.status_code == 200
    assert res.content[:4] == b"RIFF"


# -- cancellation -----------------------------------------------------------

def test_cancel_stops_background_rendering(client):
    job = client.post("/v1/speak/prepare", json={"text": LONG_TEXT}).json()
    time.sleep(0.2)                      # let the background pass get going
    res = client.post("/v1/speak/cancel", json={"job_id": job["job_id"]})
    assert res.json()["cancelled"] is True

    rendered = len(client.engine.calls)
    time.sleep(0.4)
    assert len(client.engine.calls) <= rendered + 1, "rendering continued after cancel"
    assert rendered < len(job["sentences"]), "test is not exercising an in-flight job"


def test_cancel_is_idempotent(client):
    job = client.post("/v1/speak/prepare", json={"text": LONG_TEXT}).json()
    client.post("/v1/speak/cancel", json={"job_id": job["job_id"]})
    again = client.post("/v1/speak/cancel", json={"job_id": job["job_id"]}).json()
    assert again["cancelled"] is False
    assert again["reason"] == "nothing running"


def test_cancel_of_unknown_job_is_reported(client):
    body = client.post("/v1/speak/cancel", json={"job_id": "does-not-exist"}).json()
    assert body == {"cancelled": False, "reason": "unknown job_id"}


def test_cancelling_one_job_does_not_touch_a_later_one(client):
    first = client.post("/v1/speak/prepare", json={"text": LONG_TEXT}).json()
    second = client.post("/v1/speak/prepare", json={"text": LONG_TEXT + " Extra tail sentence."}).json()
    # Preparing the second already cancelled the first; cancelling the stale job
    # must not stop the one the listener actually switched to.
    client.post("/v1/speak/cancel", json={"job_id": first["job_id"]})
    res = client.get(f"/v1/audio/{second['sentences'][0]['key']}.wav")
    assert res.status_code == 200
