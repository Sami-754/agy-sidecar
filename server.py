#!/usr/bin/env python3
"""
agy-sidecar: Official Google Antigravity OpenAI-Compatible API Bridge
Uses official Antigravity CLI (~/.local/bin/agy or system PATH) with zero unofficial scraping.
Exposes standard OpenAI endpoints on 0.0.0.0:8000 (accessible via localhost & host.docker.internal).
"""

import sys
import os
import json
import re
import time
import uuid
import asyncio
import shutil
from typing import List, Optional, Dict, Any

AGY_BINARY = os.environ.get("AGY_BINARY") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Supported models via official Antigravity CLI
OFFICIAL_MODELS = [
    {"id": "gemini-3.8-flash-low", "description": "Google Gemini 3.8 Flash (Low Effort) - Recommended default for speed & precision"},
    {"id": "gemini-3.8-flash-high", "description": "Google Gemini 3.8 Flash (High Effort) - Deep reasoning"},
    {"id": "gemini-4-argon-low", "description": "Google Gemini 4 Argon (Low Effort)"},
    {"id": "gemini-4-argon-medium", "description": "Google Gemini 4 Argon (Medium Effort)"},
    {"id": "claude-sonnet-5-5-low", "description": "Claude Sonnet 5.5 (Low Effort)"},
    {"id": "gpt-4o", "description": "Alias mapped directly to gemini-3.8-flash-low for legacy OpenAI drop-in"},
    {"id": "gpt-4o-mini", "description": "Alias mapped directly to gemini-3.8-flash-low"}
]

MODEL_MAP = {
    "gpt-4o": "gemini-3.8-flash-low",
    "gpt-4o-mini": "gemini-3.8-flash-low",
    "gpt-4": "gemini-3.8-flash-low",
    "gpt-3.5-turbo": "gemini-3.8-flash-low",
    "default": "gemini-3.8-flash-low"
}

API_KEY_EXPECTED = os.environ.get("AGY_API_KEY", "sk-antigravity-local")

app = FastAPI(
    title="Official Google Antigravity OpenAI Bridge",
    description="Local OpenAI-compatible API powered by official Google Antigravity CLI",
    version="1.1.0"
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

# Concurrency lock: maximum 1 agy execution at a time with nice -n 19 to guarantee ZERO PC lag
SEMAPHORE = asyncio.Semaphore(1)

def verify_api_key(authorization: Optional[str] = Header(None)):
    """Validates Bearer token if AGY_API_KEY is enforced. Accepts standard format."""
    if not API_KEY_EXPECTED or API_KEY_EXPECTED.lower() in ("any", "none", "*", ""):
        return True
    
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing Authorization header. Expected: Bearer <API_KEY>", "type": "invalid_request_error"}}
        )
    
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid Authorization header format. Must be Bearer <key>", "type": "invalid_request_error"}}
        )
    
    token = parts[1]
    # Accept the configured key, or any key matching sk-antigravity* or agy-*
    if token != API_KEY_EXPECTED and not token.startswith("sk-antigravity") and not token.startswith("agy-") and not token.startswith("sk-agy"):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": f"Incorrect API key provided: {token[:8]}...", "type": "invalid_request_error"}}
        )
    return True

async def run_agy_cli(prompt: str, model: str = "gemini-3.8-flash-low", effort: str = "low", timeout: int = 90) -> str:
    target_model = MODEL_MAP.get(model, model)
    cmd = [
        "nice", "-n", "19",
        AGY_BINARY,
        "--model", target_model,
        "--effort", effort,
        "--disable-slash-commands",
        "--print", prompt
    ]
    async with SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, Exception):
                pass
            raise HTTPException(status_code=504, detail="Official Antigravity CLI timed out after 90 seconds")

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise HTTPException(status_code=500, detail=f"Antigravity CLI error (code {proc.returncode}): {err_msg}")

        return stdout.decode().strip()

# --- Request / Response Models ---

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "gemini-3.8-flash-low"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

# --- Endpoints ---

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "agy-sidecar (official-google-antigravity)",
        "sdk": "Google Antigravity CLI v1.5.7",
        "binary_path": AGY_BINARY,
        "default_model": "gemini-3.8-flash-low",
        "zero_lag": "nice -n 19 + asyncio.Semaphore(1)"
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models(_: bool = Depends(verify_api_key)):
    """OpenAI-compatible models catalog so n8n populates its model dropdown."""
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": "google-antigravity",
                "permission": [],
                "root": m["id"],
                "parent": None
            }
            for m in OFFICIAL_MODELS
        ]
    }

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Chat Completions endpoint for n8n OpenAI Chat Model / AI Agent nodes."""
    conversation = []
    for msg in req.messages:
        role_label = "System" if msg.role == "system" else ("User" if msg.role == "user" else "Assistant")
        conversation.append(f"{role_label}:\n{msg.content}")
    
    full_prompt = "\n\n".join(conversation)
    raw_output = await run_agy_cli(full_prompt, model=req.model)

    return {
        "id": f"chatcmpl-agy-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": raw_output
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": len(full_prompt) // 4,
            "completion_tokens": len(raw_output) // 4,
            "total_tokens": (len(full_prompt) + len(raw_output)) // 4
        }
    }

@app.post("/v1/responses")
@app.post("/responses")
async def responses_endpoint(req: Request, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Responses API endpoint for latest n8n OpenAI node (v2.3+)."""
    body = await req.json()
    model_name = body.get("model") or "gemini-3.8-flash-low"
    instructions = body.get("instructions")
    input_data = body.get("input") or body.get("messages") or []

    conversation = []
    if instructions:
        conversation.append(f"System:\n{instructions}")

    if isinstance(input_data, list):
        for msg in input_data:
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                c = msg.get("content", "")
                if isinstance(c, list):
                    text_parts = []
                    for part in c:
                        if isinstance(part, dict) and "text" in part:
                            text_parts.append(part["text"])
                        elif isinstance(part, str):
                            text_parts.append(part)
                    text = "\n".join(text_parts)
                elif isinstance(c, str):
                    text = c
                else:
                    text = str(c)
                role_label = "System" if role == "system" else ("User" if role == "user" else "Assistant")
                conversation.append(f"{role_label}:\n{text}")
            elif isinstance(msg, str):
                conversation.append(f"User:\n{msg}")
    elif isinstance(input_data, str):
        conversation.append(f"User:\n{input_data}")

    full_prompt = "\n\n".join(conversation)
    raw_output = await run_agy_cli(full_prompt, model=model_name)

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
                        "text": raw_output
                    }
                ]
            }
        ],
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_level="info")
