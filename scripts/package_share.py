"""Build a portable source ZIP with a sanitized snapshot of the public catalog."""

import argparse
import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = [
    "README.md",
    "SHARE_GUIDE.txt",
    "requirements.txt",
    "pyproject.toml",
    "uv.lock",
    "start.cmd",
    "start.ps1",
    ".gitignore",
]
SOURCE_DIRS = ["farm", "web", "scripts", "tests", "catalogs"]
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html", ".json"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "innovation-farming-share.zip",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(
            f"Output already exists: {output}. Choose another --output path."
        )
    source_db = ROOT / "data" / "catalog.sqlite3"
    if not source_db.is_file():
        raise FileNotFoundError(
            "Index the public catalog before creating the demo handoff."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".share-catalog-", suffix=".sqlite3", dir=output.parent, delete=False
    ) as temporary:
        snapshot_path = Path(temporary.name)
    try:
        source = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            source.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            snapshot.executescript(
                "DELETE FROM jobs; DELETE FROM http_cache; DELETE FROM preferences;"
            )
            snapshot.execute("VACUUM")
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Catalog integrity verification failed.")
            count = snapshot.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
            sections = snapshot.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            embedded = snapshot.execute(
                "SELECT COUNT(*) FROM documents WHERE vector IS NOT NULL"
            ).fetchone()[0]
            repositories = snapshot.execute(
                "SELECT full_name,component,commit_sha,manifest FROM repositories ORDER BY full_name,component"
            ).fetchall()
            if any(
                not name.startswith(("oracle-samples/", "oracle-livelabs/"))
                for name, _, _, _ in repositories
            ):
                raise RuntimeError(
                    "This handoff builder only permits the selected public Oracle catalog."
                )
            if any(
                snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("jobs", "http_cache", "preferences")
            ):
                raise RuntimeError("Machine-specific state was not removed.")
        finally:
            source.close()
            snapshot.close()
        files = [ROOT / name for name in ROOT_FILES]
        for directory in SOURCE_DIRS:
            files.extend(
                path
                for path in (ROOT / directory).rglob("*")
                if path.is_file()
                and path.suffix in SOURCE_SUFFIXES
                and not any(
                    part.startswith((".", "__pycache__"))
                    for part in path.relative_to(ROOT).parts
                )
            )
        attribution = [
            "PUBLIC CATALOG SOURCES",
            "",
            "Selected public documentation excerpts and metadata are bundled for review.",
            "The original repositories, revision links, and reported license identifiers follow.",
            "License metadata is a repository-level declaration, not a legal assessment.",
            "",
        ]
        for name, component, commit, manifest_json in repositories:
            manifest = json.loads(manifest_json)
            attribution.extend(
                [
                    name + (" / " + component if component else ""),
                    f"https://github.com/{name}/tree/{commit}"
                    + ("/" + component if component else ""),
                    "Reported license: " + manifest.get("license", "Unknown"),
                    "",
                ]
            )
        info = {
            "application": "Innovation Farming",
            "version": "0.2.0",
            "built_at_utc": datetime.now(UTC).isoformat(),
            "solutions": count,
            "sections": sections,
            "embedded_sections": embedded,
            "model_cache_included": False,
            "machine_history_included": False,
        }
        with zipfile.ZipFile(
            output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(files):
                archive.write(
                    path, "innovation-farming/" + path.relative_to(ROOT).as_posix()
                )
            archive.write(snapshot_path, "innovation-farming/data/catalog.sqlite3")
            archive.writestr(
                "innovation-farming/CATALOG_SOURCES.txt", "\n".join(attribution)
            )
            archive.writestr(
                "innovation-farming/PACKAGE_INFO.json",
                json.dumps(info, indent=2) + "\n",
            )
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP checksum verification failed.")
            members = archive.namelist()
            blocked = {
                ".venv",
                "models",
                "imports",
                "__pycache__",
                ".env",
                ".oci",
                ".aws",
                ".git",
            }
            if any(blocked.intersection(Path(name).parts) for name in members):
                raise RuntimeError("An excluded artifact was found in the ZIP.")
        print(
            json.dumps(
                {
                    **info,
                    "archive": str(output),
                    "size_bytes": output.stat().st_size,
                    "archive_files": len(members),
                },
                indent=2,
            )
        )
    finally:
        snapshot_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
