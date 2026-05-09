"""Local mock AI proxy — OpenMoney Pro plan (Haiku 4.5 only) をシミュレートするローカル proxy。

開発・初期セットアップ専用。本番の openmoney-backend は使わない。

仕様:
  - auth/billing チェックなし (= any bearer token を通す)
  - Haiku 4.5 のみ許可 (= Pro plan 相当)、それ以外は 403
  - ANTHROPIC_API_KEY を使って直接 upstream へ転送
  - streaming / non-streaming 両対応
  - usage を stdout に出力

Usage:
    uv run scripts/mock_ai_proxy.py
    # or
    .venv/bin/python3 scripts/mock_ai_proxy.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# プロジェクトルートの .env を読む
load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE = "https://api.anthropic.com"
PORT = int(os.environ.get("MOCK_PROXY_PORT", "8082"))
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Haiku 4.5 の許可 alias 一覧
_HAIKU_ALIASES = {
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
}

app = FastAPI(title="OpenMoney local mock AI proxy", version="dev")


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────

def _err(status: int, err_type: str, message: str, **extra) -> JSONResponse:
    body = {"type": "error", "error": {"type": err_type, "message": message, **extra}}
    return JSONResponse(status_code=status, content=body)


def _is_haiku(model: str) -> bool:
    return model.lower().replace("_", "-") in _HAIKU_ALIASES or "haiku" in model.lower()


def _build_headers(req: Request) -> dict[str, str]:
    headers: dict[str, str] = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": req.headers.get("anthropic-version", DEFAULT_ANTHROPIC_VERSION),
        "content-type": "application/json",
        "accept": req.headers.get("accept", "application/json"),
    }
    if beta := req.headers.get("anthropic-beta"):
        headers["anthropic-beta"] = beta
    return headers


_PASS_HEADERS = {
    "content-type", "request-id",
    "anthropic-ratelimit-requests-limit", "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining", "anthropic-ratelimit-tokens-reset",
    "retry-after",
}


def _filter(upstream_headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in upstream_headers.items() if k.lower() in _PASS_HEADERS}


def _log_usage(model: str, usage: dict) -> None:
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cr  = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
    print(f"[mock-proxy] {model}  in={inp} out={out} cache_read={cr}", flush=True)


_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


# ─────────────────────────────────────────────
# /v1/messages
# ─────────────────────────────────────────────

@app.post("/v1/messages")
async def v1_messages(
    request: Request,
    x_api_key: str | None = Header(None, alias="x-api-key"),
    authorization: str | None = Header(None),
):
    if not ANTHROPIC_API_KEY:
        return _err(500, "configuration_error",
                    "ANTHROPIC_API_KEY not set in .env — run setup_claude.sh first")

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _err(400, "invalid_request_error", "invalid JSON body")

    model = str(body.get("model") or "")
    if not model:
        return _err(400, "invalid_request_error", "missing 'model' in request body")

    if not _is_haiku(model):
        return _err(
            403, "model_not_allowed",
            f"[mock-proxy] {model} は Pro plan では利用不可。"
            f"Haiku 4.5 (claude-haiku-4-5-20251001) を使ってください。",
        )

    upstream_headers = _build_headers(request)
    is_stream = bool(body.get("stream", False))

    if not is_stream:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{ANTHROPIC_BASE}/v1/messages",
                headers=upstream_headers,
                content=body_bytes,
            )
            if r.status_code == 200:
                try:
                    _log_usage(model, r.json().get("usage") or {})
                except Exception:
                    pass
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "application/json"),
                headers=_filter(r.headers),
            )

    # streaming
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        upstream_req = client.build_request(
            "POST", f"{ANTHROPIC_BASE}/v1/messages",
            headers=upstream_headers, content=body_bytes,
        )
        upstream = await client.send(upstream_req, stream=True)
    except Exception:
        await client.aclose()
        raise

    if upstream.status_code != 200:
        err_body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=err_body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=_filter(upstream.headers),
        )

    async def passthrough() -> AsyncIterator[bytes]:
        accumulated: dict = {}
        buffer = b""
        try:
            async for chunk in upstream.aiter_raw():
                buffer += chunk
                yield chunk
                while True:
                    idx = buffer.find(b"\n\n")
                    if idx < 0:
                        break
                    event_block, buffer = buffer[:idx], buffer[idx + 2:]
                    _update_usage(event_block, accumulated)
        finally:
            await upstream.aclose()
            await client.aclose()
            if accumulated:
                _log_usage(model, accumulated)

    return StreamingResponse(
        passthrough(),
        status_code=200,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
        headers=_filter(upstream.headers),
    )


def _update_usage(event_block: bytes, acc: dict) -> None:
    text = event_block.decode("utf-8", errors="replace")
    event_type = data = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
    if not data or event_type not in {"message_start", "message_delta"}:
        return
    try:
        obj = json.loads(data)
    except Exception:
        return
    if event_type == "message_start":
        usage = (obj.get("message") or {}).get("usage") or {}
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if k in usage:
                acc[k] = int(usage[k] or 0)
    elif event_type == "message_delta":
        if "output_tokens" in (obj.get("usage") or {}):
            acc["output_tokens"] = int(obj["usage"]["output_tokens"] or 0)


# ─────────────────────────────────────────────
# /v1/messages/count_tokens
# ─────────────────────────────────────────────

@app.post("/v1/messages/count_tokens")
async def v1_count_tokens(
    request: Request,
    x_api_key: str | None = Header(None, alias="x-api-key"),
    authorization: str | None = Header(None),
):
    if not ANTHROPIC_API_KEY:
        return _err(500, "configuration_error", "ANTHROPIC_API_KEY not set in .env")
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _err(400, "invalid_request_error", "invalid JSON body")
    if model := body.get("model"):
        if not _is_haiku(str(model)):
            return _err(403, "model_not_allowed",
                        f"[mock-proxy] {model} は Pro plan では利用不可")
    upstream_headers = _build_headers(request)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.post(
            f"{ANTHROPIC_BASE}/v1/messages/count_tokens",
            headers=upstream_headers, content=body_bytes,
        )
        return Response(
            content=r.content, status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
            headers=_filter(r.headers),
        )


# ─────────────────────────────────────────────
# /v1/models  (Claude Code が叩く場合の stub)
# ─────────────────────────────────────────────

@app.get("/v1/models")
async def v1_models():
    return {
        "data": [
            {"id": "claude-haiku-4-5-20251001", "type": "model", "display_name": "Claude Haiku 4.5"},
        ]
    }


# ─────────────────────────────────────────────
# /health
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    key_ok = bool(ANTHROPIC_API_KEY)
    return {"status": "ok", "proxy": "mock", "anthropic_key_set": key_ok, "port": PORT}


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が .env に設定されていません", file=sys.stderr)
        sys.exit(1)
    print(f"[mock-proxy] 起動 → http://localhost:{PORT}  (Haiku 4.5 only, dev mode)")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
