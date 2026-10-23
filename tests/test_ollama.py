import json

import httpx
import pytest

from farm.config import Settings
from farm.ollama import (
    MAX_CONTENT_BYTES,
    MAX_RESPONSE_BYTES,
    OllamaClient,
    OllamaUnavailable,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1.example.com:11434",
        "http://127.0.0.1@evil.test:11434",
        "http://username:password@localhost:11434",
        "http://localhost:11434/api",
        "http://localhost:11434?key=secret",
        "http://localhost:11434#fragment",
        "http://localhost:0",
        "http://localhost:70000",
        "http://localhost:hello",
        "file://localhost/model",
        "http://[::ffff:192.168.1.1]:11434",
        "http://[::1%25eth0]:11434",
        "http://localhost\\evil.test",
        "http://local host:11434",
    ],
)
def test_settings_reject_nonlocal_and_ambiguous_ollama_urls(url):
    with pytest.raises(ValueError, match="FARM_OLLAMA_URL"):
        Settings(ollama_url=url)


@pytest.mark.parametrize(
    "url", ["http://localhost:11434", "http://127.0.0.1:11434/", "https://[::1]:11434"]
)
def test_settings_accept_only_explicit_local_service_urls(url):
    assert Settings(ollama_url=url).ollama_url == url.rstrip("/")


@pytest.mark.parametrize("timeout", [0, 181, float("inf"), float("nan"), True, "60"])
def test_settings_bound_ollama_timeout(timeout):
    with pytest.raises(ValueError, match="FARM_OLLAMA_TIMEOUT_SECONDS"):
        Settings(ollama_timeout_seconds=timeout)


def test_environment_ollama_settings(monkeypatch):
    monkeypatch.setenv("FARM_OLLAMA_URL", "http://[::1]:11434/")
    monkeypatch.setenv("FARM_OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("FARM_OLLAMA_TIMEOUT_SECONDS", "45")
    settings = Settings()
    assert settings.ollama_url == "http://[::1]:11434"
    assert settings.ollama_timeout_seconds == 45


def make_client(handler, **settings):
    return OllamaClient(Settings(**settings), transport=httpx.MockTransport(handler))


def tags_response(model="llama3.2:3b"):
    return httpx.Response(200, json={"models": [{"name": model}]})


def test_status_cache_returns_copies_and_refreshes():
    requests = []

    def handler(request):
        requests.append(request)
        return tags_response()

    client = make_client(handler)
    first = client.status()
    assert first["available"] and first["model_available"]
    first["available"] = False
    assert client.status()["available"]
    assert len(requests) == 1
    client.status(refresh=True)
    assert len(requests) == 2
    assert requests[0].url.path == "/api/tags"


def test_model_availability_does_not_match_unrelated_tags():
    client = make_client(lambda _: tags_response("llama3.2:1b"))
    status = client.status()
    assert status["available"] is True
    assert status["model_available"] is False
    with pytest.raises(OllamaUnavailable, match="not installed"):
        client.chat_json([{"role": "user", "content": "hello"}], {})
    latest = make_client(
        lambda _: tags_response("embeddinggemma:latest"), ollama_model="embeddinggemma"
    )
    assert latest.status()["model_available"]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_connection_failures_are_clear_and_do_not_leak_request_content(error):
    def handler(request):
        raise error("secret repository snippet", request=request)

    client = make_client(handler)
    status = client.status()
    assert status["available"] is False
    assert "secret" not in status["message"]
    with pytest.raises(OllamaUnavailable) as caught:
        client.chat_json([{"role": "user", "content": "secret"}], {})
    assert "secret" not in str(caught.value)


def test_json_request_schema_options_and_http_safety(monkeypatch):
    requests = []
    client_options = []
    original_client = httpx.Client

    def inspect_client(**kwargs):
        client_options.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", inspect_client)

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return tags_response()
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": '{"language":"Python"}'},
                "done": True,
            },
        )

    client = make_client(handler, ollama_timeout_seconds=45)
    schema = {
        "type": "object",
        "properties": {"language": {"type": "string"}},
        "required": ["language"],
        "additionalProperties": False,
    }
    messages = [{"role": "user", "content": "Extract the language from Python."}]
    assert client.chat_json(messages, schema, max_tokens=120) == {"language": "Python"}
    payload = json.loads(requests[-1].content)
    assert payload["format"] == schema
    assert payload["messages"] == messages
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 120}
    assert "tools" not in payload
    assert all(options["trust_env"] is False for options in client_options)
    assert all(options["follow_redirects"] is False for options in client_options)
    assert client_options[-1]["timeout"].read == 45
    assert client_options[0]["timeout"].read == 2


def test_redirects_are_never_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.test/collect"})

    client = make_client(handler)
    status = client.status()
    assert not status["available"]
    assert "HTTP 307" in status["message"]
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"


@pytest.mark.parametrize(
    "response",
    [
        {"message": {"content": "not json"}, "done": True},
        {"message": {"content": "[]"}, "done": True},
        {"message": {"content": '{"x":NaN}'}, "done": True},
        {"message": {"content": '{"x":1,"x":2}'}, "done": True},
        {"message": {"content": "{}"}, "done": False},
        {"message": {"content": "{}"}, "done": True, "done_reason": "length"},
        {"message": {"content": "x" * (MAX_CONTENT_BYTES + 1)}, "done": True},
        {"error": "secret repo content"},
    ],
)
def test_invalid_generated_answers_are_rejected(response):
    def handler(request):
        return (
            tags_response()
            if request.url.path == "/api/tags"
            else httpx.Response(200, json=response)
        )

    client = make_client(handler)
    with pytest.raises(OllamaUnavailable) as caught:
        client.chat_json([{"role": "user", "content": "hello"}], {})
    assert "secret" not in str(caught.value)


def test_response_body_size_is_bounded():
    client = make_client(
        lambda _: httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
    )
    assert "oversized" in client.status()["message"]


def test_caller_cannot_send_tools_or_unbounded_generation():
    client = make_client(lambda _: tags_response())
    with pytest.raises(ValueError, match="role and text"):
        client.chat_json([{"role": "tool", "content": "execute me"}], {})
    with pytest.raises(ValueError, match="too large"):
        client.chat_json([{"role": "user", "content": "a" * 120_001}], {})
    with pytest.raises(ValueError, match="max_tokens"):
        client.chat_json([{"role": "user", "content": "hello"}], {}, max_tokens=100_000)
