# FlashSentiment Trader — Backend

![CI](https://github.com/mehdipardo/trade-agents/actions/workflows/ci.yml/badge.svg)

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

## Tests & dev

```bash
make install      # dev dependencies
make lint         # ruff
make test         # pytest -m "not slow"
make eval         # analyst golden-set evaluation (CI gate)
make run          # uvicorn --reload
make up           # docker compose up (app + redis)
```

CI (GitHub Actions, `.github/workflows/ci.yml`) runs ruff, the test suite, and
the golden-set eval gate on every push/PR to `main`.

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

## Pipeline

```
news ─▶ dedup ─▶ analyst (1 LLM call) ─▶ risk (deterministic) ─▶ executor ─▶ notifier
                     │ NEUTRAL/low-conf         │ vetoed                │
                     └──────────────────────────┴───────────────────────┴─▶ notifier ─▶ WS/Slack
```

Every node records its latency in `timings_ms`; the notifier broadcasts the
full state to `/ws/live` and posts to Slack (non-blocking).

## Exchange: futures testnet (shorts)

The agents must be able to **short**, which spot markets cannot do, so the
executor targets a **futures/perpetuals testnet**. Binance Futures is
geo-blocked in France, so the default is **Kraken Futures** (CCXT
`krakenfutures`, demo environment via `set_sandbox_mode(True)`); **MEXC** is a
supported alternative. Use **testnet keys with no withdrawal rights**. Without
keys the executor runs an **offline paper fill** so the full demo still works.
For a live Kraken Futures testnet, set the whitelist to its perpetual symbols,
e.g. `ASSET_WHITELIST=BTC/USD:USD,ETH/USD:USD,SOL/USD:USD`.

## Risk rules (defaults, all configurable)

| Rule | Default | Rejects when |
|---|---|---|
| Confidence threshold | 0.6 | `confidence < threshold` |
| Min intensity | 3 | `intensity < 3` |
| Sizing by intensity | 3→1%, 4→2%, 5→3% of equity | — |
| Notional cap | min(5% equity, 100 USDT) | — |
| Side | BULL→buy (long), BEAR→sell (short) | — |
| Stop-loss / take-profit | 1.5% / 3.0% (RR 1:2) | — |
| Max trades / hour | 6 (sliding) | exceeded |
| Cooldown per asset | 15 min | recent trade on asset |
| Positions per asset | 1 (either direction) | already open |
| Daily loss cap | −3% equity | **latches kill switch** |
| Manual kill switch | `/admin/killswitch` | active |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + safety-guard status |
| GET | `/api/signals` `/api/orders` `/api/positions` | dashboard reads |
| POST | `/admin/inject` | inject a scenario or raw event |
| POST | `/admin/killswitch` | activate / reset the kill switch |
| GET | `/admin/state` | risk-state snapshot |
| GET | `/admin/scenarios` | list demo scenarios |
| POST | `/webhooks/news` | secured generic webhook (`X-Webhook-Secret`) |
| WS | `/ws/live` | live pipeline feed |

## Demo (5 scenarios, no external source)

```bash
docker compose up --build          # or: uvicorn app.main:app
python scripts/demo.py             # injects the 5 scenarios and prints outcomes
```

Expected (sequential): `trump_btc_bull` → BULL/BTC **long executed**;
`cpi_hot_bear` → BEAR/BTC, **vetoed** by the concurrency guard because BTC is
already held from the Trump trade (short a BTC-free run instead to see it
execute); `sec_etf_approval` → BULL/SOL **long executed**; `neutral_report` →
**skipped_neutral**; `prompt_injection` → **NEUTRAL** (injection ignored). The
BEAR-opens-a-short path is covered directly in `tests/test_risk_rules.py` and
`tests/test_graph_e2e.py`.

## Analyst evaluation (golden set)

A labeled golden set (`data/golden/eval_set.jsonl`, 50 news items) measures the
analyst's sentiment accuracy, asset-mapping accuracy, per-class P/R/F1, a
confusion matrix, and calibration (reliability bins + Expected Calibration
Error). It runs against whatever analyst is active — the real LLM when a key is
set, otherwise the deterministic offline classifier (stable for CI regression).

```bash
python scripts/eval.py --min-accuracy 0.6   # non-zero exit if below the floor
```

The offline classifier scores ~68% sentiment accuracy with low ECE (~0.03); a
real LLM scores higher. Optional **Langfuse** tracing (self-hosted) activates
only when `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set — inert otherwise.

## Limitations (honest)

- Demonstrates **agentic reactivity** (news → order in ~1–2 s), **not** alpha vs HFT.
- Paper/testnet only; there is no live-trading code path.
- No native OCO: SL/TP are enforced by a 2 s polling monitor.
- In offline mode (no keys) the analyst uses a keyword classifier and orders are
  paper fills — clearly logged, never presented as real analysis.

## Roadmap status

Étapes **0–9 complete**: scaffold, schemas+simulator, mock graph, LLM analyst,
risk engine, executor+shorts, notifier, ingestion, hardening+demo, and the
Étape 9 stretch (golden-set evaluation with accuracy/calibration + optional
Langfuse tracing). Remaining optional ideas: native exchange OCO orders.
