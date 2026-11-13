# AGENTS.md — Antigravity Sidecar (`agy-service`)

Local OpenAI-compatible API daemon backed by the official compiled Google Antigravity CLI binary (`agy`).

## Operational Directives
* **Binary Path:** Resolved automatically via `shutil.which("agy")` or `~/.local/bin/agy` (or `AGY_BINARY` environment variable).
* **Zero Scraping:** Do NOT replace with unofficial web endpoints or reverse-engineered cookies. All traffic routes through the official CLI harness authenticated to the host Google account.
* **Zero Lag Policy:** Any CLI spawn MUST maintain `nice -n 19` and lock behind `asyncio.Semaphore(1)`. Never increase concurrency beyond 1; background gaming/workstation frame-rate stability takes precedence over throughput.
* **Port Binding:** `0.0.0.0:8000` (dual access for `localhost` and Docker `host.docker.internal`).

## Endpoints
* `GET /health` — Service health and binary metadata.
* `GET /v1/models` — OpenAI model catalog (dynamically auto-fetched via `agy models` + GPT-4o drop-in aliases).
* `POST /v1/chat/completions` — Standard OpenAI Chat Completions (used by AI Agent nodes, Cursor, IDEs).
* `POST /v1/responses` — OpenAI Responses API (required by n8n OpenAI Node v2.3+).

## Prompting Design
All prompting is dynamic. The daemon acts purely as an inference bridge without hardcoded business rules:
* Send your prompt, rules, constraints, and personas directly in the request body to `/v1/chat/completions` or `/v1/responses`.
* In n8n or client apps, configure task directives and schemas directly in the caller's node parameters.

## Daemon Lifecycle
```bash
# Check status / logs
systemctl --user status agy-sidecar.service
journalctl --user -u agy-sidecar -f

# Restart daemon after edits
systemctl --user restart agy-sidecar.service
```
