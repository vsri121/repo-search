"""Evidence-based extraction. Documentation mentions are not execution claims."""

import json
import re
import tomllib
from pathlib import PurePosixPath
from urllib.parse import quote

from .embeddings import fingerprint

GENERATOR = "evidence-rules/1"
TECHNOLOGIES = {
    "Python": r"\bpython\b",
    "JavaScript": r"\bjavascript\b",
    "TypeScript": r"\btypescript\b",
    "Java": r"\bjava\b",
    "Go": r"\bgolang\b",
    "React": r"\breact(?:\.js)?\b",
    "FastAPI": r"\bfastapi\b",
    "Spring": r"\bspring(?: boot)?\b",
    "Node.js": r"\bnode(?:\.js|js)\b",
    "Oracle Database": r"\boracle (?:database|db|autonomous)|\boracledb\b|\bautonomous database\b",
    "Oracle Cloud": r"\boci\b|\boracle cloud\b",
    "Docker": r"\bdocker\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "LangChain": r"\blangchain\b",
    "Streamlit": r"\bstreamlit\b",
    "Redis": r"\bredis\b",
}
CAPABILITIES = {
    "Document processing": r"\bpdf\b|document (?:processing|extraction|ingestion)",
    "Question answering": r"question.answer|\bchatbot\b|retrieval.augmented|\brag\b",
    "Vector search": r"vector (?:search|database|store)|semantic search",
    "Authentication": r"\bauthentication\b|\boauth\b|single sign.on",
    "Data pipelines": r"data pipeline|\betl\b|data ingestion",
    "Application deployment": r"deploy(?:ment|ing)?|infrastructure.as.code",
    "Machine learning": r"machine learning|model training|\bpredictive\b",
    "Analytics": r"\banalytics\b|business intelligence|data visualization",
    "API integration": r"\brest api\b|\bgraphql\b|api integration",
    "Notifications": r"\bnotifications\b|email alerts|slack integration",
}


def source_url(snapshot, path, start=1, end=1):
    return f"https://github.com/{snapshot['full_name']}/blob/{snapshot['commit']}/{quote(path, safe='/')}#L{start}-L{end}"


def evidence_for(snapshot, pattern):
    for path, text in snapshot["files"].items():
        for index, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line, re.IGNORECASE) and not line.lstrip().startswith(
                ("![", "[![")
            ):
                return {
                    "path": path,
                    "line": index,
                    "quote": line.strip()[:400],
                    "url": source_url(snapshot, path, index, index),
                }
    return None


