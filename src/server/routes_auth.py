"""二段階認証 OTP 受信・FCM トークン登録・即時実行トリガーのルート群。"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import File, HTTPException, UploadFile
from pydantic import BaseModel

from src.server import app, _get, _set


class TokenBody(BaseModel):
    token: str


class AcceptBody(BaseModel):
    request_id: str


class OtpBody(BaseModel):
    code: str


@app.post("/register-token")
async def register_token(body: TokenBody):
    _set("device_token", body.token)
    _set("token_updated_at", datetime.now().isoformat())
    print(f"[server] FCM token 登録: {body.token[:20]}...")
    return {"status": "ok"}


@app.post("/accept")
async def accept(body: AcceptBody):
    pending = _get("pending_request_id")
    if pending != body.request_id:
        raise HTTPException(status_code=400, detail="request_id mismatch")
    _set("accept_status", "accepted")
    _set("accepted_at", datetime.now().isoformat())
    print(f"[server] Accept受信: {body.request_id}")
    return {"status": "accepted"}


@app.post("/vpass-otp")
async def vpass_otp(body: OtpBody):
    if not body.code or not body.code.strip():
        raise HTTPException(status_code=400, detail="code is required")
    _set("vpass_otp", body.code.strip())
    print(f"[server] VPASS OTP受信: {body.code[:2]}****")
    return {"status": "ok"}


@app.post("/amazon-otp")
async def amazon_otp(body: OtpBody):
    if not body.code or not body.code.strip():
        raise HTTPException(status_code=400, detail="code is required")
    _set("amazon_otp", body.code.strip())
    print(f"[server] Amazon OTP受信: {body.code[:2]}****")
    return {"status": "ok"}


@app.post("/upload-receipt")
async def upload_receipt(file: UploadFile = File(...)):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    image_bytes = await file.read()
    mime = file.content_type or "image/jpeg"
    filename = f"{datetime.now():%Y%m%d_%H%M%S}_{file.filename or 'receipt.jpg'}"
    from src.receipts import process_receipt
    result = process_receipt(image_bytes, filename, mime)
    print(f"[server] レシート受信: {filename} → tx_id={result['transaction_id']}")
    return result


@app.post("/run-now")
async def run_now():
    _set("run_now", "1")
    print("[server] 即時実行リクエスト受信")
    return {"status": "ok"}


@app.get("/status")
async def status():
    # 各 scraper の最終実行状態（daemon が `scraper:<name>:*` キーで記録）
    import sqlite3
    from src.db import DB_PATH
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT key, value FROM daemon_state "
        "WHERE key LIKE 'scrape:%' OR key LIKE 'matching:%'"
    ).fetchall()
    con.close()
    scrapers: dict[str, dict] = {}
    matching: dict[str, dict] = {}
    for r in rows:
        # key 形式: '<group>:<name>:<field>'
        parts = r["key"].split(":", 2)
        if len(parts) != 3:
            continue
        group, name, field = parts
        target = scrapers if group == "scrape" else matching
        target.setdefault(name, {})[field] = r["value"]

    return {
        "pending_request_id": _get("pending_request_id"),
        "accept_status": _get("accept_status"),
        "last_run": _get("last_run"),
        "run_now": _get("run_now"),
        "device_token": (_get("device_token") or "")[:20] + "..." if _get("device_token") else None,
        "scrapers": scrapers,
        "matching": matching,
    }
