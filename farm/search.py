"""Hybrid retrieval with BM25, cosine similarity, rank fusion, and repo grouping."""

import json
import re
import time

import numpy as np

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "build",
    "building",
    "by",
    "can",
    "create",
    "creating",
    "for",
    "from",
    "has",
    "have",
    "help",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "make",
    "making",
    "me",
    "my",
    "need",
    "of",
    "on",
    "or",
    "our",
    "please",
    "that",
    "the",
    "this",
    "to",
    "use",
    "using",
    "want",
    "we",
    "what",
    "which",
    "with",
    "would",
}


def terms(text):
    return list(
        dict.fromkeys(
            t for t in re.findall(r"[a-z0-9_]{2,}", text.lower()) if t not in STOP_WORDS
        )
    )[:40]


def search(store, engine, query, technology="", limit=12):
    started = time.perf_counter()
    repositories = {
        r["id"]: r
        for r in store.repositories()
        if not technology
        or technology.casefold()
        in [t["name"].casefold() for t in r["manifest"]["technologies"]]
    }
    documents = {
        d["id"]: d for d in store.documents() if d["repository_id"] in repositories
    }
    tokens = terms(query)
    lexical = []
    if tokens:
        match = " OR ".join('"' + token + '"' for token in tokens)
        with store.connect() as db:
            lexical = [
                r["document_id"]
                for r in db.execute(
                    "SELECT document_id FROM document_fts WHERE document_fts MATCH ? ORDER BY bm25(document_fts,0,0,2,1) LIMIT 200",
                    (match,),
                )
                if r["document_id"] in documents
            ]
    semantic, similarities = [], {}
    indexed = [
        d
        for d in documents.values()
        if d["model"] == engine.settings.model_name and d["vector"]
    ]
    vector = engine.embed_query(query) if engine.model is not None and indexed else None
    if vector is not None:
        matrix = np.array([json.loads(d["vector"]) for d in indexed], dtype=np.float32)
        query_vector = np.array(vector, dtype=np.float32)
        scores = (
            matrix
            @ query_vector
            / np.maximum(
                np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vector), 1e-9
            )
        )
        ordered = np.argsort(scores)[::-1][:200]
        for position in ordered:
            identifier = indexed[position]["id"]
            similarities[identifier] = float(scores[position])
            if scores[position] >= 0.52:
                semantic.append(identifier)
    lexical_ranks = {identifier: i + 1 for i, identifier in enumerate(lexical)}
    semantic_ranks = {identifier: i + 1 for i, identifier in enumerate(semantic)}
    grouped = {}
    for identifier in set(lexical) | set(semantic):
        document = documents[identifier]
        score = (
            0.4 / (60 + lexical_ranks[identifier]) if identifier in lexical_ranks else 0
        ) + (
            0.6 / (60 + semantic_ranks[identifier])
            if identifier in semantic_ranks
            else 0
        )
        matched = [token for token in tokens if token in document["body"].lower()]
        excerpt = document["body"].split("\n", 1)[-1].strip()
        # Prefer the first matching paragraph instead of cutting the evidence at random.
        paragraphs = [p.strip() for p in excerpt.split("\n\n") if p.strip()]
        if paragraphs:
            excerpt = max(paragraphs, key=lambda p: sum(t in p.lower() for t in tokens))
        proof = {
            "document_id": identifier,
            "path": document["path"],
            "title": document["title"],
            "excerpt": excerpt[:700],
            "url": document["url"],
            "line_start": document["line_start"],
            "line_end": document["line_end"],
            "matched_terms": matched,
            "semantic_similarity": round(similarities[identifier], 4)
            if identifier in similarities
            else None,
            "rank_score": score,
            "kind": "manifest" if document["path"] == "@manifest" else "source",
        }
        grouped.setdefault(document["repository_id"], []).append(proof)
    results = []
    for repository_id, evidence in grouped.items():
        evidence.sort(key=lambda item: (-item["rank_score"], item["document_id"]))
        repository = repositories[repository_id]
        all_terms = list(dict.fromkeys(t for e in evidence for t in e["matched_terms"]))
        score = evidence[0]["rank_score"]
        results.append(
            {
                **repository,
                "rank_score": round(score * 60, 4),
                "evidence": evidence[:3],
                "matched_terms": all_terms,
                "match_type": "Semantic + keyword"
                if any(e["semantic_similarity"] is not None for e in evidence)
                and all_terms
                else "Semantic"
                if any(e["semantic_similarity"] is not None for e in evidence)
                else "Keyword",
                "unverified_terms": [t for t in tokens if t not in all_terms],
            }
        )
    results.sort(key=lambda r: (-r["rank_score"], r["full_name"]))
    return {
        "results": results[:limit],
        "query": query,
        "mode": "hybrid" if vector is not None else "keyword",
        "total": len(results),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "notice": "Scores rank relevance; they do not verify feature support. Results cover selected indexed files only.",
    }
