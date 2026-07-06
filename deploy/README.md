# Deploy on your Hostinger VPS (one shareable HTTPS URL)

This serves the backend **and** the dashboard from your VPS, reachable at
`https://srv1278524.hstgr.cloud` — the link you share with your agency.

## Prerequisites (once)

SSH into the VPS and install Docker:

```bash
ssh root@76.13.20.122

# Docker + compose plugin
curl -fsSL https://get.docker.com | sh

# Open the web ports (Caddy needs 80 + 443 for HTTPS)
ufw allow 80/tcp && ufw allow 443/tcp || true
```

> Ports 80 and 443 must be reachable from the internet. In the Hostinger panel,
> check **VPS → Règles de pare-feu** and allow 80/443 if a firewall is set.

## Deploy

```bash
git clone https://github.com/mehdipardo/trade-agents.git
cd trade-agents
git checkout main

cp .env.example .env
# Edit .env and paste your free Groq key:  GROQ_API_KEY=gsk_...
# (news firehose + economic calendar are already enabled)

docker compose -f docker-compose.prod.yml up -d --build
```

Open **https://srv1278524.hstgr.cloud** — the first load may take ~30 s while
Caddy issues the TLS certificate.

## Update to the latest version

```bash
cd trade-agents && git pull
docker compose -f docker-compose.prod.yml up -d --build
```

## Useful commands

```bash
docker compose -f docker-compose.prod.yml logs -f app     # backend logs
docker compose -f docker-compose.prod.yml ps              # status
docker compose -f docker-compose.prod.yml down            # stop
```

## Notes

- **No key?** The stack still runs (offline classifier + paper fills). Paste the
  Groq key to switch on real analysis; add Kraken Futures / MEXC testnet keys for
  real testnet orders.
- **Admin endpoints** (`/admin/inject`, `/admin/killswitch`) are public by
  default so you can drive the demo. To lock them, uncomment the `basic_auth`
  block in `deploy/Caddyfile`.
- **Server location** is Jakarta — fine for a demo dashboard; for ultra-low-latency
  trading you'd later move the box closer to the exchange.
- **1 vCPU / 4 GB** is plenty for a single-instance demo.
