# Design Brief — FlashSentiment Trader Dashboard

> Brief for the frontend (Next.js) dashboard that consumes the FlashSentiment
> Trader backend. Hand this to the design/build agent.

## 1. Product context (read this first)

FlashSentiment Trader is an **event-driven agentic trading demo**. A news event
enters → an LLM analyst classifies it (sentiment, intensity, asset) → a
deterministic risk engine approves/vetoes → an order is placed on a crypto
**futures testnet** → the result is broadcast live.

**Honest positioning (must be visible, never oversold):** this demonstrates
*agentic reactivity* (news → order in ~1–2 s), **not** alpha vs HFT. It is
**paper trading / testnet only**. A persistent, tasteful `PAPER TRADING · TESTNET`
badge and a short "demo — not investment advice" line are required.

**Audience:** shown in agency demos to non-technical and technical stakeholders.
It must feel *credible and production-grade*, and it must *tell the story* of the
pipeline at a glance.

## 2. Goal

A **single screen** that does two jobs at once:
1. **Hero — animated pipeline.** The star of the demo: watch an event flow
   `dedup → analyst → risk → executor → notifier` in real time, with per-node
   latency and the headline **total latency news→order**.
2. **Cockpit — panels below.** Credible operator view: open positions, P&L,
   recent signals, recent orders, live risk state, and the kill switch.

## 3. Aesthetic direction

**Clean light SaaS** — think Linear / Vercel / Stripe dashboard.

- Light background (near-white `#FBFBFD` / white cards), soft neutral grays,
  hairline borders (`~1px`, low-contrast), gentle shadows, generous whitespace,
  `12–16px` rounded corners.
- **One primary accent** (calm indigo/blue). Semantic colors carry meaning, not
  decoration (see §6).
- Type: a clean grotesk (Inter/Geist) for UI; a **monospace** (Geist Mono /
  JetBrains Mono) for all numbers — prices, sizes, latencies, %.
- Motion is **purposeful and smooth**, never flashy: eased transitions,
  subtle pulses. The animation should read as "precise machine", not "casino".
- Theme-aware: light is primary; a dark variant is a plus but optional.

## 4. Single-screen layout

```
┌───────────────────────────────────────────────────────────────────┐
│  Top bar: ● health  |  FlashSentiment Trader  |  PAPER·TESTNET  |  ⛔ Kill │
├───────────────────────────────────────────────────────────────────┤
│  HERO — animated pipeline                                           │
│   [dedup] → [analyst] → [risk] → [executor] → [notifier]           │
│   latency chips per node · big "news→order: 1.2s" · current headline│
├───────────────────────┬───────────────────────────────────────────┤
│ Open positions        │ Risk state                                 │
│ (long/short, SL/TP)   │ (trades/hr gauge, daily P&L, cooldowns)    │
├───────────────────────┼───────────────────────────────────────────┤
│ Recent signals feed   │ Recent orders feed                         │
├───────────────────────┴───────────────────────────────────────────┤
│  Demo controls: inject scenario × 5                                 │
└───────────────────────────────────────────────────────────────────┘
```

Responsive: on narrow screens the cockpit grid collapses to one column; the hero
pipeline becomes vertical or horizontally scrollable (never let the page scroll
sideways).

## 4b. Sources & Calendar (curated catalog — added)

The platform no longer relies on operator-pasted URLs. It ships a **curated
catalog** of vetted live sources, selectable from the UI, plus a **calendar view**
for scheduled macro events. Two new surfaces to design (a second view or a
slide-over panel is fine; keep the pipeline+cockpit as the home screen):

### Sources panel
- A grid/list of catalog cards, each: name, **kind** badge (economic / social /
  regulatory / news), a one-line description, a **cost** chip (free/freemium/paid),
  an honest **reactivity** label (e.g. "scheduled → ~1–2s", "~seconds (fragile)"),
  optional caveat note, and an **on/off toggle**.
- Data: `GET /api/sources` → `[{id,name,kind,description,cost,reactivity,notes,
  tags,enabled}]`; toggle via `POST /admin/sources/{id}/toggle {enabled}`.
- Seed content: Economic calendar (recommended, on by default), Trump / Truth
  Social (live, fragile), SEC press & litigation, US Congress tracked bills
  (CLARITY/GENIUS), crypto news baseline.

### Calendar view (economic events)
- **Upcoming macro events ranked by expected volatility** (NFP/CPI/FOMC = 5).
  Each row: title, currency/country, **volatility 1–5** (show as a meter or
  ★-style), scheduled time + countdown, forecast/previous, and an **"Arm"** toggle.
- "Armed" = the system pre-positions a watcher that captures the print within
  ~1–2 s of release and fires it into the pipeline (this is the demo's strongest,
  free reactivity story — make armed events feel special: a subtle "primed" state).
- Data: `GET /api/calendar/upcoming?min_volatility=` → `[{id,title,currency,when,
  impact,volatility,forecast,previous,actual,armed}]`; arm via
  `POST /admin/calendar/arm {event_id,armed}`.
