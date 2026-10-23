"""Queue missing sources from a catalog through the running app's HTTP API."""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "catalogs" / "oracle-public.json",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Watch queued jobs until they finish (up to 30 minutes).",
    )
    args = parser.parse_args()
    sources = json.loads(args.catalog.read_text(encoding="utf-8"))["sources"]
    with httpx.Client(base_url=args.url, timeout=30) as client:
        response = client.get("/api/repositories")
        response.raise_for_status()
        existing = {
            (r["full_name"].casefold(), r["component"]) for r in response.json()
        }
        pending = {}
        for source in sources:
            key = source["repository"].casefold(), source.get("component", "")
            if key in existing:
                print(
                    "Already indexed:",
                    source["repository"],
                    source.get("component", ""),
                    flush=True,
                )
                continue
            response = client.post("/api/repositories", json=source)
            response.raise_for_status()
            identifier = response.json()["job_id"]
            pending[identifier] = source
            existing.add(key)
            print(
                "Queued:",
                source["repository"],
                source.get("component", ""),
                identifier,
                flush=True,
            )
        if not args.wait or not pending:
            return 0
        deadline = time.monotonic() + 1800
        failures, last_messages = [], {}
        while pending and time.monotonic() < deadline:
            response = client.get("/api/jobs")
            response.raise_for_status()
            for job in response.json():
                if job["id"] not in pending:
                    continue
                message = job["status"], job["message"]
                if last_messages.get(job["id"]) != message:
                    print(
                        f"{job['status']}: {pending[job['id']]['repository']}: {job['message']}",
                        flush=True,
                    )
                    last_messages[job["id"]] = message
                if job["status"] in {"completed", "failed"}:
                    if job["status"] == "failed":
                        failures.append(pending[job["id"]]["repository"])
                    del pending[job["id"]]
            if pending:
                time.sleep(5)
        if pending:
            print(
                "Stopped waiting; these jobs remain in Index activity:",
                list(pending),
                flush=True,
            )
        if failures:
            print("Failed sources:", ", ".join(failures), flush=True)
        return 1 if pending or failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (httpx.HTTPError, ValueError, OSError) as error:
        raise SystemExit(f"Catalog import failed: {error}") from error
