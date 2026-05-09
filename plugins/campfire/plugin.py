from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.campfire.scraper import run as _run

async def _runner():
    return await _run(headless=True)

PLUGIN = ServiceSpec(
    name="campfire",
    display_name="CAMPFIRE",
    description="支援履歴・領収書",
    runner=_runner,
    env_keys=[
        EnvKeySpec("CAMPFIRE_EMAIL", "メール", type="email"),
        EnvKeySpec("CAMPFIRE_PW", "パスワード", type="password"),
    ],
    provided_banks=("CAMPFIRE",),
    history_url="https://camp-fire.jp/mypage/backers",
    detail_url_template="https://camp-fire.jp/mypage/backers/{order_id}",
    requires_otp=False,
    requires_user_accept=False,
)
