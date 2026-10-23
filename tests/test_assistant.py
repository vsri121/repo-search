from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from farm.app import create_app
from farm.assistant import (
    AssistantBusy,
    FindingsOutput,
    ReuseAssistant,
    checked_assessments,
    extract_requirements,
    prepare_evidence,
    quoted_span,
)
from farm.config import Settings
from farm.embeddings import LocalEmbeddings
from farm.ollama import OllamaUnavailable
from farm.search import search
from farm.store import Store


@pytest.fixture
def catalog(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings.db_path)
    source = "# Document assistant\n\nA Python chatbot answers questions about PDF documents.\n\nEvery answer includes source citations."
    store.save_snapshot(
        "repo-a",
        "example/pdf-assistant",
        "",
        "a" * 40,
        {"technologies": [{"name": "Python"}], "name": "pdf-assistant"},
        [
            {
                "id": "doc-a",
                "path": "README.md",
                "title": "Document assistant",
                "body": "Repository: example/pdf-assistant. Section: Document assistant.\n"
                + source,
                "line_start": 1,
                "line_end": 5,
                "url": "https://github.com/example/pdf-assistant/blob/"
                + "a" * 40
                + "/README.md#L1-L5",
                "fingerprint": "hash-a",
            }
        ],
    )
    return settings, store, LocalEmbeddings(settings, store)


def requirements():
    return {"requirements": [{"kind": "capability", "quote": "PDF chatbot"}]}


def finding(**overrides):
    return {
        "requirement_id": "r1",
        "evidence_id": "e1",
        "explanation": "The README documents a chatbot for PDF questions.",
        **overrides,
    }


class FakeClient:
    def __init__(self, *outputs):
        self.configuration = SimpleNamespace(
            ollama_model="llama3.2:3b", ollama_timeout_seconds=60
        )
        self.outputs = list(outputs)
        self.calls = []

    def chat_json(self, messages, schema, max_tokens):
        self.calls.append(messages)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    def status(self, refresh=False):
        return {
            "available": True,
            "model_available": True,
            "model": "llama3.2:3b",
            "message": "Ready",
        }


def test_assistant_preserves_retrieval_and_checks_source_quotes(catalog):
    _, store, engine = catalog
    expected = search(store, engine, "PDF chatbot")
    client = FakeClient(requirements(), {"findings": [finding()]})
    result = ReuseAssistant(store, engine, client).analyze("PDF chatbot")
    assert result["status"] == "complete"
    assert [r["id"] for r in result["retrieval"]["results"]] == [
        r["id"] for r in expected["results"]
    ]
    citation = result["assessments"][0]["findings"][0]["citations"][0]
    assert citation["line_start"] == citation["line_end"] == 3
    assert citation["url"].endswith("#L3-L3")
    assert citation["quote"] in store.documents()[0]["body"]
    assert result["requirements"][0]["quote"] == "PDF chatbot"
    assert result["timings"]["total_ms"] >= 0


@pytest.mark.parametrize(
    "override",
    [
        {"evidence_id": "e99"},
        {"requirement_id": "r99"},
    ],
)
def test_fabricated_or_misattributed_evidence_is_omitted(catalog, override):
    _, store, engine = catalog
    assistant = ReuseAssistant(
        store, engine, FakeClient(requirements(), {"findings": [finding(**override)]})
    )
    result = assistant.analyze("PDF chatbot")
    assert result["status"] == "partial"
    assert result["discarded_items"] == 1
    result_finding = result["assessments"][0]["findings"][0]
    assert result_finding["status"] == "not_found"
    assert result_finding["citations"] == []
    assert "retrieved passages" in result_finding["explanation"]


def test_requirements_must_quote_user_and_preserve_their_words():
    client = FakeClient(
        {
            "requirements": [
                {
                    "kind": "constraint",
                    "quote": "without cloud services",
                },
                {"kind": "technology", "quote": "Java"},
            ]
        }
    )
    result, discarded = extract_requirements(
        client, "PDF chatbot without cloud services"
    )
    assert result == [
        {
            "id": "r1",
            "text": "without cloud services",
            "kind": "constraint",
            "quote": "without cloud services",
        }
    ]
    assert discarded == 1


def test_offline_model_returns_normal_results_and_releases_lock(catalog):
    _, store, engine = catalog
    assistant = ReuseAssistant(
        store, engine, FakeClient(OllamaUnavailable("Start local Ollama."))
    )
    result = assistant.analyze("PDF chatbot")
    assert result["status"] == "unavailable"
    assert result["retrieval"]["results"]
    assert result["requirements"] == result["assessments"] == []
    assert not assistant.lock.locked()


def test_invalid_generation_keeps_extracted_requirements(catalog):
    _, store, engine = catalog
    assistant = ReuseAssistant(
        store,
        engine,
        FakeClient(requirements(), {"findings": [finding(url="javascript:bad")]}),
    )
    result = assistant.analyze("PDF chatbot")
    assert result["status"] == "partial"
    assert result["requirements"]
    assert result["assessments"] == []
    assert not assistant.lock.locked()


def test_malformed_requirement_schema_never_reaches_assessment(catalog):
    _, store, engine = catalog
    client = FakeClient({"requirements": "invented"})
    result = ReuseAssistant(store, engine, client).analyze("PDF chatbot")
    assert result["status"] == "unavailable"
    assert len(client.calls) == 1


def test_empty_search_does_not_invoke_generation(catalog):
    _, store, engine = catalog
    client = FakeClient()
    assistant = ReuseAssistant(store, engine, client)
    result = assistant.analyze("zyxxyyzz")
    assert result["status"] == "no_evidence"
    assert client.calls == []


