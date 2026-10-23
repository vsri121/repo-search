"""SQLite persistence. A repository refresh replaces its snapshot atomically."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS repositories (
                id TEXT PRIMARY KEY, full_name TEXT NOT NULL, component TEXT NOT NULL,
                commit_sha TEXT NOT NULL, manifest TEXT NOT NULL, indexed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                path TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, url TEXT NOT NULL,
                fingerprint TEXT NOT NULL, vector TEXT, model TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
                document_id UNINDEXED, repository_id UNINDEXED, title, body,
                tokenize='porter unicode61'
            );
            CREATE TABLE IF NOT EXISTS vector_cache (
                fingerprint TEXT NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL,
                PRIMARY KEY(fingerprint, model)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, component TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, repository_id TEXT
            );
            CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, etag TEXT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def repositories(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM documents d WHERE d.repository_id=r.id) AS section_count FROM repositories r ORDER BY indexed_at DESC"
            ).fetchall()
        return [{**dict(r), "manifest": json.loads(r["manifest"])} for r in rows]

    def repository(self, identifier):
        return next((r for r in self.repositories() if r["id"] == identifier), None)

    def documents(self, repository_id=None):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM documents"
                    + (" WHERE repository_id=?" if repository_id else ""),
                    (repository_id,) if repository_id else (),
                )
            ]

    def save_snapshot(
        self, identifier, full_name, component, commit, manifest, documents
    ):
        with self.connect() as db:
            db.execute("DELETE FROM document_fts WHERE repository_id=?", (identifier,))
            db.execute("DELETE FROM documents WHERE repository_id=?", (identifier,))
            db.execute(
                "INSERT INTO repositories VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET commit_sha=excluded.commit_sha, manifest=excluded.manifest, indexed_at=excluded.indexed_at",
                (identifier, full_name, component, commit, json.dumps(manifest), now()),
            )
            for d in documents:
                db.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        d["id"],
                        identifier,
                        d["path"],
                        d["title"],
                        d["body"],
                        d["line_start"],
                        d["line_end"],
                        d["url"],
                        d["fingerprint"],
                        json.dumps(d["vector"])
                        if d.get("vector") is not None
                        else None,
                        d.get("model"),
                    ),
                )
                db.execute(
                    "INSERT INTO document_fts VALUES (?,?,?,?)",
                    (d["id"], identifier, d["title"], d["body"]),
                )

    def enqueue(self, kind, target, component=""):
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM jobs WHERE kind=? AND target=? AND component=? AND status IN ('queued','running')",
                (kind, target, component),
            ).fetchone()
            if existing:
                return existing["id"]
            identifier = uuid4().hex
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,NULL)",
                (
                    identifier,
                    kind,
                    target,
                    component,
                    "queued",
                    "Waiting to start",
                    now(),
                    now(),
                ),
            )
            return identifier

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                return dict(row)

    def update_job(self, identifier, message, status="running", repository_id=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET message=?, status=?, updated_at=?, repository_id=COALESCE(?,repository_id) WHERE id=?",
                (message, status, now(), repository_id, identifier),
            )

    def jobs(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def preference(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_preference(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, value))
