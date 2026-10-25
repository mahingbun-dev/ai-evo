# AgentEvo — Self-hosted Agent Quality Engineering Platform

AgentEvo is a self-hosted Agent quality engineering platform — the positioning is *evaluation as a release gate*: model and Agent changes pass evaluation before they ship. Live today: model configs, dataset management, pre-evaluation scripts, 9 task evaluation types with AI judges (AI judge, agent long-session, consistency, success-rate, RAG retrieval, binary classification, latency, plugin, safety red-teaming) plus persona-driven simulation via scenario runs, deterministic end-state verifiers (script / db_assert / file_assert / http_assert, aggregated into a `verification_pass_rate` gate metric), trajectory matching for agent sessions (strict / unordered / subset / superset tool-call sequence assertions with expected/forbidden entries, aggregated into a `trajectory_match_rate` gate metric), τ-bench-style pass^k statistics (judge-type per-row multi-trial 2-5 plus consistency/success-rate repeats, pass@1 / pass^k / score SE), significance testing for A/B tests (multi-round paired runs with Wilcoxon signed-rank + paired t + Cliff's delta and exact McNemar), judge governance (per-execution judge config snapshots with masked credentials, `<eval_data>` prompt-injection fencing on by default that can only be disabled explicitly per task with an audit trail, and canary rows — ~2% empty/garbage probes that raise a "judge can be fooled" risk badge when scored high, excluded from all aggregates), human gold-score annotation with judge–human agreement reports, result analytics and A/B testing, plus the release-gate loop — threshold gates configured on evaluation tasks (with a verdict banner on results and a gate column in execution history), API Keys with scoped permissions, webhook notifications, the developer-settings console for keys/webhooks, and the `aievo-cli` command line for CI (exit codes 0=passed / 1=gate failed / 2=error, with GitHub/GitLab/Jenkins templates under `ci-templates/`). Built with FastAPI + Vue 3 + Celery, deployable as a single Docker Compose stack.

![Home](docs/assets/00-home.png)

[![CI](https://github.com/mahingbun-dev/ai-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/mahingbun-dev/ai-evo/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-teal)
![Vue 3](https://img.shields.io/badge/Vue-3-brightgreen)

[中文文档](README.zh-CN.md)

## What it does

Preparation → Evaluation → Analysis, in one platform:

| Module | Capabilities |
|---|---|
| Model configs | OpenAI / Anthropic compatible endpoints, custom HTTP (import from curl), runtime variable injection, streaming connectivity test |
| Datasets | Excel import (dynamic fields), Agent session file import, media fields, field-level validation, dataset versioning (immutable snapshots, row-level diff between versions, restore, and follow-latest / lock-a-version binding on evaluation tasks); OTel-compatible trace ingest (M8: `POST /ingest/otel-traces` accepts OTLP/HTTP JSON, Arize Phoenix exports, or Langfuse exports and turns each trace into an agent-session dataset row feeding the existing trace evaluation chain — the platform coexists with your tracing stack, it is not a tracing backend) |
| Script library | Pre-evaluation scripts (zip / folder packages), `model.py` contract declaration, online file-tree editing, scaffold templates |
| Evaluation tasks | AI judge / Agent long-session / consistency / success-rate / RAG retrieval / binary classification / latency / plugin / safety red-teaming (dual-channel: built-in attack pack + mutation, deterministic detectors + judge rubric, M3), batch runs, real-time progress over WebSocket; persona-driven simulation (M7, runs through scenario objects instead of tasks) — user-simulator personas, fault injection (tool timeout / error / dirty data / contradiction / context overflow), end-state verification, pass^k, trial replay and failed-trial-to-dataset; evaluation-template versioning (immutable snapshots, field-level diff, restore, M5); per-task cron scheduling (5-field cron, Beijing time, optional time window; dispatched through the regular run path — team quota and per-task mutex apply; scan loop lives in the API process with a Redis lock + `next_run_at` CAS claim, no Celery beat, M5) |
| Judge engines | Three engines live: Claude Code judge (default), the generic LLM judge graph, and the DeepSeek Agent judge (deepseek-harness-sdk, wired into the task flow at M8); "compare with Claude Code" on the config card produces a two-engine judge agreement report (per-row score diffs / within-tolerance agreement rate over the same batch of existing outputs, M8); per-task evaluation dimensions |
| Judge concurrency pool | Global cap on judge LLM concurrency: independent limits per graph / claude_agent / harness / plugin engine (Redis lease + heartbeat + zombie reclamation, FIFO queue with ghost-head recycling, fail-open on Redis degradation), per-task row-level concurrency (`concurrency`, 1–64, decoupled from judge count), super-admin monitor & config page with force-release |

First registered user automatically becomes the super admin; sessions are JWT (`Authorization: Bearer`), passwords stored as PBKDF2-HMAC-SHA256 hashes. Enterprise identity: OIDC SSO (authorization code + PKCE S256) and LDAP/AD read-only auth (search + bind, with RFC 4515 filter-value escaping and empty-password rejection) can be enabled with `AUTH_METHODS=local,oidc,ldap` in any combination (a single `oidc` or `ldap` value disables local login/registration) — the super admin configures the IdP or directory (with connectivity self-check) in developer settings; first enterprise login JIT-provisions the account (or maps an existing one by email), and directory groups map to platform teams via a shared mapping table.

## Quick start (local development)

Prerequisites: Python 3.12+, Node.js 18+ with pnpm, Docker (for PostgreSQL and Redis).

```bash
# 1. Start PostgreSQL + Redis (dev containers)
docker compose -f docker-compose.dev.yml up -d

# 2. Backend dependencies (use a virtualenv)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment (.env is not committed)
cp .env.example .env
#   set DATABASE_URL / REDIS_URL / AUTH_JWT_SECRET

# 4. Run database migrations
export AIEVO_DATABASE_URL="$DATABASE_URL"
alembic upgrade head

# 5. Start the backend (port 8100)
python scripts/run_local_dev.py

# 6. Start the frontend (another terminal, /api proxies to 8100)
cd frontend && pnpm install && pnpm dev   # https://localhost:3000
```

Open `https://localhost:3000` → register (first user becomes super admin) → add a target model and a judge model under **Model Configs** → import a dataset from Excel → create and run an evaluation task.

Docker only and just want a taste? See the one-minute demo in
[README.zh-CN.md](README.zh-CN.md)（一分钟演示）or [QUICK_START.md](QUICK_START.md):
`AIEVO_DEMO_MODE=true` + `COMPOSE_PROFILES=bundled-db` starts a full stack with a
bundled PostgreSQL, auto-generated JWT secret, automatic migrations and a
one-click demo evaluation on the home page (built-in mock endpoint, no
credentials needed; production deployments must set `AUTH_JWT_SECRET` explicitly
and keep demo mode off).

## Deploy with Docker Compose

The image is built online (`Dockerfile.online`: Node stage builds the frontend, Python stage installs dependencies):

```bash
cp .env.example .env   # set DATABASE_URL / REDIS_URL / AUTH_JWT_SECRET / IMAGE_TAG
docker compose up -d --build
```

- `api` serves both the REST API and the built frontend on port 8100 (`AIEVO_PORT` to change)
- `worker` runs Celery evaluation tasks; `logs/` and `uploads/` are persisted via volumes
- database migrations run automatically on `up -d`: a one-shot `migrate` service runs
  `alembic upgrade head` first (api depends on its successful exit) and the api
  entrypoint re-runs it as an idempotent no-op safety net — no manual
  `docker exec` migration after upgrades
- on the first `up -d`, the one-shot `volume-init` service automatically migrates the
  `uploads/` and `logs/` volume ownership to the container runtime user (uid 10002) —
  no manual `chown` needed (`docker compose ps` showing it as `exited(0)` is normal)
- set `COMPOSE_PROFILES=bundled-db` in `.env` to also start a bundled PostgreSQL
- pre-evaluation script execution mode `SCRIPT_EXEC_MODE`: the default `local` is
  **for development machines only** (scripts run as plain subprocesses next to the
  platform); production/enterprise deployments must not use `local` — set
  `SCRIPT_EXEC_MODE=container` once the sandbox channel is in place, see
  section 9 of [docs/build-pipeline.md](docs/build-pipeline.md)

The full `.env` variable reference is in section 4.4.1 of
[docs/build-pipeline.md](docs/build-pipeline.md) and `.env.example`;
the offline/CI pipeline (locked wheels, bundled Claude Code CLI) is described in [docs/build-pipeline.md](docs/build-pipeline.md).

## Evaluation types

| Type | One-liner | Needs a judge model |
|---|---|---|
| AI judge | LLM judge scores model outputs on configured dimensions | Yes |
| Agent (long session) | Judge scores imported agent session transcripts | Yes |
| Consistency | Same input called N times, measures output stability | Optional |
| Success rate | Share of successful calls | No |
| RAG retrieval | Precision / recall and 4 more retrieval metrics | Partial |
| Binary classification | Label comparison: accuracy / sensitivity / specificity | No |
| Latency | Per-call response time against a configurable threshold (avg / p50 / p90 / p99) | No |
| Plugin | Static lint of imported plugin packages (7 sub-scores), optionally plus a judge-layer rubric review (standard mode) | Standard mode only |
| Safety (red team) | Built-in attack pack (prompt injection / jailbreak / sensitive content / factual assertion, 30+ samples each) attacks the target model; dual-channel verdicts — deterministic detectors (Chinese PII & sensitive-word lexicons) plus a judge rubric channel, with optional mutation enhancement, optional deepeval metric switches, and compliance-evidence report export (markdown / xlsx) | Yes (rubric channel) |

Dialogue evaluation is not a separate type — multi-turn scenarios are covered by the Agent (long session) type.

## Documentation

- [User guide (中文, with screenshots)](docs/user_guide.md)
- [Quick start (中文)](QUICK_START.md)
- [CI gate templates — GitHub Actions / GitLab / Jenkins](ci-templates/README.md) and the [`aievo-cli`](cli/README.md) command line that powers them
- API docs are served by the backend at `/api-docs` once running

## Security notes

- `AUTH_JWT_SECRET` must be set to a strong random value (`openssl rand -hex 32`):
  startup fails fast when it is missing; the "derive from DATABASE_URL" fallback
  requires explicitly setting `AUTH_JWT_ALLOW_DERIVED=true`, is for local development
  only, and must never be enabled in enterprise deployments
- OIDC SSO callback is state-checked and PKCE (S256)-protected; SSO login state
  is a signed short-lived HttpOnly cookie — a mismatched state, forged/expired
  cookie, replayed code, or open-redirect target is rejected without provisioning
  anything (covered by `tests/security/test_oidc_attack_surface.py`)
- LDAP login escapes filter values per RFC 4515 before substitution, rejects
  empty passwords before any bind (anonymous-bind quirk), and reports directory
  failures uniformly as "wrong username or password" without leaking directory
  state (covered by `tests/security/test_ldap_attack_surface.py`)
- Share links (M5) carry 192-bit tokens stored as sha256 hashes only (the
  plaintext is returned exactly once at creation); the public report endpoint
  is anonymous but rate-limited (Redis, 10 req/min/IP) and answers expired,
  revoked and unknown tokens with an identical 404 so tokens cannot be
  enumerated (covered by `tests/security/test_share_links.py`)
- `SCRIPT_EXEC_MODE=local` (pre-evaluation scripts run as plain subprocesses) is for
  development machines only and forbidden in enterprise deployments; the compliant
  path is `container` sandbox mode — see section 9 of [docs/build-pipeline.md](docs/build-pipeline.md)
- anc-hub endpoints use strict TLS verification by default; self-signed internal endpoints can opt out with `AIEVO_HUB_TLS_INSECURE=1`
- This platform intentionally calls user-configured model endpoints (including internal gateways) — deploy it inside a trust boundary you control

## License

[Apache-2.0](LICENSE)
