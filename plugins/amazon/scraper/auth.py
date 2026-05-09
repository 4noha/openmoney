"""Amazon ログイン・OTP / 2FA 処理。"""
from __future__ import annotations

import asyncio
import os

from playwright.async_api import Page

from . import _notify


_AUTH_PATTERNS = ("signin", "ap/signin", "ap/mfa", "ax/claim", "ap/cvf", "challenge")
_OTP_PATTERNS  = ("ap/mfa", "ap/cvf", "ax/claim", "challenge", "ap/signin/confirmation")


def _needs_login(url: str) -> bool:
    return any(p in url for p in _AUTH_PATTERNS)


def _on_orders_page(url: str) -> bool:
    from urllib.parse import urlparse
    path = urlparse(url).path
    return "your-orders" in path or "order-history" in path or "gp/css/order" in path


def _needs_otp(url: str) -> bool:
    return any(p in url for p in _OTP_PATTERNS)


async def login(page: Page) -> None:
    """
    メールアドレス・パスワードを入力し、SMS/OTP は FCM 経由で Android から受け取る。
    """
    email = os.environ.get("AMAZON_EMAIL", "")
    password = os.environ.get("AMAZON_PW", "")
    if not email or not password:
        raise RuntimeError("AMAZON_EMAIL / AMAZON_PW が .env に設定されていません")

    try:
        await page.wait_for_selector("input[name='email'], input[type='email']", timeout=10_000)
        await page.fill("input[name='email'], input[type='email']", email)
        for btn in ["input#continue", "input[type='submit']", "button[type='submit']"]:
            try:
                await page.click(btn, timeout=3_000)
                break
            except Exception:
                continue
        await page.wait_for_load_state("load")
    except Exception:
        pass

    try:
        await page.wait_for_selector("input[name='password'], input[type='password']", timeout=10_000)
        await page.fill("input[name='password'], input[type='password']", password)
        for btn in ["input#signInSubmit", "input[type='submit']", "button[type='submit']"]:
            try:
                await page.click(btn, timeout=3_000)
                break
            except Exception:
                continue
        await page.wait_for_load_state("load")
    except Exception:
        pass

    max_attempts = 5
    for _ in range(max_attempts):
        if _on_orders_page(page.url):
            break
        if _needs_otp(page.url):
            await _handle_otp(page)
            await page.wait_for_load_state("load")
        else:
            if not _on_orders_page(page.url):
                print("Amazon: ブラウザで残りの認証を完了してください（最大10分）...")
                _notify("Amazon の認証が必要です。ブラウザで完了してください。")
                try:
                    await page.wait_for_function(
                        """() => {
                            const path = window.location.pathname;
                            return path.includes('your-orders') || path.includes('order-history')
                                || path.includes('gp/css/order') || location.href.includes('ap/mfa')
                                || location.href.includes('ap/cvf');
                        }""",
                        timeout=600_000,
                    )
                except Exception:
                    pass
            break

    print(f"ログイン後URL: {page.url}")


async def _handle_otp(page: Page) -> None:
    print("Amazon: SMS / OTP 認証が必要です")
    _notify("Amazon の認証コードを入力してください")
    _push_otp_request()

    otp = await _wait_for_otp()

    for sel in [
        "input[name='otpCode']",
        "input[name='code']",
        "input[id*='otp' i]",
        "input[id*='auth-mfa' i]",
        "input[type='tel']",
        "input[type='number']",
        "input[maxlength='6']",
        "input[maxlength='8']",
    ]:
        try:
            await page.fill(sel, otp, timeout=3_000)
            break
        except Exception:
            continue

    for btn in ["input#auth-signin-button", "input[type='submit']", "button[type='submit']"]:
        try:
            await page.click(btn, timeout=3_000)
            break
        except Exception:
            continue


def _push_otp_request() -> None:
    try:
        from src.fcm import send
        from src.server import _get
        token = _get("device_token")
        if token:
            send(token, title="Amazon 認証コード", body="SMS の認証コードをアプリで入力してください",
                 data={"type": "otp_request", "service": "amazon"})
    except Exception:
        pass


async def _wait_for_otp() -> str:
    import sys
    try:
        if sys.stdin.isatty():
            code = input("Amazon 認証コードを入力してください: ").strip()
            if code:
                return code
    except EOFError:
        pass

    print("Amazon OTP待機中（最大5分）... Android から /amazon-otp へ POST してください")
    try:
        from src.server import _get, _set
    except ImportError:
        raise RuntimeError("サーバーモジュールが読み込めません")

    deadline = asyncio.get_event_loop().time() + 300
    while asyncio.get_event_loop().time() < deadline:
        otp = _get("amazon_otp")
        if otp:
            _set("amazon_otp", "")
            print("  Amazon OTP受信（サーバー経由）")
            return otp
        await asyncio.sleep(5)

    raise RuntimeError("Amazon OTP入力タイムアウト（5分）")
