"""Content-addressed cache of synthesized sentences.

The key is a hash of everything that affects the audio, so a cache hit is
always safe and a re-read of the same article is instant. Concurrent requests
for the same key share one synthesis via an in-flight future.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable

from .config import AUDIO_REV, CACHE_DIR, CACHE_ENABLED, CACHE_MAX_BYTES, KEY_REGISTRY_MAX

log = logging.getLogger(__name__)

Synthesizer = Callable[[], Awaitable[bytes]]


def make_key(engine_id: str, voice: str, speed: float, text: str) -> str:
    """Content address for one rendered chunk.

    AUDIO_REV is part of the payload so that changing how audio is produced -
    trimming, gaps, anything that alters the sound of the same text - retires
    every existing entry instead of serving stale renders forever.
    """
    payload = "\x00".join([str(AUDIO_REV), engine_id, voice, f"{speed:.3f}", text])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AudioCache:
    def __init__(self, root: Path = CACHE_DIR, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.enabled = CACHE_ENABLED
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        # key -> (text, voice, speed), so /v1/audio/{key} can synthesize a
        # sentence that was prepared but evicted before it was requested.
        self._params: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        self._writes_since_prune = 0
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    # -- key registry -------------------------------------------------------

    def remember(self, key: str, text: str, voice: str, speed: float) -> None:
        self._params[key] = (text, voice, speed)
        self._params.move_to_end(key)
        while len(self._params) > KEY_REGISTRY_MAX:
            self._params.popitem(last=False)

    def params_for(self, key: str) -> tuple[str, str, float] | None:
        params = self._params.get(key)
        if params is not None:
            self._params.move_to_end(key)
        return params

    # -- storage ------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.wav"

    def get(self, key: str) -> bytes | None:
        if not self.enabled:
            return None
        path = self.path_for(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        # Touch so LRU pruning keeps what is actually being replayed.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes) -> None:
        if not self.enabled:
            return
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file so a crash never leaves a truncated WAV behind.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

        self._writes_since_prune += 1
        if self._writes_since_prune >= 200:
            self.prune()

    async def get_or_synth(self, key: str, synthesize: Synthesizer) -> bytes:
        """Return cached audio, or synthesize it once even if asked concurrently."""
        cached = self.get(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        self._inflight[key] = future
        try:
            data = await synthesize()
        except BaseException as exc:  # propagate to every waiter, then re-raise
            if not future.done():
                future.set_exception(exc)
            self._inflight.pop(key, None)
            # Nobody may await this future; stop asyncio complaining about it.
            future.exception()
            raise
        else:
            self.put(key, data)
            if not future.done():
                future.set_result(data)
            self._inflight.pop(key, None)
            return data

    # -- maintenance --------------------------------------------------------

    def stats(self) -> dict[str, int]:
        files = list(self.root.rglob("*.wav")) if self.enabled else []
        return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}

    def prune(self) -> int:
        """Evict least-recently-used entries until under the size cap."""
        self._writes_since_prune = 0
        if not self.enabled:
            return 0
        entries = []
        total = 0
        for path in self.root.rglob("*.wav"):
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, path))
            total += st.st_size
        if total <= self.max_bytes:
            return 0

        entries.sort()
        freed = 0
        for _, size, path in entries:
            if total - freed <= self.max_bytes:
                break
            try:
                path.unlink()
                freed += size
            except OSError:
                continue
        log.info("cache prune freed %.1f MB", freed / 1024**2)
        return freed
