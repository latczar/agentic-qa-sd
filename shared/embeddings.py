"""Turning text into vectors.

Two implementations behind one interface: the real Ollama one, and a fake that
needs no model running. Tests use the fake so the whole suite stays fast and
works in CI, where there is no Ollama - the same pattern as the replay provider
in the sibling ai-auto project.
"""

import hashlib
import json
import math
import random
import urllib.error
import urllib.request
from typing import Protocol

from shared.config import EMBEDDING_MODEL, EMBEDDING_PROVIDER, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL
from shared.models import EMBEDDING_DIMENSIONS


class EmbeddingError(RuntimeError):
    """Raised when text could not be turned into a usable vector.

    Deliberately one error type for every cause - unreachable Ollama, HTTP
    error, unparseable body, wrong number of dimensions. Callers only ever need
    to decide "did this work or not", and a caller that can't act differently on
    the difference shouldn't be made to catch four exception types.
    """


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class OllamaEmbeddingProvider:
    """Calls a locally running Ollama. No API key, no network egress, no cost."""

    def __init__(
        self,
        base_url: str = OLLAMA_URL,
        model: str = EMBEDDING_MODEL,
        timeout: float = OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "prompt": text}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise EmbeddingError(
                f"Ollama returned HTTP {exc.code} for model '{self.model}'. "
                f"Is the model pulled? Try: ollama pull {self.model}"
            ) from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(
                f"Could not reach Ollama at {self.base_url}: {exc.reason}. Is it running?"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError(f"Ollama returned a response that wasn't JSON: {exc}") from exc

        vector = body.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError(f"Ollama response had no usable 'embedding' field: {body!r}")

        # Checked here rather than left to Postgres. A wrong-width vector is a
        # configuration mistake (wrong model in EMBEDDING_MODEL), and saying so
        # beats a database error about vector dimensions three layers away.
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"Model '{self.model}' returned {len(vector)} dimensions, but the "
                f"database column expects {EMBEDDING_DIMENSIONS}. Wrong embedding model?"
            )

        return [float(value) for value in vector]


class FakeEmbeddingProvider:
    """Deterministic stand-in for Ollama: same text always gives the same vector.

    These vectors carry no meaning - similar text does NOT land near other
    similar text. That's fine, because what the tests around ingestion actually
    check is the plumbing: did a row get written, was the embedding skipped when
    the content hash matched, was a failure handled. Judging retrieval *quality*
    needs the real model, and that belongs in the Phase 11 evaluation suite.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        # Seed a PRNG from the text so the output is stable across processes and
        # machines. Python's built-in hash() is salted per process and would give
        # a different vector on every run.
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rng = random.Random(seed)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]

        # Normalise to unit length, like real embedding models do, so anything
        # downstream that assumes that holds true for the fake as well.
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]


def get_embedding_provider(name: str = EMBEDDING_PROVIDER) -> EmbeddingProvider:
    """Pick a provider by name, so switching is an env var and not a code change."""
    if name == "ollama":
        return OllamaEmbeddingProvider()
    if name == "fake":
        return FakeEmbeddingProvider()
    raise ValueError(f"Unknown EMBEDDING_PROVIDER '{name}' - expected 'ollama' or 'fake'")
