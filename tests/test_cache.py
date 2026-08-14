"""The cache must never return audio that was rendered with different settings."""
from __future__ import annotations

import asyncio

import pytest

from server.cache import AudioCache, make_key


@pytest.fixture()
def cache(tmp_path):
    c = AudioCache(root=tmp_path, max_bytes=10_000)
    c.enabled = True
    return c


def test_key_depends_on_every_input():
    base = make_key("eng", "af_heart", 1.0, "hello")
    assert base != make_key("eng", "af_bella", 1.0, "hello")
    assert base != make_key("eng", "af_heart", 1.5, "hello")
    assert base != make_key("eng", "af_heart", 1.0, "hello!")
    assert base != make_key("other", "af_heart", 1.0, "hello")


def test_key_is_stable():
    assert make_key("eng", "v", 1.0, "x") == make_key("eng", "v", 1.0, "x")


def test_put_then_get_roundtrip(cache):
    cache.put("abc123", b"payload")
    assert cache.get("abc123") == b"payload"


def test_get_missing_returns_none(cache):
    assert cache.get("nothere") is None


def test_disabled_cache_never_stores(tmp_path):
    c = AudioCache(root=tmp_path, max_bytes=1000)
    c.enabled = False
    c.put("k", b"data")
    assert c.get("k") is None


@pytest.mark.asyncio
async def test_concurrent_requests_synthesize_once(cache):
    calls = 0

    async def synth():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return b"audio"

    results = await asyncio.gather(*(cache.get_or_synth("dup", synth) for _ in range(5)))
    assert results == [b"audio"] * 5
    assert calls == 1, "in-flight dedup should collapse concurrent requests"


@pytest.mark.asyncio
async def test_failed_synthesis_is_not_cached(cache):
    async def boom():
        raise RuntimeError("engine died")

    with pytest.raises(RuntimeError):
        await cache.get_or_synth("bad", boom)
    assert cache.get("bad") is None

    async def ok():
        return b"recovered"

    assert await cache.get_or_synth("bad", ok) == b"recovered"


def test_prune_evicts_until_under_cap(cache):
    for i in range(20):
        cache.put(f"{i:064d}", b"x" * 1000)
    assert cache.stats()["bytes"] > cache.max_bytes
    cache.prune()
    assert cache.stats()["bytes"] <= cache.max_bytes


def test_params_registry_roundtrip(cache):
    cache.remember("k1", "some text", "af_heart", 1.0)
    assert cache.params_for("k1") == ("some text", "af_heart", 1.0)
    assert cache.params_for("missing") is None


def test_audio_revision_is_part_of_the_key(monkeypatch):
    """Changing how audio is produced must retire existing renders."""
    import server.cache as cache_module

    before = cache_module.make_key("kokoro-mlx", "af_heart", 1.0, "Hello there.")
    monkeypatch.setattr(cache_module, "AUDIO_REV", cache_module.AUDIO_REV + 1)
    after = cache_module.make_key("kokoro-mlx", "af_heart", 1.0, "Hello there.")
    assert before != after, "a pipeline change would have served stale audio"
