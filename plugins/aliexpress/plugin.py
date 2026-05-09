from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.aliexpress.scraper import run as _run

async def _runner():
    return await _run(headless=True)

PLUGIN = ServiceSpec(
    name="aliexpress",
    display_name="AliExpress",
    description="注文履歴・領収書 PDF (headed フォールバック)",
    runner=_runner,
    env_keys=[
        EnvKeySpec("ALIEXPRESS_EMAIL", "メール", type="email"),
        EnvKeySpec("ALIEXPRESS_PW", "パスワード", type="password"),
    ],
    provided_banks=("AliExpress",),
    history_url="https://www.aliexpress.com/p/order/index.html",
    detail_url_template="https://www.aliexpress.com/p/order/detail.html?orderId={order_id}",
    requires_otp=False,
    requires_user_accept=False,
)