def build_manifest(snapshot):
    metadata = snapshot["metadata"]
    technologies, capabilities, dependencies = [], [], []
    for name, pattern in TECHNOLOGIES.items():
        if proof := evidence_for(snapshot, pattern):
            technologies.append({"name": name, "basis": "mentioned", "evidence": proof})
    for path, text in snapshot["files"].items():
        name = PurePosixPath(path).name.lower()
        try:
            names = []
            if name == "package.json":
                parsed = json.loads(text)
                names = list(parsed.get("dependencies", {})) + list(
                    parsed.get("devDependencies", {})
                )
                language = "Node.js"
            elif name == "pyproject.toml":
                names = tomllib.loads(text).get("project", {}).get("dependencies", [])
                language = "Python"
            elif name == "requirements.txt":
                names = [
                    v.strip()
                    for v in text.splitlines()
                    if v.strip() and not v.lstrip().startswith(("#", "-"))
                ]
                language = "Python"
            else:
                continue
            dependencies.extend(
                {"name": str(v)[:160], "path": path, "url": source_url(snapshot, path)}
                for v in names[:60]
            )
            if language not in [t["name"] for t in technologies]:
                technologies.append(
                    {
                        "name": language,
                        "basis": "dependency file",
                        "evidence": {
                            "path": path,
                            "line": 1,
                            "quote": f"Declared in {name}",
                            "url": source_url(snapshot, path),
                        },
                    }
                )
        except (ValueError, TypeError, AttributeError):
            continue
    # Capability detection uses documentation, not dependency names or filename guesses.
    docs_snapshot = {
        **snapshot,
        "files": {
            p: t
            for p, t in snapshot["files"].items()
            if p.lower().endswith((".md", ".rst", ".txt"))
            and PurePosixPath(p).name.lower() != "requirements.txt"
        },
    }
    for name, pattern in CAPABILITIES.items():
        if proof := evidence_for(docs_snapshot, pattern):
            capabilities.append(
                {"name": name, "basis": "documented mention", "evidence": proof}
            )
    description = metadata.get("description") or ""
    if snapshot["component"] or not description:
        for path, text in docs_snapshot["files"].items():
            if (
                PurePosixPath(path).name.lower().startswith("readme")
                or snapshot["component"]
            ):
                paragraphs = [
                    re.sub(r"\s+", " ", p).strip()
                    for p in text.split("\n\n")
                    if len(p.strip()) > 65
                    and not p.lstrip().startswith(("#", "<", "![", "[![", "```", "|"))
                ]
                if paragraphs:
                    description = paragraphs[0][:600]
                    break
    license_info = metadata.get("license") or {}
    return {
        "schema_version": 1,
        "generator": GENERATOR,
        "name": snapshot["component"].split("/")[-1]
        if snapshot["component"]
        else metadata["name"],
        "full_name": snapshot["full_name"],
        "description": description
        or "No description found in the selected documentation.",
        "owner": metadata["owner"]["login"],
        "repository_url": f"https://github.com/{snapshot['full_name']}",
        "component_path": snapshot["component"],
        "default_branch": snapshot["branch"],
        "commit": snapshot["commit"],
        "technologies": technologies,
        "capabilities": capabilities,
        "dependencies": dependencies[:80],
        "license": license_info.get("spdx_id") or "Unknown",
        "archived": bool(metadata.get("archived")),
        "topics": metadata.get("topics") or [],
        "coverage": {
            "files_indexed": len(snapshot["files"]),
            "eligible_files": snapshot["eligible_files"],
            "tree_files": snapshot["tree_files"],
            "bytes": snapshot["bytes"],
            "bounded": len(snapshot["files"]) < snapshot["eligible_files"],
        },
        "limitations": [
            "Capabilities are documentation mentions; application behavior has not been executed or verified.",
            "Only selected README, documentation, and dependency files are indexed.",
            "Absence from this manifest does not prove a feature is absent from the repository.",
        ],
        "sources": [
            {"path": path, "url": source_url(snapshot, path), "hash": fingerprint(text)}
            for path, text in snapshot["files"].items()
        ],
    }


def build_documents(snapshot, manifest, settings):
    documents = []

    def add(path, title, text, start, end, url):
        # Include repo/component context in the embedding while retaining the original excerpt.
        contextual = f"Repository: {snapshot['full_name']}. Component: {snapshot['component'] or 'repository'}. Section: {title}.\n{text}"
        documents.append(
            {
                "id": fingerprint(
                    snapshot["full_name"].lower()
                    + snapshot["component"]
                    + path
                    + str(start)
                ),
                "path": path,
                "title": title,
                "body": contextual,
                "line_start": start,
                "line_end": end,
                "url": url,
                "fingerprint": fingerprint(contextual),
            }
        )

    summary = "\n".join(
        [
            manifest["description"],
            "Technologies: " + ", ".join(t["name"] for t in manifest["technologies"]),
            "Documented topics: "
            + ", ".join(c["name"] for c in manifest["capabilities"]),
            "Repository topics: " + ", ".join(manifest["topics"]),
        ]
    )
    first_source = manifest["sources"][0]
    add("@manifest", "Project overview", summary, 1, 1, first_source["url"])
    for path, text in snapshot["files"].items():
        lines = text.splitlines()
        current, start, words, title = [], 1, 0, PurePosixPath(path).name
        for number, line in enumerate(lines, 1):
            heading = re.match(r"^#{1,6}\s+(.+)", line)
            if current and (
                heading or words + len(line.split()) > settings.chunk_words
            ):
                add(
                    path,
                    title,
                    "\n".join(current),
                    start,
                    number - 1,
                    source_url(snapshot, path, start, number - 1),
                )
                current, start, words = [], number, 0
            if heading:
                title = heading.group(1).strip()
            current.append(line)
            words += len(line.split())
            if len(documents) >= settings.max_chunks:
                return documents
        if current and any(v.strip() for v in current):
            add(
                path,
                title,
                "\n".join(current),
                start,
                len(lines),
                source_url(snapshot, path, start, len(lines)),
            )
        if len(documents) >= settings.max_chunks:
            break
    return documents[: settings.max_chunks]
