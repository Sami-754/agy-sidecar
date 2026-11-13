#!/usr/bin/env python3
"""
agy-sidecar: Official Google Antigravity OpenAI-Compatible API Bridge
Uses official Antigravity CLI (~/.local/bin/agy or system PATH) with zero unofficial scraping.
Exposes standard OpenAI endpoints on 0.0.0.0:8000 (accessible via localhost & host.docker.internal).
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger("agy-sidecar")

API_KEY_EXPECTED = os.environ.get("AGY_API_KEY", "sk-antigravity-local")
DEFAULT_MODEL_ENV = os.environ.get("AGY_DEFAULT_MODEL")
MODELS_CACHE_TTL = int(os.environ.get("AGY_MODELS_CACHE_TTL", "30"))
MODELS_RETRY_TTL = 10
MODELS_FETCH_TIMEOUT = int(os.environ.get("AGY_MODELS_TIMEOUT", "45"))
CLI_TIMEOUT = int(os.environ.get("AGY_TIMEOUT", "90"))

# Legacy OpenAI drop-in aliases mapped dynamically to the active default model
LEGACY_ALIAS_CATALOG: List[Dict[str, str]] = [
    {"id": "gpt-4o", "description": "OpenAI drop-in alias mapped to active default Antigravity model"},
    {"id": "gpt-4o-mini", "description": "OpenAI drop-in alias mapped to active default Antigravity model"},
]
LEGACY_ALIAS_NAMES = {"gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5-turbo", "default"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_STRUCTURED_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:[-.:/_][A-Za-z0-9]+)+$")
_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$")

# Concurrency lock: maximum 1 agy execution at a time with nice -n 19 to guarantee ZERO PC lag
SEMAPHORE = asyncio.Semaphore(1)

# Dynamic caches (invalidated automatically on TTL expiry or agy binary update)
_models_lock = asyncio.Lock()
_models_cache: List[Dict[str, str]] = []
_models_cache_expires_at: float = 0.0
_models_cache_sig: Optional[Tuple[str, int, int]] = None
_models_source: str = "uninitialized"
_models_bg_task: Optional[asyncio.Task] = None

_version_lock = asyncio.Lock()
_version_cache: Optional[str] = None
_version_sig: Optional[Tuple[str, int, int]] = None


def _resolve_agy_binary() -> str:
    """Resolves the official agy CLI path dynamically from env, PATH, or ~/.local/bin."""
    return os.environ.get("AGY_BINARY") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")


def _get_binary_signature() -> Optional[Tuple[str, int, int]]:
    """Returns (path, mtime_ns, size) of the agy binary to detect updates without spawning a process."""
    binary = _resolve_agy_binary()
    try:
        st = os.stat(binary)
        return (binary, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


async def _exec_agy_cli(args: List[str], timeout: float) -> Tuple[int, str, str]:
    """Executes the official agy CLI with nice -n 19 and strict Semaphore(1) serialization."""
    binary = _resolve_agy_binary()
    cmd = ["nice", "-n", "19", binary, *args]
    async with SEMAPHORE:
        # Use unlinked temp files so detached child daemons (e.g. MCP helpers) inheriting stdio cannot block EOF
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                )
            except FileNotFoundError:
                return 127, "", f"Antigravity CLI binary not found at: {binary}"
            except OSError as exc:
                return 1, "", f"Failed to start Antigravity CLI: {exc}"

            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, Exception):
                    pass
                raise

            out_f.seek(0)
            err_f.seek(0)
            return (
                returncode or 0,
                out_f.read().decode("utf-8", errors="replace"),
                err_f.read().decode("utf-8", errors="replace"),
            )


def _parse_agy_models(raw_stdout: str) -> List[Dict[str, str]]:
    """Parses `agy models` stdout into a deduplicated list of {id, description} dicts."""
    cleaned = _ANSI_RE.sub("", raw_stdout)
    models: List[Dict[str, str]] = []
    seen = set()

    for raw_line in cleaned.splitlines():
        if "\r" in raw_line:
            raw_line = raw_line.rsplit("\r", 1)[-1]
        line = raw_line.strip()
        if not line:
            continue

        if "\t" in line:
            if line.lower().startswith(("fetching ", "loading ")):
                continue
            model_id, desc = line.split("\t", 1)
            model_id = model_id.strip()
            desc = desc.strip() or model_id
        else:
            parts = line.split(None, 1)
            model_id = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else model_id
            if not _STRUCTURED_ID_RE.match(model_id):
                continue

        if not _MODEL_ID_RE.match(model_id) or model_id in seen:
            continue

        seen.add(model_id)
        models.append({"id": model_id, "description": desc})

    return models


def _with_legacy_aliases(models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Appends OpenAI drop-in aliases if not already present in the live CLI catalog."""
    existing = {m["id"] for m in models}
    combined = list(models)
    for alias in LEGACY_ALIAS_CATALOG:
        if alias["id"] not in existing:
            combined.append(alias)
    return combined


