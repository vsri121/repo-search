"""Measure retrieval against reviewable pilot judgments using only GET requests.

The supplied cases are development checks, not an independent accuracy benchmark.
No LLM responses are generated, repositories changed, or model downloads requested.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
K = 3


def source_key(source):
    return source["repository"].casefold(), source.get("component", "").strip("/")


def repository_key(repository):
    return repository["full_name"].casefold(), repository.get("component", "").strip(
        "/"
    )


def load_cases(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    cases = data["cases"]
    identifiers = set()
    if not isinstance(cases, list) or not cases:
        raise ValueError("The case file must contain a nonempty cases list.")
    for case in cases:
        identifier = case["id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
        ):
            raise ValueError("Each case needs a unique nonempty string id.")
        identifiers.add(identifier)
        if not isinstance(case["query"], str) or not 2 <= len(case["query"]) <= 1200:
            raise ValueError(
                f"Case {identifier}: query must contain 2-1200 characters."
            )
        if case.get("kind") not in {"positive", "no_match"}:
            raise ValueError(f"Case {identifier}: kind must be positive or no_match.")
        expected = case.get("expected", [])
        if not isinstance(expected, list):
            raise TypeError(f"Case {identifier}: expected must be a list.")
        if bool(expected) != (case["kind"] == "positive"):
            raise ValueError(
                f"Case {identifier}: positive cases need expected sources; no_match cases must not have them."
            )
        for source in expected:
            if not isinstance(source, dict):
                raise TypeError(f"Case {identifier}: expected sources must be objects.")
            if (
                not isinstance(source.get("repository"), str)
                or "/" not in source["repository"]
            ):
                raise ValueError(
                    f"Case {identifier}: expected sources need owner/repository names."
                )
            if not isinstance(source.get("component", ""), str):
                raise TypeError(f"Case {identifier}: component must be a string.")
        if len({source_key(source) for source in expected}) != len(expected):
            raise ValueError(f"Case {identifier}: expected sources must be unique.")
    return data, hashlib.sha256(raw).hexdigest()


def get_json(client, path, **kwargs):
    response = client.get(path, **kwargs)
    response.raise_for_status()
    return response.json()


def evaluate(client, definition, fingerprint, allow_keyword):
    status = get_json(client, "/api/status")
    repositories = get_json(client, "/api/repositories")
    available = {repository_key(repository) for repository in repositories}
    missing = sorted(
        {
            source_key(source)
            for case in definition["cases"]
            for source in case.get("expected", [])
        }
        - available
    )
    if missing:
        raise ValueError(
            "Expected sources are not indexed: "
            + ", ".join(
                name + ("/" + component if component else "")
                for name, component in missing
            )
        )
    if not repositories:
        raise ValueError("The catalog is empty. Index the pilot sources first.")
    ready = bool(status["model"]["ready"])
    coverage = (
        status["embedded_sections"] == status["sections"] and status["sections"] > 0
    )
    if not allow_keyword and not (ready and coverage):
        raise ValueError(
            "Hybrid evaluation requires a ready embedding model and embeddings for every section. Set up semantic search and wait for indexing, or use --allow-keyword for a separately labeled run."
        )
    snapshot = sorted(
        [
            {
                "repository": r["full_name"],
                "component": r.get("component", ""),
                "commit_sha": r["commit_sha"],
                "sections": r["section_count"],
            }
            for r in repositories
        ],
        key=lambda item: (item["repository"], item["component"]),
    )
    rows = []
    for case in definition["cases"]:
        row = {
            "id": case["id"],
            "kind": case["kind"],
            "query": case["query"],
            "expected": case.get("expected", []),
        }
        try:
            response = get_json(
                client, "/api/search", params={"q": case["query"], "limit": K}
            )
            if not allow_keyword and response["mode"] != "hybrid":
                raise ValueError(
                    "The query fell back to keyword search; hybrid results would not be comparable."
                )
            results = response["results"][:K]
            keys = [repository_key(result) for result in results]
            if len(set(keys)) != len(keys):
                raise ValueError("The search API returned duplicate repositories.")
            row.update(
                mode=response["mode"],
                results=[
                    {
                        "repository": result["full_name"],
                        "component": result.get("component", ""),
                        "rank_score": result["rank_score"],
                    }
                    for result in results
                ],
            )
            if case["kind"] == "positive":
                relevant = {source_key(source) for source in case["expected"]}
                ranks = [rank for rank, key in enumerate(keys, 1) if key in relevant]
                row.update(
                    hit_at_3=int(bool(ranks)),
                    reciprocal_rank_at_3=1 / ranks[0] if ranks else 0.0,
                    precision_at_3=len(ranks) / K,
                )
            else:
                row.update(
                    unrelated_query_returned_results=bool(results),
                    returned_count=len(results),
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            row["error"] = str(error)
        rows.append(row)
    completed = [row for row in rows if "error" not in row]
    positives = [row for row in completed if row["kind"] == "positive"]
    negatives = [row for row in completed if row["kind"] == "no_match"]

    def metric(field):
        return (
            round(sum(row[field] for row in positives) / len(positives), 6)
            if positives
            else None
        )

    current = get_json(client, "/api/repositories")
    current_snapshot = sorted(
        [
            {
                "repository": r["full_name"],
                "component": r.get("component", ""),
                "commit_sha": r["commit_sha"],
                "sections": r["section_count"],
            }
            for r in current
        ],
        key=lambda item: (item["repository"], item["component"]),
    )
    unchanged = current_snapshot == snapshot
    return {
        "report_version": 1,
        "case_set": definition.get("name", "Unnamed case set"),
        "case_file_sha256": fingerprint,
        "label_status": definition.get("label_status", "User-provided judgments"),
        "complete": len(completed) == len(rows) and unchanged,
        "catalog_unchanged_during_run": unchanged,
        "retrieval_modes": sorted({row["mode"] for row in completed}),
        "model": {
            key: status["model"].get(key) for key in ("model", "dimensions", "ready")
        },
        "embedding_coverage": {
            "embedded_sections": status["embedded_sections"],
            "sections": status["sections"],
        },
        "catalog_snapshot": snapshot,
        "counts": {
            "planned": len(rows),
            "completed": len(completed),
            "errors": len(rows) - len(completed),
            "positive_completed": len(positives),
            "no_match_completed": len(negatives),
        },
        "metrics_on_completed_cases": {
            "hit_at_3": metric("hit_at_3"),
            "mrr_at_3": metric("reciprocal_rank_at_3"),
            "precision_at_3_known_relevant": metric("precision_at_3"),
            "unrelated_queries_returning_results": sum(
                row["unrelated_query_returned_results"] for row in negatives
            ),
            "unrelated_query_false_positive_rate": round(
                sum(row["unrelated_query_returned_results"] for row in negatives)
                / len(negatives),
                6,
            )
            if negatives
            else None,
        },
        "limitations": [
            "Pilot development judgments are source-anchored but are not independently reviewed or exhaustive relevance judgments.",
            "Precision@3 counts only explicitly labeled relevant sources and always divides by three; unlabeled relevant sources can lower the reported score.",
            "Unrelated-query matches measure failure to abstain for these cases only; the cases do not establish that the whole catalog has no relevant material.",
            "Metrics cover completed cases only. Do not compare incomplete runs, mixed retrieval modes, or changed catalogs as a stable benchmark.",
            "This measures repository ranking, not LLM factuality, citation entailment, executable feature support, or production accuracy.",
        ],
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cases", nargs="?", type=Path, default=ROOT / "catalogs" / "retrieval-eval.json"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="HTTP timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--allow-keyword",
        action="store_true",
        help="Allow a separately labeled keyword or mixed-mode run; default requires fully embedded hybrid retrieval.",
    )
    parser.add_argument(
        "--output", type=Path, help="Also write the JSON report to this local file."
    )
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive.")
        definition, fingerprint = load_cases(args.cases)
        with httpx.Client(
            base_url=args.url.rstrip("/"),
            timeout=args.timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            report = evaluate(client, definition, fingerprint, args.allow_keyword)
        content = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        print(content, end="")
        return 0 if report["complete"] else 1
    except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as error:
        print(
            json.dumps({"complete": False, "error": str(error)}, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
