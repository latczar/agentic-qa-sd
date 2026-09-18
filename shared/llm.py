"""Talking to the generation model.

Same shape as shared/embeddings.py, on purpose: a real Ollama provider and a
fake that needs no model running, behind one interface. Tests script exactly
what the fake returns - malformed JSON, a schema violation, a good response -
so the validation/retry logic gets exercised without ever calling a real
model. Same pattern as the replay provider in the sibling ai-auto project.
"""

import json
import urllib.error
import urllib.request
from typing import Protocol

from shared.config import GENERATION_MODEL, LLM_PROVIDER, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL


class LLMError(RuntimeError):
    """Raised when the model could not be reached or returned nothing usable."""


class LLMProvider(Protocol):
    def generate(self, prompt: str) -> str: ...


class OllamaLLMProvider:
    """Calls a locally running Ollama, requesting JSON-formatted output.

    `format: "json"` makes Ollama constrain its output to syntactically valid
    JSON. That's not the same as matching *our* schema - a model can return
    valid JSON with the wrong fields - so callers still validate against
    TicketAnalysis. But it does rule out the model wrapping its answer in a
    sentence like "Sure, here's the JSON: {...}", which is worth eliminating
    for free before validation even runs.
    """

    def __init__(
        self,
        base_url: str = OLLAMA_URL,
        model: str = GENERATION_MODEL,
        timeout: float = OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "format": "json", "stream": False}
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LLMError(
                f"Ollama returned HTTP {exc.code} for model '{self.model}'. "
                f"Is the model pulled? Try: ollama pull {self.model}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"Could not reach Ollama at {self.base_url}: {exc.reason}. Is it running?"
            ) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ollama returned a response that wasn't JSON: {exc}") from exc

        text = body.get("response")
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"Ollama response had no usable 'response' field: {body!r}")

        return text


class FakeLLMProvider:
    """Returns pre-scripted responses in order, one per call.

    Not "deterministic from the input" like FakeEmbeddingProvider - a test
    needs to control the exact text back (valid JSON, malformed JSON, JSON
    with an extra field) to exercise a specific validation path, so it scripts
    the response list itself rather than deriving it from the prompt.
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses) if responses is not None else []
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise LLMError("FakeLLMProvider has no scripted responses left")
        return self._responses.pop(0)


def get_llm_provider(name: str = LLM_PROVIDER) -> LLMProvider:
    """Pick a provider by name, so switching is an env var and not a code change."""
    if name == "ollama":
        return OllamaLLMProvider()
    if name == "fake":
        return FakeLLMProvider()
    raise ValueError(f"Unknown LLM_PROVIDER '{name}' - expected 'ollama' or 'fake'")
