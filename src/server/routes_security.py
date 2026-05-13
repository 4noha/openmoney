"""ログインゲート + .env 暗号化 API + /login /settings ページ。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from src.server import app
from src import security

_DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")

# デモモード時にブロックするパス (POST/PUT/PATCH/DELETE のみ)
_DEMO_READONLY_PREFIXES = (
    "/api/services/",
    "/api/user-tags",
    "/api/security/encrypt",
    "/api/security/keychain",
    "/api/security/change-password",
    "/api/scrape/",   # 手動スクレイプトリガー
    "/run-now",       # daemon 即時実行フラグ
    "/api/aoiro/",    # 青色申告
    "/api/tax/",      # インボイス管理
)


_TEMPLATES = Path(__file__).parent / "templates"

# 認証不要のパス（解錠前でもアクセス可）
_PUBLIC_PATHS = {
    "/login",
    "/api/ping",
    "/api/security/status",
    "/api/security/unlock",
    "/api/security/lock",
}
_PUBLIC_PREFIXES = ("/static/",)


class UnlockBody(BaseModel):
    password: str


class EncryptKeyBody(BaseModel):
    key: str


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class LoginGateMiddleware(BaseHTTPMiddleware):
    """暗号化済 *_EMAIL がある (=セットアップ済) のに未解錠なら /login へ。
    - .env が空 / *_EMAIL 未登録 → /login で「.env に EMAIL を登録」案内
    - すでに解錠済                 → そのまま通す
    HTML ページはリダイレクト、API は 401。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        if security.is_unlocked():
            return await call_next(request)
        # 未解錠: *_EMAIL があれば必ず /login 経由で解錠してもらう
        # (暗号化済なら verify、平文だけなら bootstrap でこのパスワードで暗号化)
        # *_EMAIL がそもそも無ければ /login が「.env 登録案内」を出す
        accept = request.headers.get("accept", "")
        if path.startswith("/api/") or "json" in accept:
            # Starlette middleware は HTTPException を catch しないので Response 直返
            return JSONResponse({"detail": "unlock required"}, status_code=401)
        # next: 解錠後に元のページに戻れるよう pathname + query を持たせる。
        # クエリ・フラグメントを含む URL を url-encode する (open redirect 防止のため
        # サーバ側では使わず、login.html の JS が同一オリジン path として検証する)。
        from urllib.parse import quote
        original = request.url.path
        if request.url.query:
            original += "?" + request.url.query
        return RedirectResponse(url=f"/login?next={quote(original, safe='')}", status_code=302)


app.add_middleware(LoginGateMiddleware)


class DemoReadOnlyMiddleware(BaseHTTPMiddleware):
    """デモモードで設定系 API への書き込みをブロック。"""
    async def dispatch(self, request: Request, call_next):
        if not _DEMO_MODE or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) or path == p.rstrip("/") for p in _DEMO_READONLY_PREFIXES):
            return JSONResponse(
                {"detail": "デモモードでは設定の変更はできません"},
                status_code=403,
            )
        return await call_next(request)


if _DEMO_MODE:
    app.add_middleware(DemoReadOnlyMiddleware)


# ─────────────────────────────────────────────
# ページ
# ─────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return (_TEMPLATES / "login.html").read_text()


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    from src.server.templates_helper import render
    return render("settings.html")


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

@app.get("/api/security/status")
async def api_security_status():
    return {
        "has_email": security.has_email_in_env(),
        "has_encrypted_email": security.has_encrypted_email(),
        "unlocked": security.is_unlocked(),
        "plaintext_keys": security.plaintext_keys() if security.is_unlocked() else [],
        "keys": security.env_keys_status(),
        "keychain_available": security.keychain_available(),
        "keychain_has_master": security.keychain_has_master(),
        "demo_mode": _DEMO_MODE,
        "demo_next_reset_at": os.environ.get("DEMO_NEXT_RESET_AT") if _DEMO_MODE else None,
    }


@app.post("/api/security/keychain-save")
async def api_security_keychain_save():
    """現在解錠中の master password を OS Keychain に保存 (#24)。
    daemon 再起動時に自動解錠されるようになる。"""
    if not security.is_master_loaded():
        raise HTTPException(status_code=403, detail="まず unlock してください")
    if not security.keychain_available():
        raise HTTPException(status_code=400, detail="OS Keychain が利用できません")
    pw = security.get_master()
    ok = security.keychain_set_master(pw)
    if not ok:
        raise HTTPException(status_code=500, detail="Keychain 書込に失敗")
    return {"status": "ok"}


@app.post("/api/security/keychain-delete")
async def api_security_keychain_delete():
    """OS Keychain から master password を削除 (#24)。
    自動解錠を解除する (UI ロックそのものはこの API では変えない)。"""
    if not security.keychain_available():
        raise HTTPException(status_code=400, detail="OS Keychain が利用できません")
    ok = security.keychain_delete_master()
    return {"status": "ok" if ok else "not_found"}


@app.post("/api/security/change-password")
async def api_security_change_password(body: ChangePasswordBody):
    """master password 変更。.env 全暗号化 entry を old → new で再暗号化、
    Keychain に保存されていれば new で更新。解錠中のみ呼べる。"""
    if not security.is_master_loaded():
        raise HTTPException(status_code=403, detail="まず unlock してください")
    try:
        n = security.change_master_password(body.old_password, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "ok", "rewritten": n}


@app.post("/api/security/unlock")
async def api_security_unlock(body: UnlockBody):
    """パスワード入力。暗号化された *_EMAIL を復号して email 形式なら成功。"""
    try:
        security.unlock(body.password)
    except RuntimeError as e:
        # *_EMAIL 自体が無い
        raise HTTPException(status_code=409, detail=str(e))
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "ok"}


@app.post("/api/security/lock")
async def api_security_lock():
    security.lock_master()
    return {"status": "ok"}


@app.post("/api/security/encrypt-key")
async def api_security_encrypt_key(body: EncryptKeyBody):
    try:
        ok = security.encrypt_env_key(body.key)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "ok" if ok else "skipped"}


@app.post("/api/security/encrypt-all-plaintext")
async def api_security_encrypt_all():
    """残っている平文値を一括暗号化。"""
    try:
        keys = security.plaintext_keys()
        for k in keys:
            security.encrypt_env_key(k)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "ok", "encrypted": keys}