def test_concurrency_rejects_duplicate_work_without_releasing_active_lock(catalog):
    _, store, engine = catalog
    assistant = ReuseAssistant(store, engine, FakeClient())
    assistant.lock.acquire()
    try:
        with pytest.raises(AssistantBusy):
            assistant.analyze("PDF chatbot")
        assert assistant.status()["busy"] is True
    finally:
        assistant.lock.release()


def test_unexpected_failure_releases_assistant_lock(catalog):
    _, store, engine = catalog
    assistant = ReuseAssistant(store, engine, FakeClient(RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        assistant.analyze("PDF chatbot")
    assert not assistant.lock.locked()


def test_synthetic_manifest_cannot_be_used_as_source_evidence(catalog):
    _, store, engine = catalog
    with store.connect() as db:
        db.execute("UPDATE documents SET path='@manifest'")
    retrieval = search(store, engine, "PDF chatbot")
    passages, allowed = prepare_evidence(store, retrieval)
    assert not passages and not allowed
    client = FakeClient(requirements())
    result = ReuseAssistant(store, engine, client).analyze("PDF chatbot")
    assert result["status"] == "no_evidence"
    assert len(client.calls) == 1


def test_server_selected_quote_uses_original_source_and_precise_lines(catalog):
    _, store, engine = catalog
    retrieval = search(store, engine, "PDF chatbot")
    _, allowed = prepare_evidence(store, retrieval)
    proposed = FindingsOutput.model_validate({"findings": [finding()]})
    result, rejected = checked_assessments([{"id": "r1"}], retrieval, proposed, allowed)
    citation = result[0]["findings"][0]["citations"][0]
    assert rejected == 0
    assert "\n" not in citation["quote"]
    assert citation["line_start"] == 3


def test_fake_quote_cannot_be_added_by_the_model(catalog):
    _, store, engine = catalog
    client = FakeClient(
        requirements(),
        {
            "findings": [
                finding(quote="This project supports enterprise single sign-on.")
            ]
        },
    )
    result = ReuseAssistant(store, engine, client).analyze("PDF chatbot")
    assert result["status"] == "partial"
    assert not result["assessments"]


@pytest.mark.parametrize(
    "text,quote", [("JavaScript", "Java"), ("NoSQL", "SQL"), ("Pythonic", "Python")]
)
def test_partial_word_does_not_validate_an_invented_requirement(text, quote):
    assert quoted_span(text, quote) is None


def test_quote_verification_uses_original_text_with_flexible_whitespace():
    assert (
        quoted_span(
            "A Python chatbot answers\n questions", "A Python chatbot answers questions"
        ).group()
        == "A Python chatbot answers\n questions"
    )


def test_immediate_negative_and_optional_qualifiers_are_preserved():
    client = FakeClient(
        {
            "requirements": [
                {"quote": "cloud services", "kind": "capability"},
                {"quote": "Python", "kind": "technology"},
                {"quote": "Python", "kind": "capability"},
            ]
        }
    )
    extracted, discarded = extract_requirements(
        client, "PDF chatbot without cloud services and optional Python"
    )
    assert [r["text"] for r in extracted] == [
        "without cloud services",
        "optional Python",
    ]
    assert all(r["kind"] == "constraint" for r in extracted)
    assert discarded == 0


def test_table_headers_and_separators_do_not_become_evidence(catalog):
    _, store, engine = catalog
    with store.connect() as db:
        db.execute(
            "UPDATE documents SET body=?",
            (
                "Repository: example/pdf-assistant.\n| Project | Description |\n|---------|-------------|\nA Python chatbot answers questions about PDF documents.",
            ),
        )
    passages, _ = prepare_evidence(store, search(store, engine, "PDF chatbot"))
    assert passages
    assert all(
        "| Project |" not in p["quote"] and "|---" not in p["quote"] for p in passages
    )


def test_corrupt_evidence_cannot_generate_a_citation(catalog):
    _, store, engine = catalog
    retrieval = search(store, engine, "PDF chatbot")
    _, allowed = prepare_evidence(store, retrieval)
    allowed["e1"]["quote"] = "This project supports enterprise single sign-on."
    proposed = FindingsOutput.model_validate({"findings": [finding()]})
    result, rejected = checked_assessments([{"id": "r1"}], retrieval, proposed, allowed)
    assert rejected == 1
    assert result[0]["findings"][0]["citations"] == []


def test_repeated_findings_merge_source_quotes_without_false_failures(catalog):
    _, store, engine = catalog
    client = FakeClient(
        requirements(), {"findings": [finding(), finding(), finding(evidence_id="e2")]}
    )
    result = ReuseAssistant(store, engine, client).analyze("PDF chatbot")
    assert result["status"] == "complete"
    assert result["discarded_items"] == 0
    findings = result["assessments"][0]["findings"]
    assert len(findings) == 1
    assert len(findings[0]["citations"]) == 2


def test_assistant_api_input_boundary_and_busy_handling(catalog):
    settings, _, _ = catalog
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        assert client.post("/api/assistant", json={"query": "  "}).status_code == 422
        assert (
            client.post("/api/assistant", json={"query": "x" * 1201}).status_code == 422
        )
        assert (
            client.post(
                "/api/assistant",
                json={"query": "PDF"},
                headers={"Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert (
            client.post("/api/assistant", json={"query": "zyxxyyzz"}).json()["status"]
            == "no_evidence"
        )
        app.state.assistant.lock.acquire()
        try:
            response = client.post("/api/assistant", json={"query": "PDF chatbot"})
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "5"
            assert client.get("/health").json()["ok"]
            assert client.get("/api/search", params={"q": "PDF"}).json()["results"]
        finally:
            app.state.assistant.lock.release()
