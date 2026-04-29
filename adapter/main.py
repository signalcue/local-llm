from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="SharpCue Local LLM Adapter")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5-coder:32b")
PARALLEL_MODE = os.getenv("PARALLEL_MODE", "true").lower() == "true"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "14"))
ENV_NAME = os.getenv("ENV_NAME", "dev")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1024"))


def _now() -> int:
    return int(time.time())


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n".join(chunks).strip()
    return str(content)


def _to_ollama_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    system_msg = payload.get("system")
    if isinstance(system_msg, str) and system_msg.strip():
        out.append({"role": "system", "content": system_msg.strip()})

    for msg in payload.get("messages", []) or []:
        role = msg.get("role", "user")
        content = _flatten_content(msg.get("content", ""))
        out.append({"role": role, "content": content})

    return out


def _resolve_local_model(requested_model: Any) -> str:
    if not isinstance(requested_model, str) or not requested_model.strip():
        return MODEL_NAME

    normalized = requested_model.strip().lower()
    # Anthropic-compatible callers will send Claude model ids that Ollama cannot load.
    # Route those requests to the configured local model transparently.
    if normalized.startswith("claude"):
        return MODEL_NAME

    return requested_model.strip()


async def _push_comparison(entry: dict[str, Any]) -> None:
    redis = aioredis.from_url(REDIS_URL)
    await redis.rpush("comparisons", json.dumps(entry, ensure_ascii=True))
    await redis.expire("comparisons", LOG_RETENTION_DAYS * 86400)
    await redis.aclose()


def _extract_market_hint(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    market_id = metadata.get("market_id") or metadata.get("ticker") or metadata.get("event_id")
    source_app = metadata.get("source_app") or metadata.get("source")
    request_tag = metadata.get("request_tag")
    analysis_run_id = (
        metadata.get("analysis_run_id")
        or request.headers.get("x-analysis-run-id")
        or payload.get("analysis_run_id")
        or request_tag
    )
    return {
        "market_id": market_id,
        "source_app": source_app,
        "request_tag": request_tag,
        "analysis_run_id": analysis_run_id,
    }


async def _call_claude_and_log(
    original_payload: dict[str, Any],
    local_response: dict[str, Any],
    request_id: str,
    requested_model: str,
    local_model: str,
    local_latency_ms: int,
    local_tokens: dict[str, int],
    source_hint: dict[str, Any],
) -> None:
    entry: dict[str, Any] = {
        "record_version": 3,
        "timestamp": _now(),
        "env_name": ENV_NAME,
        "request_id": request_id,
        "analysis_run_id": source_hint.get("analysis_run_id"),
        "market_id": source_hint.get("market_id"),
        "source_app": source_hint.get("source_app"),
        "request_tag": source_hint.get("request_tag"),
        "requested_model": requested_model,
        "local_model": local_model,
        "local_latency_ms": local_latency_ms,
        "local_status": "success",
        "local_input_tokens": local_tokens.get("input_tokens", 0),
        "local_output_tokens": local_tokens.get("output_tokens", 0),
        "claude_status": "not_started",
        "claude_latency_ms": None,
        "prompt": original_payload,
        "local_response": local_response,
        "claude_response": None,
        "claude_error": None,
    }

    if not ANTHROPIC_API_KEY:
        entry["claude_status"] = "skipped_missing_key"
        entry["claude_error"] = "ANTHROPIC_API_KEY is not set"
        await _push_comparison(entry)
        return

    claude_start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=original_payload,
            )
            resp.raise_for_status()
            entry["claude_response"] = resp.json()
            entry["claude_status"] = "success"
            entry["claude_http_status"] = resp.status_code
    except Exception as exc:
        entry["claude_status"] = "error"
        entry["claude_error"] = str(exc)
    finally:
        entry["claude_latency_ms"] = int((time.monotonic() - claude_start) * 1000)

    await _push_comparison(entry)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "parallel_mode": PARALLEL_MODE,
        "model": MODEL_NAME,
        "ollama_host": OLLAMA_HOST,
    }


@app.post("/v1/messages")
async def messages(request: Request) -> JSONResponse:
    request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:20]}"
    request_start = time.monotonic()
    payload = await request.json()
    requested_model = str(payload.get("model") or MODEL_NAME)
    local_model = _resolve_local_model(requested_model)
    source_hint = _extract_market_hint(payload, request)

    ollama_messages = _to_ollama_messages(payload)
    if not ollama_messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    ollama_payload = {
        "model": local_model,
        "messages": ollama_messages,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": OLLAMA_TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=ollama_payload)
            resp.raise_for_status()
            ollama_data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ollama request failed: {exc}") from exc

    content = ((ollama_data.get("message") or {}).get("content") or "").strip()
    # Strip <think>...</think> blocks emitted by reasoning models (e.g. qwq, deepseek-r1)
    # before the JSON payload so downstream parsers see clean output.
    import re as _re
    content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
    prompt_tokens = int(ollama_data.get("prompt_eval_count") or 0)
    completion_tokens = int(ollama_data.get("eval_count") or 0)
    local_latency_ms = int((time.monotonic() - request_start) * 1000)

    anthropic_response = {
        "id": f"msg_local_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model or local_model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        },
    }

    if PARALLEL_MODE:
        asyncio.create_task(
            _call_claude_and_log(
                original_payload=payload,
                local_response=anthropic_response,
                request_id=request_id,
                requested_model=requested_model,
                local_model=local_model,
                local_latency_ms=local_latency_ms,
                local_tokens={
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                },
                source_hint=source_hint,
            )
        )

    return JSONResponse(content=anthropic_response)
