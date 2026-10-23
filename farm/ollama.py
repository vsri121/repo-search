"""Bounded structured requests to a local Ollama instance.

The caller validates generated objects against its own Pydantic models. A JSON
schema constrains generation, but is not treated as proof of a correct answer.
"""

import json
import threading
import time
from typing import Any

import httpx

from .config import Settings

MAX_RESPONSE_BYTES = 128 * 1024
MAX_CONTENT_BYTES = 32 * 1024


class OllamaUnavailable(RuntimeError):
    """A local model cannot currently produce a usable response."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def _parse_json(value: str | bytes) -> Any:
    return json.loads(
        value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )


class OllamaClient:
    def __init__(
        self, configuration: Settings, *, transport: httpx.BaseTransport | None = None
    ):
        self.configuration = configuration
        self._transport = transport
        self._status_lock = threading.Lock()
        self._cached_status: dict | None = None
        self._status_time = 0.0

    def _request(self, endpoint: str, *, timeout: float, payload: dict | None = None):
        try:
            with httpx.Client(
                base_url=self.configuration.ollama_url,
                timeout=httpx.Timeout(timeout, connect=min(2.0, timeout)),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                method = "GET" if payload is None else "POST"
                arguments = {} if payload is None else {"json": payload}
                with client.stream(method, endpoint, **arguments) as response:
                    if response.status_code != 200:
                        if response.status_code == 404:
                            raise OllamaUnavailable(
                                "Ollama could not find the configured model or API. "
                                "Check the local model installation."
                            )
                        raise OllamaUnavailable(
                            f"Ollama rejected the request (HTTP {response.status_code})."
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise OllamaUnavailable(
                                "Ollama returned an oversized response. Try again."
                            )
                    try:
                        value = _parse_json(bytes(data))
                    except (ValueError, UnicodeError, RecursionError):
                        raise OllamaUnavailable(
                            "Ollama returned malformed JSON. Try again."
                        ) from None
                    if not isinstance(value, dict):
                        raise OllamaUnavailable(
                            "Ollama returned an invalid response. Try again."
                        )
                    return value
        except httpx.TimeoutException:
            raise OllamaUnavailable(
                "Ollama timed out. The model may still be loading; try again."
            ) from None
        except httpx.RequestError:
            raise OllamaUnavailable(
                "Ollama is unavailable. Start the local Ollama service and try again."
            ) from None

    def status(self, refresh: bool = False) -> dict:
        with self._status_lock:
            if (
                not refresh
                and self._cached_status is not None
                and time.monotonic() - self._status_time < 15
            ):
                return dict(self._cached_status)
            result = {
                "available": False,
                "model": self.configuration.ollama_model,
                "model_available": False,
                "message": "Ollama is unavailable.",
            }
            try:
                response = self._request("/api/tags", timeout=2.0)
                models = response.get("models")
                if not isinstance(models, list):
                    raise OllamaUnavailable("Ollama returned an invalid model list.")
                names = {
                    value
                    for model in models
                    if isinstance(model, dict)
                    for value in (model.get("name"), model.get("model"))
                    if isinstance(value, str)
                }
                configured = self.configuration.ollama_model
                aliases = {configured}
                if ":" not in configured.rsplit("/", 1)[-1]:
                    aliases.add(configured + ":latest")
                installed = bool(aliases.intersection(names))
                result.update(
                    available=True,
                    model_available=installed,
                    message=(
                        "Local model is ready."
                        if installed
                        else f"The configured model '{configured}' is not installed in Ollama."
                    ),
                )
            except OllamaUnavailable as exc:
                result["message"] = str(exc)
            self._cached_status = result
            self._status_time = time.monotonic()
            return dict(result)

    def chat_json(
        self, messages: list[dict], schema: dict, max_tokens: int = 1200
    ) -> dict:
        if (
            not isinstance(messages, list)
            or not 1 <= len(messages) <= 64
            or any(
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str)
                for message in messages
            )
        ):
            raise ValueError("Messages must contain only a role and text content.")
        if sum(len(message["content"]) for message in messages) > 120_000:
            raise ValueError("The local model request is too large.")
        if not isinstance(schema, dict):
            raise TypeError("A JSON object schema is required.")
        if len(json.dumps(schema, allow_nan=False)) > 32_000:
            raise ValueError("The output schema is too large.")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= 4096
        ):
            raise ValueError("max_tokens must be between 1 and 4096.")
        status = self.status()
        if not status["available"] or not status["model_available"]:
            raise OllamaUnavailable(status["message"])
        response = self._request(
            "/api/chat",
            timeout=self.configuration.ollama_timeout_seconds,
            payload={
                "model": self.configuration.ollama_model,
                "messages": messages,
                "format": schema,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": 8192,
                    "num_predict": max_tokens,
                },
            },
        )
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if (
            response.get("done") is not True
            or response.get("done_reason") == "length"
            or not isinstance(content, str)
            or len(content.encode("utf-8")) > MAX_CONTENT_BYTES
        ):
            raise OllamaUnavailable(
                "Ollama returned an incomplete or oversized answer. Try again."
            )
        try:
            answer = _parse_json(content)
        except (ValueError, UnicodeError, RecursionError):
            raise OllamaUnavailable(
                "The local model did not return valid structured JSON. Try again."
            ) from None
        if not isinstance(answer, dict):
            raise OllamaUnavailable(
                "The local model did not return a structured object. Try again."
            )
        return answer
