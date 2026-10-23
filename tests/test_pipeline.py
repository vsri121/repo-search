from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from farm.app import create_app
from farm.config import Settings
from farm.embeddings import LocalEmbeddings
from farm.github import GitHubClient, clean_component, file_priority, parse_repository
from farm.manifest import build_documents, build_manifest
from farm.search import search
from farm.store import Store
from farm.worker import Worker, repository_id


@pytest.fixture
def configuration(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def snapshot():
    return {
        "full_name": "example/pdf-assistant",
        "commit": "a" * 40,
        "branch": "develop",
        "component": "",
        "metadata": {
            "name": "pdf-assistant",
            "description": "Answer questions about PDFs using source citations.",
            "owner": {"login": "example"},
            "topics": ["documents"],
            "license": {"spdx_id": "MIT"},
        },
        "files": {
            "README.md": "# Document assistant\n\nA Python chatbot with vector search for PDF documents and retrieval-augmented answers.\n\n## Citations\n\nEvery answer includes source citations.\n\n## Run\n\nInstall the requirements and start the documented application.\n",
            "requirements.txt": "fastapi>=0.115\noracledb\n",
        },
        "eligible_files": 2,
        "tree_files": 5,
        "bytes": 300,
    }


def save(store, configuration, snapshot):
    manifest = build_manifest(snapshot)
    documents = build_documents(snapshot, manifest, configuration)
    identifier = repository_id(snapshot["full_name"], snapshot["component"])
    store.save_snapshot(
        identifier,
        snapshot["full_name"],
        snapshot["component"],
        snapshot["commit"],
        manifest,
        documents,
    )
    return identifier, documents


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost/x/y",
        "https://github.com@evil.test/a/b",
        "https://github.com/a/b/tree/main",
        "../evil",
        "https://github.com:443/a/b",
        "https://github.com/a/b?token=secret",
    ],
)
def test_only_public_github_repository_identifiers(value):
    with pytest.raises(ValueError):
        parse_repository(value)


def test_repository_and_scope_validation():
    assert (
        parse_repository("https://github.com/oracle-livelabs/database.git/")
        == "oracle-livelabs/database"
    )
    assert clean_component("ai-vector-rag/lab1") == "ai-vector-rag/lab1"
    for value in ("../secrets", "docs/../secrets", "docs\\secret", "docs//lab"):
        with pytest.raises(ValueError):
            clean_component(value)
    assert file_priority(".env") is None
    assert file_priority("docs/private.pem") is None
    assert file_priority("node_modules/README.md") is None
    assert file_priority("lab1/setup.md") is not None


def test_manifest_claims_include_evidence_and_dependencies(snapshot):
    manifest = build_manifest(snapshot)
    assert manifest["default_branch"] == "develop"
    assert "Python" in [t["name"] for t in manifest["technologies"]]
    assert "Authentication" not in [c["name"] for c in manifest["capabilities"]]
    for capability in manifest["capabilities"]:
        assert capability["basis"] == "documented mention"
        assert snapshot["commit"] in capability["evidence"]["url"]
        assert (
            capability["evidence"]["quote"]
            in snapshot["files"][capability["evidence"]["path"]]
        )
    assert manifest["dependencies"][0]["name"].startswith("fastapi")


def test_chunks_have_accurate_line_provenance(configuration, snapshot):
    documents = build_documents(snapshot, build_manifest(snapshot), configuration)
    for document in documents:
        if document["path"] == "@manifest":
            continue
        lines = snapshot["files"][document["path"]].splitlines()
        original = "\n".join(lines[document["line_start"] - 1 : document["line_end"]])
        assert document["body"].split("\n", 1)[1] == original
        assert f"#L{document['line_start']}-L{document['line_end']}" in document["url"]


def test_refresh_removes_deleted_sections_and_fts(configuration, snapshot):
    store = Store(configuration.db_path)
    identifier, _documents = save(store, configuration, snapshot)
    snapshot["files"] = {"README.md": "# Updated\n\nKubernetes deployment guide."}
    snapshot["metadata"]["description"] = "Kubernetes deployment guide."
    snapshot["commit"] = "b" * 40
    save(store, configuration, snapshot)
    assert store.repository(identifier)["commit_sha"] == "b" * 40
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM document_fts WHERE document_fts MATCH 'citations'"
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM document_fts").fetchone()[0] == len(
            store.documents()
        )


