# Innovation Farming

A local prototype for finding reusable solutions in public GitHub repositories. It reads selected documentation and dependency files, generates manifests with source evidence, and searches a persistent SQLite catalog using BM25 and genuine local embeddings. An optional Ollama model extracts requirements and explains reuse opportunities against retrieved source passages.

This is a standalone application. It does not require an Oracle database, OCI credentials, or a separate frontend build.

## Clone the project

```powershell
git clone https://github.com/vsri121/repo-search.git
cd repo-search
```

A fresh Git checkout starts with an empty catalog. Follow the startup steps below, then add sources in the browser or import the included 20-source public catalog. Model downloads, the local database, credentials, and cloned source repositories are kept out of Git.

## Run on Windows

Python 3.11+ and Git are required. From this directory:

```powershell
.\start.cmd
```

Or, with uv:

```powershell
uv sync
uv run uvicorn farm.app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The CMD launcher also works when PowerShell script execution is disabled; it does not change system policy. Pass a different port with `.\start.cmd 8001`. A PowerShell launcher is included for environments that permit scripts. Use a single server worker; it owns the local durable indexing queue. The provided launchers bind only to loopback. Do not expose this prototype on a shared network without adding authentication and repository authorization.

Add a public GitHub repository and optionally a relative component folder. The starter-source action queues five selected Oracle LiveLabs sources. In **Index activity**, follow the actual progress and retry failed jobs. In **Discover**, search by requirement, filter technologies, and open a solution to inspect evidence, source sections, and its downloadable JSON manifest.

The discovery page includes an interactive constellation of actual indexed sources, technology filters with repository counts, and grid/list result layouts. The constellation is a way to open sources, not a graph of inferred semantic relationships. Press `/` or `Ctrl/Cmd+K` to focus search. Your layout preference is saved locally in the browser. Motion respects the operating system's reduced-motion setting, and small screens use a bottom navigation bar.

The expanded source list in `catalogs/oracle-public.json` contains those five LiveLabs sources plus fifteen Oracle sample-code repositories covering AI, MCP, graph, integrations, observability, database drivers, security, and automation. With the app running, queue missing sources and watch their progress with:

```powershell
.venv/Scripts/python.exe scripts/index_catalog.py --wait
```

Rerunning this command skips sources already present in the catalog. Use the library's Sync action to refresh an existing source. Completion and coverage depend on the live indexing jobs; the source list alone is not an indexed dataset.

Use **Set up semantic search** to download `BAAI/bge-small-en-v1.5` through FastEmbed. Its quantized model weights are approximately 67 MB; the complete cache can be larger. Inference runs on the CPU locally. This downloads model artifacts from Hugging Face/FastEmbed's supported model distribution; search queries and documents are not sent to a hosted inference API. The initial model setup can take several minutes. Indexing and keyword search work before the model is ready, and the UI labels the current mode explicitly. Startup reloads a previously enabled model and updates missing embeddings.

## Optional local analysis with Ollama

Install [Ollama](https://ollama.com/download), start it, and download the text generation model once:

```powershell
ollama pull llama3.2:3b
```

Ollama normally listens at `http://127.0.0.1:11434`. If it is not running, launch the installed Ollama application or use `ollama serve`. The farming app checks availability; it does not install Ollama or download its models. No Oracle database, OCI account, or hosted model API key is required. The model download needs internet access and disk space; analysis speed depends on the machine.

Search in **Discover** first, then choose **Analyze reuse with Ollama**. Ordinary search remains available independently of Ollama. Analysis retrieves evidence using the original query and preserves the existing ranking, extracts up to six explicit requirements into structured data, then asks `llama3.2:3b` to assess up to three candidate repositories against that evidence. It shows citations and flags requirements without supporting evidence in the retrieved passages. If Ollama is unavailable, its model is missing, or generation fails, the app reports the problem and preserves ordinary search.

Analysis uses two generation requests, each with a default 60-second timeout, so the full operation can take approximately two minutes plus retrieval overhead. Cancel stops the browser waiting; an already running backend inference request may finish.

The two model roles are separate. `BAAI/bge-small-en-v1.5` continues to generate the existing 384-dimensional search embeddings. Ollama's `llama3.2:3b` generates the analysis text. Installing `embeddinggemma` does not change the embedding provider or reuse its 768-dimensional vectors. Changing the embedding model would require re-embedding the catalog and evaluating retrieval again.

The app selects numbered source excerpts before generation. The model chooses excerpt IDs; the app derives each quote, repository, and line link itself and checks the quote against the indexed source. These checks establish that a quote occurs in the indexed text; they do not establish that it entails the model's interpretation or that a repository's feature works. “Not found” means no supporting evidence was identified within retrieved coverage, not that the entire repository lacks the feature. Treat the analysis as a review aid and inspect the linked source before reuse.

## Current scope

- Public GitHub repositories or an explicitly selected component inside one.
- Up to 24 eligible text files, 80 KB per file, 900 KB total, and 100 sections per indexed solution. Coverage is shown in project details. Large repositories should be scoped to a workshop or component.
- GitHub default branches are resolved to immutable commit SHAs. Evidence links reference that revision and source lines.
- Structured manifest facts and documentation mentions are extracted using rules. Optional Ollama analysis is generated separately from those manifests; capabilities are not execution-verified.
- One embedding per project overview and documentation section. Sections retain repository context. The stored model identity gates vector comparisons.
- FTS5 BM25 and local cosine similarity are fused by reciprocal rank, then grouped by solution. A provisional cosine floor of 0.52 reduces unrelated semantic matches; this requires evaluation on the intended corpus. Scores are relevance rankings, not feature confidence.
- Sync checks commits, caches GitHub responses with ETags, caches embeddings by content hash and model, and atomically replaces snapshots. A failed refresh leaves the old index intact. Deleted sections disappear on successful refresh.
- Clone fetches the indexed Git commit into a new folder under `data/imports/`, including the whole repository when a component is indexed. It does not install dependencies or execute project code. Failed clones may leave a partial folder for inspection.
- Jobs persist in SQLite and interrupted jobs are requeued on restart. A failed clone can be restarted from project details; index and model jobs have Retry actions.

