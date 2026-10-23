"""One durable SQLite-backed job worker for the single-process local app."""

import json
import logging
import subprocess
from threading import Event, Thread

from .embeddings import fingerprint
from .github import GitHubClient
from .manifest import GENERATOR, build_documents, build_manifest

logger = logging.getLogger(__name__)


def repository_id(name, component=""):
    return fingerprint(name.casefold() + "/" + component)[:20]


class Worker:
    def __init__(self, settings, store, engine):
        self.settings, self.store, self.engine = settings, store, engine
        self.stop = Event()
        self.thread = None

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='queued', message='Resuming after application restart' WHERE status='running'"
            )
        if self.store.preference("model_enabled") == "true":
            self.store.enqueue("model", self.settings.model_name)
        self.thread = Thread(target=self.run, name="repository-indexer", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

    def backfill(self):
        documents = self.store.documents()
        self.engine.embed_documents(documents)
        with self.store.connect() as db:
            for d in documents:
                if d.get("vector") is not None:
                    vector = (
                        d["vector"]
                        if isinstance(d["vector"], str)
                        else json.dumps(d["vector"])
                    )
                    db.execute(
                        "UPDATE documents SET vector=?,model=? WHERE id=?",
                        (vector, self.settings.model_name, d["id"]),
                    )

    def process(self, job):
        progress = lambda message: self.store.update_job(job["id"], message)
        if job["kind"] == "model":
            progress(
                "Downloading or loading the local model; first setup can take a few minutes"
            )
            self.engine.prepare()
            progress("Embedding indexed documentation locally")
            self.backfill()
            self.store.update_job(
                job["id"], "Local semantic search is ready", "completed"
            )
            return
        if job["kind"] == "clone":
            repository = self.store.repository(job["target"])
            if not repository:
                raise ValueError("Indexed repository was not found.")
            base = self.settings.data_dir / "imports"
            base.mkdir(parents=True, exist_ok=True)
            target = base / (
                repository["full_name"].replace("/", "--") + "--" + job["id"][:8]
            )
            target.mkdir(exist_ok=False)
            manifest = repository["manifest"]
            progress("Cloning the indexed revision; source code will not be executed")
            for args in (
                ["init", str(target)],
                [
                    "-C",
                    str(target),
                    "remote",
                    "add",
                    "origin",
                    manifest["repository_url"] + ".git",
                ],
                [
                    "-C",
                    str(target),
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    repository["commit_sha"],
                ],
                ["-C", str(target), "checkout", "--detach", "FETCH_HEAD"],
            ):
                result = subprocess.run(
                    ["git", *args],
                    capture_output=True,
                    timeout=120,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(
                        "Git clone failed. Check Git and network access. A partial checkout may remain in data/imports."
                    )
            self.store.update_job(
                job["id"], "Cloned to " + str(target), "completed", repository["id"]
            )
            return
        identifier = repository_id(job["target"], job["component"])
        previous = self.store.repository(identifier)
        current_commit = (
            previous["commit_sha"]
            if previous and previous["manifest"]["generator"] == GENERATOR
            else None
        )
        client = GitHubClient(self.settings, self.store)
        try:
            snapshot = client.snapshot(
                job["target"], job["component"], progress, current_commit
            )
        finally:
            client.close()
        if snapshot.get("unchanged"):
            self.store.update_job(
                job["id"],
                "Already up to date at " + snapshot["commit"][:8],
                "completed",
                identifier,
            )
            return
        identifier = repository_id(snapshot["full_name"], job["component"])
        progress("Extracting an evidence-backed manifest and documentation sections")
        manifest = build_manifest(snapshot)
        documents = build_documents(snapshot, manifest, self.settings)
        progress(
            "Creating local embeddings"
            if self.engine.model is not None
            else "Building the keyword index; semantic model is not loaded"
        )
        self.engine.embed_documents(documents)
        self.store.save_snapshot(
            identifier,
            snapshot["full_name"],
            job["component"],
            snapshot["commit"],
            manifest,
            documents,
        )
        self.store.update_job(
            job["id"],
            f"Indexed {len(snapshot['files'])} files and {len(documents)} sections at {snapshot['commit'][:8]}",
            "completed",
            identifier,
        )

    def run(self):
        while not self.stop.is_set():
            job = self.store.claim_job()
            if not job:
                self.stop.wait(0.5)
                continue
            try:
                self.process(job)
            except Exception as error:
                logger.exception("Job %s failed", job["id"])
                message = (
                    str(error)
                    if isinstance(error, (ValueError, RuntimeError))
                    else f"{type(error).__name__}: operation failed. Check the local server log and retry."
                )
                self.store.update_job(job["id"], message[:700], "failed")