def test_snapshot_transaction_preserves_old_index_on_failure(configuration, snapshot):
    store = Store(configuration.db_path)
    identifier, documents = save(store, configuration, snapshot)
    with pytest.raises(KeyError):
        store.save_snapshot(
            identifier,
            snapshot["full_name"],
            "",
            "b" * 40,
            build_manifest(snapshot),
            [{"id": "broken"}],
        )
    assert store.repository(identifier)["commit_sha"] == "a" * 40
    assert len(store.documents()) == len(documents)


def test_keyword_search_groups_sources_and_handles_empty_matches(
    configuration, snapshot
):
    store = Store(configuration.db_path)
    save(store, configuration, snapshot)
    engine = LocalEmbeddings(configuration, store)
    result = search(store, engine, '"PDF" citations OR *')
    assert result["mode"] == "keyword"
    assert result["total"] == 1
    assert result["results"][0]["evidence"]
    assert search(store, engine, "zzzxxyy")["total"] == 0
    assert search(store, engine, "PDF", technology="Java")["total"] == 0


def test_duplicate_active_jobs_are_coalesced(configuration):
    store = Store(configuration.db_path)
    first = store.enqueue("index", "example/repo")
    assert first == store.enqueue("index", "example/repo")
    assert store.claim_job()["id"] == first
    assert store.claim_job() is None
    store.update_job(first, "done", "completed")
    assert store.enqueue("index", "example/repo") != first


def test_failed_sync_does_not_replace_existing_snapshot(configuration, snapshot):
    store = Store(configuration.db_path)
    identifier, _ = save(store, configuration, snapshot)
    engine = LocalEmbeddings(configuration, store)
    job = {
        "id": store.enqueue("index", snapshot["full_name"]),
        "kind": "index",
        "target": snapshot["full_name"],
        "component": "",
    }
    worker = Worker(configuration, store, engine)
    with (
        patch(
            "farm.worker.GitHubClient.snapshot",
            side_effect=RuntimeError("network failure"),
        ),
        pytest.raises(RuntimeError),
    ):
        worker.process(job)
    assert store.repository(identifier)["commit_sha"] == snapshot["commit"]


def test_component_index_uses_subtree_and_retains_original_paths(configuration):
    store = Store(configuration.db_path)
    client = GitHubClient(configuration, store)

    def handler(request):
        path = request.url.path
        data = {
            "/repos/example/repo": {
                "full_name": "example/repo",
                "default_branch": "develop",
                "private": False,
            },
            "/repos/example/repo/commits/develop": {"sha": "a" * 40},
            "/repos/example/repo/git/trees/" + "a" * 40: {
                "tree": [{"path": "workshop", "type": "tree", "sha": "subtree"}]
            },
            "/repos/example/repo/git/trees/subtree": {
                "tree": [{"path": "README.md", "type": "blob", "size": 100}]
            },
        }
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(
                200,
                text="# Workshop\n\nUseful documentation for a database application.",
            )
        return httpx.Response(200, json=data[path])

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = client.snapshot("example/repo", "workshop", lambda _: None)
        assert "workshop/README.md" in result["files"]
    finally:
        client.close()


def test_api_persists_jobs_and_rejects_cross_origin_writes(configuration):
    app = create_app(configuration, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/health").json()["database"] == "sqlite"
        assert (
            client.post(
                "/api/repositories", json={"repository": "example/repo"}
            ).status_code
            == 202
        )
        assert client.get("/api/jobs").json()[0]["status"] == "queued"
        assert (
            client.post(
                "/api/repositories",
                json={"repository": "example/repo"},
                headers={"Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/repositories", json={"repository": "http://localhost/private"}
            ).status_code
            == 422
        )
        assert client.get("/api/repositories/missing").status_code == 404
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