## Configuration and data

Set environment variables before starting or restarting the app:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FARM_DATA_DIR` | `data/` inside this app | SQLite catalog, caches, and clone destination |
| `FARM_OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama API endpoint |
| `FARM_OLLAMA_MODEL` | `llama3.2:3b` | Ollama text generation model; download it separately |
| `FARM_OLLAMA_TIMEOUT_SECONDS` | `60` | Timeout for an Ollama generation request |
| `GITHUB_TOKEN` | Unset | Optional authentication for GitHub metadata/content-tree reads |

Only public repositories are accepted; private-repository authorization is deliberately not implemented. GitHub tokens are not stored by this app or sent to raw-content hosts. Ollama runs locally at the default endpoint. Selected evidence and the user's requirement are sent to that endpoint for analysis; keep its configuration pointed at a trusted local service.

`data/` contains the SQLite database, GitHub response cache, model cache, and explicitly cloned source. It is ignored by Git. The application does not fetch `.env` files, private keys, binaries, or run repository instructions. Indexed content is rendered as escaped text, not active HTML.

## Code map

| File | Responsibility |
| --- | --- |
| `farm/app.py` | FastAPI endpoints, local-origin boundary, static UI |
| `farm/github.py` | Validated repository IDs, scoped read-only ingestion, request cache and limits |
| `farm/manifest.py` | Manifest extraction, evidence links, documentation sections |
| `farm/store.py` | Catalog, FTS5, vector cache, and durable jobs |
| `farm/embeddings.py` | FastEmbed model loading and local inference |
| `farm/search.py` | BM25, cosine similarity, rank fusion, repository grouping |
| `farm/assistant.py` | Ollama availability, requirement extraction, evidence-based assessment, citation validation |
| `farm/worker.py` | Indexing, embedding, refresh, and clone jobs |
| `web/` | Browser UI served by Python; no Node build step |

The UI uses browser-native JavaScript and CSS so the local slice runs with one Python environment. Oracle catalog storage, OCI embeddings, a Codex MCP client, owner corrections, and scheduled/webhook refresh are future adapters/features, not implemented integrations.

## Sharing a prepared demo

`SHARE_GUIDE.txt` contains the recipient's setup steps. To build a ZIP with the source and prepared public catalog, run:

```powershell
.venv/Scripts/python.exe scripts/package_share.py
```

The archive is written to this project's ignored `dist/innovation-farming-share.zip` directory. The builder refuses to overwrite an existing ZIP; use `--output` to choose another filename. It contains a consistent SQLite snapshot with public manifests, selected documentation, and embeddings. Machine-specific job history, cached HTTP responses, and preferences are cleared from that snapshot. Virtual environments, model downloads, cloned repositories, and environment files are excluded. The live catalog is unchanged. The recipient starts with keyword search, enables the embedding model from the sidebar, and installs Ollama plus its generation model separately to use analysis.

## Validation

```powershell
uv run pytest -q
```

Automated tests cover source validation, evidence provenance, scoped ingestion, safe snapshot replacement, FTS refresh, grouping, persistent job coalescing, and the HTTP boundary. They use temporary databases and mocked network responses; no external repositories are modified. Separate live checks should verify model inference and useful ranking on real sources. A small corpus is a smoke test, not retrieval-quality validation for 50 repositories.

Ollama tests also cover unavailable or missing models, timeouts, malformed responses, loopback-only connections, invented source IDs, literal quote provenance, preserved request qualifiers, and duplicate analysis requests. Generation is optional and leaves ordinary retrieval available. These checks establish application behavior, not the factual accuracy of every model interpretation.

With the app running and semantic setup complete, run the GET-only pilot retrieval evaluation:

```powershell
.venv/Scripts/python.exe scripts/evaluate_retrieval.py --output retrieval-report.json
```

The default `catalogs/retrieval-eval.json` contains ten positive queries with reviewable expected repository/component pairs and source references, plus three deliberately unrelated queries. The script requires a ready embedding model and complete embedding coverage by default. `--allow-keyword` permits a separately labeled keyword or mixed-mode run; `--url` selects another local app port. It does not invoke Ollama, download models, sync, or change the catalog. HTTP failures are reported explicitly and cause a nonzero exit; zero means evaluation completed, not that search met an accuracy target.

The JSON report records repository revisions, model identity, retrieval modes, per-case rankings, Hit@3, MRR@3, and precision against known relevant judgments. Precision always divides by three; because pilot judgments list selected known matches rather than every relevant repository, it is not an exhaustive relevance estimate. Unrelated queries returning any result are counted separately as abstention failures. Do not compare partial runs or catalogs that changed during evaluation.

These development cases are not an independently labeled test set. Have teammates review the judgments, add realistic ambiguous and multi-repository requirements, and reserve unseen queries before making accuracy claims. Retrieval metrics do not measure whether generated explanations are correct; review citation entailment, missing requirements, and unsupported claims separately.
