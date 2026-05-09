from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.mufg.scraper import run as _run

async def _runner():
    return await _run(headless=True)

PLUGIN = ServiceSpec(
    name="mufg",
    display_name="三菱UFJ銀行",
    description="口座入出金 (Android 承認必要)",
    runner=_runner,
    env_keys=[
        EnvKeySpec("MUFG_ID", "ID", type="text"),
        EnvKeySpec("MUFG_PW", "パスワード", type="password"),
    ],
    provided_banks=("MUFG",),
    history_url="https://direct.bk.mufg.jp/",
    requires_otp=False,
    requires_user_accept=True,
)