async def _fetch_models_from_cli() -> List[Dict[str, str]]:
    """Fetches live models via `agy models`."""
    try:
        rc, stdout, stderr = await _exec_agy_cli(["models"], timeout=MODELS_FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Timed out running `agy models`")
        return []

    if rc != 0:
        logger.warning("`agy models` failed (code %s): %s", rc, (stderr or stdout).strip())
        return []

    parsed = _parse_agy_models(stdout)
    if not parsed:
        logger.warning("`agy models` returned no parseable models")
    return parsed


def _schedule_background_refresh() -> None:
    """Schedules a single background model refresh without blocking the caller."""
    global _models_bg_task
    if _models_bg_task and not _models_bg_task.done():
        return
    _models_bg_task = asyncio.create_task(get_available_models(force_refresh=True))


async def get_available_models(force_refresh: bool = False) -> List[Dict[str, str]]:
    """Returns available models, auto-refreshing on TTL expiry or agy binary updates."""
    global _models_cache, _models_cache_expires_at, _models_cache_sig, _models_source

    now = time.monotonic()
    sig = _get_binary_signature()
    binary_changed = _models_cache_sig is not None and sig != _models_cache_sig
    cache_valid = bool(_models_cache) and not force_refresh and not binary_changed and now < _models_cache_expires_at

    if cache_valid:
        return _models_cache

    # Serve stale cache immediately if an LLM turn currently holds the semaphore on a routine TTL expiry
    if _models_cache and not force_refresh and not binary_changed and MODELS_CACHE_TTL > 0 and SEMAPHORE.locked():
        _schedule_background_refresh()
        return _models_cache

    async with _models_lock:
        now = time.monotonic()
        sig = _get_binary_signature()
        binary_changed = _models_cache_sig is not None and sig != _models_cache_sig
        if _models_cache and not force_refresh and not binary_changed and now < _models_cache_expires_at:
            return _models_cache

        fetched = await _fetch_models_from_cli()
        if fetched:
            _models_cache = _with_legacy_aliases(fetched)
            _models_cache_sig = sig
            _models_cache_expires_at = time.monotonic() + max(MODELS_CACHE_TTL, 0)
            _models_source = "agy-cli"
            return _models_cache

        if _models_cache:
            _models_cache_expires_at = time.monotonic() + MODELS_RETRY_TTL
            _models_source = "stale-cache"
            return _models_cache

        _models_source = "unavailable"
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "Unable to fetch models from official Antigravity CLI (`agy models`). Ensure `agy` is installed and authenticated.",
                    "type": "service_unavailable",
                }
            },
        )


