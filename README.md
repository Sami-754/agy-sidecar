# Antigravity Sidecar API (`agy-service`)

OpenAI-compatible local inference bridge powered by Google Antigravity.

> **OS Support: Linux only.** Relies on `systemd --user`, `nice -n 19`, and Linux process management. Windows is untested, unsupported, and not planned.

## Connection Parameters
* **Host Base URL:** `http://localhost:8000/v1`
* **Docker / n8n Base URL:** `http://host.docker.internal:8000/v1`
* **API Key:** `sk-antigravity-local` *(or any key matching `sk-antigravity*`)*
* **Default Model:** `gemini-3.8-flash-low`

## Supported Models
* `gemini-3.8-flash-low` (Default fast model)
* `gemini-3.8-flash-high` (Deep reasoning)
* `gemini-4-argon-low`
* `gemini-4-argon-medium`
* `claude-sonnet-5-5-low`
* `gpt-4o` / `gpt-4o-mini` (Aliases mapped directly to `gemini-3.8-flash-low`)

## Quickstart Examples

### cURL (Chat Completion)
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-antigravity-local" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
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

response = client.chat.completions.create(
    model="gemini-3.8-flash-low",
    messages=[{"role": "user", "content": "Hello world"}]
)
print(response.choices[0].message.content)
```

## Systemd Daemon Management
* **Unit File:** `~/.config/systemd/user/agy-sidecar.service`
* **Source:** `server.py`
* **Restart:** `systemctl --user restart agy-sidecar.service`
