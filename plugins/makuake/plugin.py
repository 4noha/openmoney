from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.makuake.scraper import run as _run

async def _runner():
    return await _run(headless=True)

PLUGIN = ServiceSpec(
    name="makuake",
    display_name="Makuake",
    description="応援購入履歴",
    runner=_runner,
    env_keys=[
        EnvKeySpec("MAKUAKE_EMAIL", "メール", type="email"),
        EnvKeySpec("MAKUAKE_PW", "パスワード", type="password"),
    ],
    provided_banks=("Makuake",),
    history_url="https://www.makuake.com/my/project/favorite/",
    requires_otp=False,
    requires_user_accept=False,
)
