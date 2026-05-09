"""Mercari (購入/出品) + メルカード (メルペイ翌月払い) 統合プラグイン。

mercari.py と mercard.py は同一の MERCARI_EMAIL/PW 認証 + 永続コンテキストを共有
するため 1 プラグインに統合し、provided_banks で 3 銀行を扱う。
"""
from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.mercari.scraper import run as _run_mercari
from plugins.mercari.mercard import run as _run_mercard


async def _runner():
    # mercari → mercard の順。mercari がパスキー承認 + セッション確立し、
    # mercard はその余韻 (永続コンテキスト) で OTP 不要にログインできる。
    await _run_mercari(headless=True)
    await _run_mercard(headless=True)


PLUGIN = ServiceSpec(
    name="mercari",
    display_name="メルカリ + メルカード",
    description="購入履歴・売上・メルカード明細 (パスキー承認 / セッション共有)",
    runner=_runner,
    env_keys=[
        EnvKeySpec("MERCARI_EMAIL", "メール", type="email"),
        EnvKeySpec("MERCARI_PW", "パスワード", type="password"),
    ],
    provided_banks=("Mercari", "Mercari売上", "メルカード"),
    history_url="https://jp.mercari.com/mypage/purchases",
    requires_otp=False,
    requires_user_accept=True,
)
