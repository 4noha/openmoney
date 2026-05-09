"""VPASS (三井住友カード) プラグイン manifest。"""
from __future__ import annotations

from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.vpass.scraper import run as _vpass_run


async def _runner():
    return await _vpass_run(headless=True)


PLUGIN = ServiceSpec(
    name="vpass",
    display_name="VPASS (三井住友カード)",
    description="三井住友カード明細・利用履歴",
    runner=_runner,
    env_keys=[
        EnvKeySpec("VPASS_ID", "ID", type="text"),
        EnvKeySpec("VPASS_PW", "パスワード", type="password"),
    ],
    provided_banks=("VPASS",),
    history_url="https://www.smbc-card.com/memapp/MIDdef02_001/MID3401_001CardEachPaymentTransition.do",
    requires_otp=False,           # SMS OTP は Android アプリ経由で自動処理
    requires_user_accept=False,
)