- When an armed event releases, it flows through the **same hero pipeline** — the
  UI should visibly connect "armed calendar event → pipeline fires".

## 5. Component specs

### 5.1 Hero — animated pipeline (the centerpiece)
- Five nodes as cards/pills: **dedup, analyst, risk, executor, notifier** (match
  the backend node names). Icons welcome (shield for risk, rocket for executor…).
- When an event is processed, nodes **light up in sequence** left→right as each
  WS event arrives (`event_received → signal → risk_verdict → order →
  pipeline_done`). Animate a "packet" along the edge between nodes.
- Each node shows its **latency** (`timings_ms[node]`, in ms) once it fires.
- **Branching matters:** if the flow skips (duplicate / neutral) or is vetoed
  (risk), the packet **diverts straight to notifier**, colored by the outcome
  status. Show *why* (e.g. "NEUTRAL", "asset in cooldown").
- Prominent **`news → order: 1.2s`** headline (from `total_latency_ms`).
- Show the current event's headline + source/author while it flows.
- Idle state: a calm "waiting for events" resting state (subtle heartbeat).

### 5.2 Open positions
- One row per position: asset, **LONG/SHORT** pill (green/red), entry price,
  size (USDT), **SL / TP** levels (`stop_loss_price` / `take_profit_price`),
  opened-at. Empty state: "No open positions".

### 5.3 Risk state
- **Trades this hour** as a gauge/meter vs the cap (6).
- **Daily P&L** (green/red).
- **Cooldowns** (chips per asset).
- **Kill switch** status; the toggle itself can live in the top bar.

### 5.4 Signals feed & Orders feed
- Signals: sentiment pill (BULL/BEAR/NEUTRAL), asset, intensity as ★, confidence
  %, truncated rationale, status, timestamp.
- Orders: asset, side, size, avg price, exchange latency, status.
- Both are reverse-chronological, capped (~50), with smooth insert animation.

### 5.5 Demo controls
- Five buttons to `POST /admin/inject` the canonical scenarios:
  `trump_btc_bull, cpi_hot_bear, sec_etf_approval, neutral_report,
  prompt_injection`. Label them readably ("Trump: BTC reserve", "Prompt
  injection (robustness)"…). Optional free-text webhook injector.

## 6. Status → color/emoji system

Use these consistently across pipeline, feeds, and toasts:

| status | meaning | emoji | color |
|---|---|---|---|
| `executed` | order placed | 🟢 | green |
| `rejected_risk` | risk veto | 🚫 | amber |
| `skipped_neutral` | neutral / low-conf / no asset | ⚪ | gray |
| `skipped_duplicate` | dedup | ⚪ | gray |
| `failed` | technical error | 🔴 | red |

Side: **LONG = green**, **SHORT = red** (distinct from status green/red via shape
+ label, not color alone — accessibility).

## 7. States, accessibility, motion
- Cover **loading / empty / error / reconnecting** for every panel and the WS.
- WS auto-reconnect with a visible "reconnecting" indicator; REST endpoints
  hydrate initial state on load.
- WCAG AA contrast; never rely on color alone (pair with icon/label/shape);
  respect `prefers-reduced-motion` (disable packet animation, keep state changes).
- Keyboard-operable controls; focus states.

## 8. Data contract (backend is fixed — build to this)

**WebSocket** `GET /ws/live` — messages:
```json
{ "type": "event_received|signal|risk_verdict|order|pipeline_done|heartbeat",
  "event_id": "…", "ts": "ISO-8601", "payload": { }, "timings_ms": { } }
```
Payloads: `signal` = {sentiment, intensity, asset, confidence, rationale,
event_type}; `risk_verdict` = {approved, reject_reason, side,
position_size_quote, stop_loss_pct, take_profit_pct}; `order` = {order_id,
client_order_id, symbol, side, amount, avg_price, status, exchange_latency_ms};
`pipeline_done` = full summary incl. {status, emoji, title, url, sentiment,
intensity, asset, confidence, rationale, side, reject_reason, order_status,
total_latency_ms, timings_ms}.

**REST (reads):** `GET /api/health`, `GET /api/signals?limit`,
`GET /api/orders?limit`, `GET /api/positions` → {positions, state}.

**REST (actions):** `POST /admin/inject` `{scenario}` or `{event}`;
`POST /admin/killswitch` `{active, reason}`; `GET /admin/state`;
`GET /admin/scenarios`.

## 9. Tech & constraints
- Next.js (App Router), TypeScript, WebSocket + fetch. Charting optional/light.
- The backend already emits the full contract — **no backend changes needed**.
- Keep it self-contained and fast; the demo runs `docker compose up` + this UI.

## 10. Non-goals
- No real-money UI, no exchange credentials in the frontend, no order-entry form
  beyond the demo injectors, no fabricated market data.

## 11. Deliverables
1. Layout/wireframe of the single screen (light theme).
2. High-fidelity mock of the hero animated pipeline (idle + mid-flow + a
   rejected/skip branch).
3. Cockpit panels mock (positions, risk, signals, orders).
4. Component + color tokens, and the motion spec for the pipeline animation.