async def get_agy_version() -> str:
    """Dynamically reads `agy --version`, re-checking if the binary changes on disk."""
    global _version_cache, _version_sig

    sig = _get_binary_signature()
    if _version_cache and sig == _version_sig:
        return _version_cache

    # Avoid stalling /health checks behind an active inference turn
    if _version_cache and SEMAPHORE.locked():
        return _version_cache

    async with _version_lock:
        sig = _get_binary_signature()
        if _version_cache and sig == _version_sig:
            return _version_cache

        try:
            rc, stdout, _ = await _exec_agy_cli(["--version"], timeout=10)
        except asyncio.TimeoutError:
            return _version_cache or "Google Antigravity CLI"

        if rc == 0 and stdout.strip():
            raw_ver = stdout.strip().splitlines()[0].strip().lstrip("v")
            _version_cache = f"Google Antigravity CLI v{raw_ver}"
            _version_sig = sig
            return _version_cache

        return _version_cache or "Google Antigravity CLI"


def get_default_model() -> Optional[str]:
    """Resolves the default Antigravity model dynamically from env or live CLI models."""
    if DEFAULT_MODEL_ENV:
        return DEFAULT_MODEL_ENV

    live_ids = [m["id"] for m in _models_cache if m["id"] not in LEGACY_ALIAS_NAMES]
    if not live_ids:
        return None

    for model_id in live_ids:
        if "flash" in model_id and model_id.endswith("-low"):
            return model_id
    for model_id in live_ids:
        if model_id.endswith("-low"):
            return model_id
    return live_ids[0]


def resolve_model_id(model: Optional[str]) -> Optional[str]:
    """Maps empty/legacy OpenAI model names to the active dynamic default (or None for CLI default)."""
    if not model or model in LEGACY_ALIAS_NAMES:
        return get_default_model()
    return model


def _supports_effort_flag(model_id: str) -> bool:
    """Returns True only when `--effort` will not conflict with the selected model ID."""
    if _EFFORT_SUFFIX_RE.search(model_id) or "orchestrator" in model_id:
        return False
    return True


async def _warmup_cache() -> None:
    """Populates version and dynamic model cache shortly after server boot."""
    try:
        await get_agy_version()
        await get_available_models(force_refresh=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Startup cache warmup failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    warmup_task = asyncio.create_task(_warmup_cache())
    try:
        yield
    finally:
        for task in (warmup_task, _models_bg_task):
            if task and not task.done():
                task.cancel()


app = FastAPI(
    title="Official Google Antigravity OpenAI Bridge",
    description="Local OpenAI-compatible API powered by official Google Antigravity CLI",
    version="1.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"status": "ok", "service": "agy-sidecar"}


def verify_api_key(authorization: Optional[str] = Header(None)):
    """Validates Bearer token if AGY_API_KEY is enforced. Accepts standard format."""
    if not API_KEY_EXPECTED or API_KEY_EXPECTED.lower() in ("any", "none", "*", ""):
        return True

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing Authorization header. Expected: Bearer <API_KEY>", "type": "invalid_request_error"}},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid Authorization header format. Must be Bearer <key>", "type": "invalid_request_error"}},
        )

    token = parts[1]
    # Accept the configured key, or any key matching sk-antigravity* or agy-*
    if (
        token != API_KEY_EXPECTED
        and not token.startswith("sk-antigravity")
        and not token.startswith("agy-")
        and not token.startswith("sk-agy")
    ):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": f"Incorrect API key provided: {token[:8]}...", "type": "invalid_request_error"}},
        )
    return True


