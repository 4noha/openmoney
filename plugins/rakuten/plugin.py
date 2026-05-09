from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.rakuten.scraper import run as _run

async def _runner():
    return await _run(headless=True)

PLUGIN = ServiceSpec(
    name="rakuten",
    display_name="楽天市場",
    description="注文履歴・領収書 PDF",
    runner=_runner,
    env_keys=[
        EnvKeySpec("RAKUTEN_ID", "ID", type="text"),
        EnvKeySpec("RAKUTEN_PW", "パスワード", type="password"),
    ],
    provided_banks=("楽天市場",),
    history_url="https://order.my.rakuten.co.jp/",
    requires_otp=False,
    requires_user_accept=False,
)
