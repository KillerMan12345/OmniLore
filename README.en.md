# OmniLore

**An AI-native, auditable knowledge base** ｜ *One auditable knowledge base, for every agent.*

> ## `v0.3.0-alpha`
>
> **Pre-release (alpha) — for evaluation, research and contribution only. Do not put it
> into any important workflow.** Internal generation tag v2.3; PEP 440 `0.3.0a0`.
> License: **[Apache-2.0](LICENSE)** ｜ Decisions: **[docs/DECISIONS.md](docs/DECISIONS.md)**
> Repository: **[github.com/KillerMan12345/OmniLore](https://github.com/KillerMan12345/OmniLore)**
>
> 中文版：[README.md](README.md) ｜ Documentation site: [docs/site/index.md](docs/site/index.md)

OmniLore is a *governance layer* for agent knowledge, not a replacement for RAGFlow,
Dify or Obsidian. It keeps immutable SHA-256 CAS source assets, exposes
Obsidian-style Markdown as the human editing surface, and serves every kind of
agent through an MCP stdio endpoint.

**Evidence legend used throughout this page:**
`Implemented` = code exists, evidence is offline tests / static checks ·
`Tested-real` = real-environment evidence (CI items require ≥2 consecutive green runs) ·
`Simulated` = verified only on the in-memory engine or a stub ·
`Planned` = decision accepted, not built.

---

## 1. What is real, and what is not

> **Read this first.** The structured business data currently lives in the
> **in-memory `RLSDatabaseEngine`** — it is cleared when the process exits.
> A real PostgreSQL RLS policy probe runs in CI, but that does **not** mean the
> business read/write path uses PostgreSQL. Do not claim persistence or
> multi-user sharing.

| Capability | State | Evidence boundary |
|---|---|---|
| Immutable CAS source assets + SHA-256 reconciliation | `Implemented` | Bundles recompute hashes from bytes on disk |
| Markdown / code / PDF / image ingestion | `Implemented` | Offline tests exercise the real `pdfplumber` / `pypdf` / `Pillow` libraries |
| Keyword (BM25) retrieval | `Implemented` | Real offline path |
| Hybrid retrieval (BM25 + 1024-d vectors + code symbols) | `Simulated` | The `hybrid` path uses a monkeypatched stub in CI; no real network call |
| PostgreSQL RLS policies (space + category probes) | `Tested-real` | `integration-rls` CI job on real PostgreSQL 16; **only** the 3 probes |
| Other RLS dimensions | `Simulated` | No real probes |
| Answer synthesis with two orthogonal degradation axes | `Implemented` | Offline deterministic fallback |
| Token budget governance + on-demand chunk unfolding | `Implemented` | Offline tests |
| Automatic citation grounding on synthesis | `Implemented` | Offline text-similarity checks, not external fact-checking |
| Causal version DAG + MRP change pre-review | `Implemented` | Offline tests; `REVERTED` is a flat flag, no reverse pointer is fabricated |
| Tiered MCP tooling (7 tools, `query` / `governance`) | `Implemented` | Offline protocol tests |
| Minimal storage backend contract | `Implemented` | Contract + adapter; **no multi-backend runtime switch** |
| Evidence bundle export (`audit-bundle`) | `Implemented` | Cross-process offline tests |
| Full governance demo (`revert` + `reclassify`) | `Implemented` | Offline end-to-end test |
| opt-in anonymous telemetry (local counters + `--show`) | `Implemented` | **No upload transport in this release** |
| Policy-aware MCP (11 tools) | `Planned` | Scheduled after first release |

The authoritative, per-capability matrix is
[`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md). CI green runs cover the offline
suite, `ruff`, a zero-key smoke test and the 3 RLS probes — **not** a real PostgreSQL
business data plane.

---

## 2. Quick start (no API key, no Docker)

```bash
pip install -e .
omnilore bootstrap
omnilore demo
```

`bootstrap` is idempotent: it creates the data directory, the default space and an
owner token, and prints a paste-ready MCP client snippet. `demo` ingests one sample
Markdown file and finds it again via keyword search. Without a vector provider the
deterministic fallback reports `retrieval_mode=keyword_only` — a degradation, not a failure.

```bash
omnilore demo --full --json       # two versions -> revert -> reclassify -> verify
omnilore audit-bundle --out ./out # independently verifiable evidence bundle
make test                         # full offline suite
make lint                         # ruff
```

Full CLI and MCP reference: [docs/site/governance.md](docs/site/governance.md) and
[docs/site/mcp-tools.md](docs/site/mcp-tools.md).

---

## 3. MCP surface

**7 tools**, fixed. `tools/list` accepts an optional `tier` filter; omitting it returns
all 7, so existing clients keep working.

| Tool | Tier | Purpose |
|---|---|---|
| `search_knowledge` | `query` | retrieval + synthesis + token budget + citation guard |
| `get_document_asset` | `query` | `WIKI` / `RAW_ASSET` / `CHUNK` / `EVOLUTION_DAG` |
| `query_health_trends` | `query` | read-only health trends |
| `ingest_document_or_file` | `governance` | ingest |
| `commit_memory_note` | `governance` | commit a note |
| `get_ingestion_task` | `governance` | task status |
| `review_and_fix_knowledge` | `governance` | 9 controlled governance actions |

Every read goes through RLS-aware `select_rows` plus an explicit `scope.can_read`.
Bare client file paths are rejected. New capabilities are attached to existing tool
parameters/actions rather than added as new tools.

---

## 4. Integrity model

- Source bytes are content-addressed; a one-byte change produces a different SHA-256.
- All text is UTF-8 + **LF** (`.gitattributes` pins it; a repository-wide guard enforces it),
  because reconciliation is byte-exact.
- `audit-bundle` **reopens and re-hashes the bytes on disk**; it never trusts the hash
  stored in the index. A mismatch / missing / unreadable result exits with code **3**
  ("verified, conclusion negative"), distinct from code 1 ("command failed").
- `verify-citation` compares a claim against CAS/Wiki text with a normalized
  sliding-window bi-gram score and returns `VERIFIED` / `PARTIALLY_VERIFIED` /
  `UNVERIFIED_OR_MISQUOTED`. An ungrounded claim exits with code **3** in both text and
  `--json` modes.
- `search_knowledge` runs citation grounding **by default** on synthesized answers and
  attaches `citation_verifications` + `unverified_citations_detected`. Set
  `verify_citations=false` to disable it.

> This is offline text similarity, **not** external fact-checking, and must not be
> treated as an authoritative endorsement for legal, medical or safety decisions.

---

## 5. Telemetry and privacy

D3 accepts **opt-in anonymous telemetry**, implemented with these hard constraints:

| Constraint | Implementation |
|---|---|
| Strict opt-in, off by default | Only `omnilore telemetry --enable` starts collection; a fresh install, `bootstrap` and `demo` write zero telemetry bytes |
| Minimal fields | Anonymous aggregate counters only: sub-command counts, degradation count, retrieval-mode counts |
| Self-audit | `omnilore telemetry --show` prints the complete raw payload; a recursive denylist rejects forbidden keys and PII-looking strings |
| Offline-safe | Any failure is silently abandoned; telemetry never blocks or misleads the main flow |
| Global kill switch | `OMNILORE_TELEMETRY=0` wins over persisted consent |
| No upload | `transport.implemented` is `false`; there is no endpoint, HTTP client or background process in this release |

Forbidden: document content, titles, paths, queries, `space_id`, user ids, tokens
(including hashes), IPs, hostnames and any PII.

---

## 6. Known limitations

1. **Business data is not persisted** — in-memory engine, structured state clears on exit.
2. **Only 3 RLS probes** have real PostgreSQL CI evidence; other dimensions are `Simulated`.
3. **Vector recall depends on an external provider**; with zero keys it degrades to BM25.
4. **Filesystem and database are not atomically committed together** — Outbox compensation
   plus `reconcile_consistency` surfaces drift explicitly instead of pretending atomicity.
5. **`REVERTED` is a flat state flag** with no "reverted by which version" pointer;
   the DAG does not invent one.
6. **No compliance certification is claimed** (SOC2 / ISO / etc.). Wording is limited to
   "technical traceability".
7. The `engine.py` architectural split is deliberately deferred until the contributor
   base grows.

See [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for what the threat model cannot defend
against, and [`docs/ROADMAP_STATUS.md`](docs/ROADMAP_STATUS.md) for release gates.

---

## 7. Contributing

All commits require a DCO sign-off (`git commit -s`). Every capability change must update
three places in lockstep: this page's claims, `docs/FEATURE_STATUS.md` and `CHANGELOG.md`.
Every new guard must be proven effective by a mutation test. Never write
`production`, `stable` or "enterprise-ready"; never claim a real PostgreSQL data plane
from in-memory tests.

Start with [`docs/site/contributing.md`](docs/site/contributing.md) and
[`docs/HANDOVER.md`](docs/HANDOVER.md). Report vulnerabilities through a private
GitHub Security Advisory, not a public issue.

Licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE) and
[`CONTRIBUTING.md`](CONTRIBUTING.md).
