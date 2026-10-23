"""Real local embeddings; unavailable models never become simulated vectors."""

import json
import os
from hashlib import sha256
from threading import Lock


def fingerprint(text):
    return sha256(text.encode("utf-8")).hexdigest()


class LocalEmbeddings:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.model = None
        self.error = None
        self.loading = False
        self.lock = Lock()

    def status(self):
        return {
            "ready": self.model is not None,
            "loading": self.loading,
            "model": self.settings.model_name,
            "dimensions": 384,
            "error": self.error,
            "provider": "local",
        }

    def prepare(self):
        with self.lock:
            if self.model is not None:
                return
            self.loading = True
            self.error = None
            try:
                directory = self.settings.data_dir / "models"
                directory.mkdir(parents=True, exist_ok=True)
                os.environ.setdefault("HF_HOME", str(directory / "huggingface"))
                os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                from fastembed import TextEmbedding

                self.model = TextEmbedding(
                    self.settings.model_name,
                    cache_dir=str(directory),
                    threads=2,
                    providers=["CPUExecutionProvider"],
                )
                list(self.model.query_embed("model readiness check"))
                self.store.set_preference("model_enabled", "true")
            except Exception:
                self.model = None
                self.error = "Could not load the local model. Check the network connection and retry model setup."
                raise
            finally:
                self.loading = False

    def embed_documents(self, documents):
        if self.model is None:
            return
        pending = []
        with self.store.connect() as db:
            for d in documents:
                row = db.execute(
                    "SELECT vector FROM vector_cache WHERE fingerprint=? AND model=?",
                    (d["fingerprint"], self.settings.model_name),
                ).fetchone()
                if row:
                    d.update(vector=json.loads(row[0]), model=self.settings.model_name)
                else:
                    pending.append(d)
        if pending:
            with self.lock:
                vectors = list(
                    self.model.passage_embed(
                        [d["body"] for d in pending], batch_size=16
                    )
                )
            with self.store.connect() as db:
                for document, vector in zip(pending, vectors, strict=True):
                    values = [float(v) for v in vector]
                    document.update(vector=values, model=self.settings.model_name)
                    db.execute(
                        "INSERT OR REPLACE INTO vector_cache VALUES (?,?,?)",
                        (
                            document["fingerprint"],
                            self.settings.model_name,
                            json.dumps(values),
                        ),
                    )

    def embed_query(self, text):
        if self.model is None:
            return None
        with self.lock:
            return next(iter(self.model.query_embed(text)))
