"""Bounded read-only ingestion from public GitHub repositories."""

import json
import os
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

BLOCKED_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "dist",
    "build",
    "site-packages",
    ".github",
}
METADATA = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "go.mod",
    "cargo.toml",
    "dockerfile",
    "docker-compose.yml",
    "compose.yml",
}


def parse_repository(value: str) -> str:
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if (
            parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use a public https://github.com/owner/repository URL.")
        value = parsed.path.strip("/")
    value = value.removesuffix(".git")
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", value
    ) or value.split("/")[1] in {".", ".."}:
        raise ValueError(
            "Enter owner/repository or its GitHub URL, without a branch or folder suffix."
        )
    return value


def clean_component(value: str) -> str:
    if not value:
        return ""
    value = value.strip().rstrip("/")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or path.is_absolute()
        or len(value) > 240
    ):
        raise ValueError("Component must be a relative folder path, without '..'.")
    return str(path)


def file_priority(path: str, component: str = "") -> int | None:
    relative = path[len(component) + 1 :] if component else path
    parts = PurePosixPath(relative).parts
    if not parts or any(
        p.casefold() in BLOCKED_DIRS or p.startswith(".") for p in parts
    ):
        return None
    name = parts[-1].lower()
    if name in METADATA:
        return 2 + len(parts)
    if name.startswith("readme") and name.endswith((".md", ".rst", ".txt")):
        return len(parts)
    if name.endswith((".md", ".rst", ".txt")) and any(
        p.lower() in {"docs", "documentation", "workshops", "labs", "tutorials"}
        for p in parts[:-1]
    ):
        return 10 + len(parts)
    if name.endswith((".md", ".rst")):
        return (
            12
            if any(v in name for v in ("intro", "overview", "getting-started"))
            else 20
        ) + len(parts)
    return None


class GitHubClient:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.client = httpx.Client(
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=False,
            headers={
                "User-Agent": "innovation-farming-local/0.1",
                "Accept": "application/vnd.github+json",
            },
        )

    def close(self):
        self.client.close()

    def get(self, url: str, *, api=False):
        headers = {}
        with self.store.connect() as db:
            cached = db.execute(
                "SELECT * FROM http_cache WHERE url=?", (url,)
            ).fetchone()
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        if api and os.getenv("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        for attempt in range(3):
            response = self.client.get(url, headers=headers)
            if response.status_code == 304 and cached:
                return cached["body"]
            if response.status_code == 404:
                raise ValueError(
                    "Repository, component, or file was not found. Check that the repository is public and the path exists."
                )
            if response.status_code in {403, 429}:
                raise RuntimeError(
                    "GitHub rate limit or access restriction. Retry later; an optional read-only GITHUB_TOKEN raises the API allowance."
                )
            if response.status_code >= 500 and attempt < 2:
                time.sleep(attempt + 1)
                continue
            response.raise_for_status()
            if len(response.content) > 12_000_000:
                raise ValueError(
                    "Repository tree response is too large for this local indexer."
                )
            body = response.text
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO http_cache VALUES (?,?,?)",
                    (url, response.headers.get("etag"), body),
                )
            return body
        raise RuntimeError("GitHub is temporarily unavailable.")

    def snapshot(self, full_name, component, progress, current_commit=None):
        metadata = json.loads(
            self.get(f"https://api.github.com/repos/{full_name}", api=True)
        )
        if metadata.get("private"):
            raise ValueError("This prototype indexes public repositories only.")
        canonical = metadata["full_name"]
        branch = metadata["default_branch"]
        progress("Reading the default branch and file tree")
        commit_data = json.loads(
            self.get(
                f"https://api.github.com/repos/{canonical}/commits/{quote(branch, safe='')}",
                api=True,
            )
        )
        commit = commit_data["sha"]
        if commit == current_commit:
            return {"unchanged": True, "commit": commit, "full_name": canonical}
        tree_sha = commit
        if component:
            for folder in component.split("/"):
                level = json.loads(
                    self.get(
                        f"https://api.github.com/repos/{canonical}/git/trees/{tree_sha}",
                        api=True,
                    )
                )
                child = next(
                    (
                        item
                        for item in level["tree"]
                        if item["type"] == "tree" and item["path"] == folder
                    ),
                    None,
                )
                if child is None:
                    raise ValueError(
                        "That component folder was not found at the indexed revision."
                    )
                tree_sha = child["sha"]
        tree = json.loads(
            self.get(
                f"https://api.github.com/repos/{canonical}/git/trees/{tree_sha}?recursive=1",
                api=True,
            )
        )
        if tree.get("truncated"):
            raise ValueError(
                "GitHub truncated this repository's file tree. Choose a smaller repository for this prototype."
            )
        paths = [
            {**item, "path": (component + "/" if component else "") + item["path"]}
            for item in tree["tree"]
            if item.get("type") == "blob"
        ]
        if component and not paths:
            raise ValueError(
                "That component folder was not found at the indexed revision."
            )
        candidates = [
            (priority, item["path"])
            for item in paths
            if (priority := file_priority(item["path"], component)) is not None
            and item.get("size", 0) <= self.settings.max_file_bytes
        ]
        candidates.sort()
        selected = candidates[: self.settings.max_files]
        files, total = {}, 0
        for i, (_, path) in enumerate(selected):
            progress(f"Reading file {i + 1}/{len(selected)}: {path}")
            text = self.get(
                f"https://raw.githubusercontent.com/{canonical}/{commit}/{quote(path, safe='/')}"
            )
            size = len(text.encode("utf-8"))
            if (
                size > self.settings.max_file_bytes
                or total + size > self.settings.max_total_bytes
                or "\x00" in text
            ):
                continue
            files[path] = text
            total += size
        if not files:
            raise ValueError(
                "No supported README, documentation, or dependency files were found in that scope."
            )
        return {
            "metadata": metadata,
            "full_name": canonical,
            "commit": commit,
            "branch": branch,
            "component": component,
            "files": files,
            "eligible_files": len(candidates),
            "tree_files": len(paths),
            "bytes": total,
        }
