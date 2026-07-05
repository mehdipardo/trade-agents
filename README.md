# FlashSentiment Trader — Backend

Event-driven **agentic** trading backend: a news event enters (webhook, RSS, or
scenario injection) → an LLM analyst classifies it (sentiment, intensity, asset)
→ a deterministic risk engine approves or vetoes → an order is placed on a crypto
**testnet** via CCXT → Slack notification + WebSocket broadcast to a dashboard.

> ⚠️ **Demo / paper trading only — not investment advice.** This system routes
> orders exclusively to an exchange **sandbox/testnet**. There is no live-trading
> code path. Real-money use would require regulatory review. The project
> demonstrates *agentic reactivity* (news → order in ~1–2 s), not alpha
> generation against HFT.

## Non-negotiable safety guards

- `PAPER_TRADING=true` and `EXCHANGE_SANDBOX=true` are **mandatory**. The
  application **refuses to start** otherwise.
- No live-trading branch exists anywhere in the code.
- API keys come only from the environment and are never logged. Use **testnet
  keys with no withdrawal permissions**.

## Requirements

- Python 3.11+
- Redis (for dedup, risk counters, history) — a `docker-compose.yml` provides it.

## Quickstart (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # keep PAPER_TRADING=true and EXCHANGE_SANDBOX=true
uvicorn app.main:app --reload
```

Then:

```bash
curl -s http://localhost:8000/api/health | python -m json.tool
```

## Quickstart (Docker)

```bash
cp .env.example .env
docker compose up --build
```

The stack starts `redis` + the FastAPI `app`. Health check:
`GET http://localhost:8000/api/health`.

## Safety guard demo

Starting with an unsafe configuration is rejected with a clear message:

```bash
PAPER_TRADING=false uvicorn app.main:app
# FATAL: application refused to start.
# Value error, Refusing to start due to unsafe configuration:
#   - PAPER_TRADING must be 'true'. ...
```

## Tests

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -m "not slow"
```

## Project layout

```
app/
  main.py            FastAPI app, lifespan, routers
  config.py          settings + mandatory safety guards
  logging_config.py  structlog JSON setup
  api/               HTTP + WebSocket routes
  ingestion/         normalizer, simulator, RSS poller
  graph/             LangGraph state, builder, nodes
  risk/              pure deterministic risk rules
  services/          LLM, exchange (CCXT), Slack, Redis store, position monitor
  models/            Pydantic schemas
  prompts/           analyst system prompt
data/scenarios/      canonical demo scenarios (JSON)
tests/               unit + integration tests
```

## Roadmap

Development follows the incremental roadmap in the project brief (Étape 0 →
Étape 9). This README is updated as steps land.

- **Étape 0 — Scaffold** ✅ config + safety guards, structured logging,
  `GET /api/health`, Dockerfile + docker-compose.
