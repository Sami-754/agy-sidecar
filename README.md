# Antigravity Sidecar API (`agy-service`)

OpenAI-compatible local inference bridge powered by the official Google Antigravity CLI (`agy`).

> **OS Support: Linux only.** Relies on `systemd --user`, `nice -n 19`, and Linux process management. Windows is untested, unsupported, and not planned.

## One-Command Setup (New PC)
Ensure `agy` is installed and authenticated (`agy --version`), then clone and run [`install.sh`](install.sh):
```bash
git clone https://github.com/Sami-754/agy-sidecar.git
cd agy-sidecar
./install.sh
```
`install.sh` sets up a local `.venv`, installs dependencies from `requirements.txt`, registers & starts the `agy-sidecar.service` systemd user daemon, and verifies live model discovery.

## Connection Parameters
* **Host Base URL:** `http://localhost:8000/v1`
* **Docker / n8n Base URL:** `http://host.docker.internal:8000/v1`
* **API Key:** `sk-antigravity-local` *(or any key matching `sk-antigravity*` / `agy-*`)*
* **Default Model:** Auto-resolved dynamically from `agy models` (or override via `AGY_DEFAULT_MODEL`)

## Dynamic Models
Models are **never hardcoded** — they are auto-fetched directly from `agy models`:
* Warmed up automatically on daemon startup
* Auto-invalidated immediately whenever the `agy` binary updates on disk
* Auto-refreshed on TTL expiry (`AGY_MODELS_CACHE_TTL`, default `30` seconds; set `0` to fetch live on every `/v1/models` call)
* Force-refresh anytime via `GET /v1/models?refresh=true`
* Legacy OpenAI drop-in aliases (`gpt-4o`, `gpt-4o-mini`) are mapped dynamically to the active default model

## Environment Variables (Optional)
* `AGY_BINARY` — Custom path to `agy` binary (auto-detected via `PATH` or `~/.local/bin/agy`)
* `AGY_API_KEY` — Expected Bearer key (default: `sk-antigravity-local`, or `none` to disable auth)
* `AGY_DEFAULT_MODEL` — Override the auto-selected default model ID
* `AGY_MODELS_CACHE_TTL` — Model list cache TTL in seconds (default: `30`)
* `AGY_MODELS_TIMEOUT` — Timeout for `agy models` in seconds (default: `45`)
* `AGY_TIMEOUT` — Inference timeout in seconds (default: `90`)

## Quickstart Examples

### cURL (List Models & Chat Completion)
```bash
curl -s http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-antigravity-local"

curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-antigravity-local" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Explain async/await in 2 sentences."}]
  }'
```

### Python SDK
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-antigravity-local"
)

models = [m.id for m in client.models.list().data]
response = client.chat.completions.create(
    model=models[0],
    messages=[{"role": "user", "content": "Hello world"}]
)
print(response.choices[0].message.content)
```

## Systemd Daemon Management
* **Unit File:** `~/.config/systemd/user/agy-sidecar.service`
* **Source:** `server.py`
* **Restart:** `systemctl --user restart agy-sidecar.service`
