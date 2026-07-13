# LLM observability with self-hosted Langfuse

Trace every analyst + critique LLM call — the exact prompt sent, the response,
latency, token usage — and replay any decision to debug *why* a news item was
classed bullish/bearish. Open-source, self-hosted on your VPS, no data leaves it.

## Start it

On the VPS, alongside the main stack (shared Docker network):

```bash
cd /root/trade-agents
docker compose -f docker-compose.prod.yml -f deploy/langfuse/docker-compose.yml up -d
```

## Connect the app

1. Open **http://76.13.20.122:3000** (Langfuse UI).
2. Sign up (first user is the admin), create an **Organization → Project**.
3. Project **Settings → API Keys → Create** → copy the public + secret keys.
4. Put them in `/root/trade-agents/.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=http://langfuse:3000
   ```
5. Restart the app so it picks up the keys:
   ```bash
   docker compose -f docker-compose.prod.yml up -d app
   ```

That's it. From the next analysed news item, traces appear in Langfuse.

## What you get

- **Every LLM call traced**: prompt, response, latency, prompt/completion tokens.
- **Metadata to find a decision**: each analyst trace is tagged with the
  `event_id`, `source`, and `prompt_version`; critiques are tagged `critique`.
- **Replay/debug**: open a trace, inspect the exact prompt, tweak it, and re-run
  to see how the classification would change.
- **Cost & latency dashboards** over time, per prompt version.

## Notes

- Traces are **best-effort**: if Langfuse is down or the keys are wrong, the
  pipeline keeps trading — tracing never blocks or breaks analysis.
- Change `NEXTAUTH_SECRET` and `SALT` in the compose file before exposing 3000
  publicly, and ideally reverse-proxy it behind Caddy with auth rather than
  publishing the port directly.
- Pinned to Langfuse v2 (the app uses the v2 LangChain callback API).