async def run_agy_cli(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout: int = CLI_TIMEOUT,
) -> str:
    target_model = resolve_model_id(model)
    args: List[str] = []
    if target_model:
        args.extend(["--model", target_model])
        if effort and _supports_effort_flag(target_model):
            args.extend(["--effort", effort])
    elif effort:
        args.extend(["--effort", effort])
    args.extend(["--disable-slash-commands", "--print", prompt])

    try:
        rc, stdout, stderr = await _exec_agy_cli(args, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Official Antigravity CLI timed out after {timeout} seconds")

    if rc != 0:
        err_msg = stderr.strip() or stdout.strip()
        raise HTTPException(status_code=500, detail=f"Antigravity CLI error (code {rc}): {err_msg}")

    return stdout.strip()


def _extract_text_content(content: Any) -> str:
    """Extracts plain text from OpenAI string or structured content-part arrays."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _format_role_message(role: str, content: Any) -> str:
    role_label = "System" if role == "system" else ("User" if role == "user" else "Assistant")
    return f"{role_label}:\n{_extract_text_content(content)}"


def _to_openai_model_obj(model_info: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": model_info["id"],
        "object": "model",
        "created": 1700000000,
        "owned_by": "google-antigravity",
        "permission": [],
        "root": model_info["id"],
        "parent": None,
    }


# --- Request / Response Models ---

class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    reasoning_effort: Optional[str] = None


# --- Endpoints ---

@app.get("/health")
async def health():
    sdk_version = await get_agy_version()
    return {
        "status": "ok",
        "service": "agy-sidecar (official-google-antigravity)",
        "sdk": sdk_version,
        "binary_path": _resolve_agy_binary(),
        "default_model": get_default_model() or "agy-cli-default",
        "models_count": len(_models_cache),
        "models_source": _models_source,
        "zero_lag": "nice -n 19 + asyncio.Semaphore(1)",
    }


@app.get("/v1/models")
@app.get("/models")
async def list_models(refresh: bool = False, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible models catalog auto-fetched from `agy models`."""
    models = await get_available_models(force_refresh=refresh)
    return {
        "object": "list",
        "data": [_to_openai_model_obj(m) for m in models],
    }


@app.get("/v1/models/{model_id:path}")
@app.get("/models/{model_id:path}")
async def retrieve_model(model_id: str, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible single-model lookup."""
    models = await get_available_models()
    for m in models:
        if m["id"] == model_id:
            return _to_openai_model_obj(m)
    raise HTTPException(
        status_code=404,
        detail={"error": {"message": f"The model '{model_id}' does not exist", "type": "invalid_request_error"}},
    )


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Chat Completions endpoint for n8n OpenAI Chat Model / AI Agent nodes."""
    conversation = [_format_role_message(msg.role, msg.content) for msg in req.messages]
    full_prompt = "\n\n".join(conversation)
    selected_model = req.model or get_default_model() or "default"
    raw_output = await run_agy_cli(full_prompt, model=req.model, effort=req.reasoning_effort)

    return {
        "id": f"chatcmpl-agy-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": selected_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": raw_output,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(full_prompt) // 4,
            "completion_tokens": len(raw_output) // 4,
            "total_tokens": (len(full_prompt) + len(raw_output)) // 4,
        },
    }


@app.post("/v1/responses")
@app.post("/responses")
async def responses_endpoint(req: Request, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Responses API endpoint for latest n8n OpenAI node (v2.3+)."""
    body = await req.json()
    model_name = body.get("model") or get_default_model() or "default"
    instructions = body.get("instructions")
    input_data = body.get("input") or body.get("messages") or []
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None

    conversation = []
    if instructions:
        conversation.append(f"System:\n{instructions}")

    if isinstance(input_data, list):
        for msg in input_data:
            if isinstance(msg, dict):
                conversation.append(_format_role_message(msg.get("role", "user"), msg.get("content", "")))
            elif isinstance(msg, str):
                conversation.append(f"User:\n{msg}")
    elif isinstance(input_data, str):
        conversation.append(f"User:\n{input_data}")

    full_prompt = "\n\n".join(conversation)
    raw_output = await run_agy_cli(full_prompt, model=model_name, effort=effort)

    resp_id = f"resp_{uuid.uuid4().hex[:16]}"
    prompt_tokens = len(full_prompt) // 4
    output_tokens = len(raw_output) // 4

    return {
        "id": resp_id,
        "object": "response",
        "created": int(time.time()),
        "status": "completed",
        "model": model_name,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": raw_output,
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_level="info")
