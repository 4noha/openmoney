from __future__ import annotations
from src.plugin_api import ServiceSpec
from plugins.googleplay.scraper import run as _run


async def _runner():
    return await _run(days=365)


PLUGIN = ServiceSpec(
    name="googleplay",
    display_name="Google Play",
    description=(
        "googleplay-noreply@google.com の「Google Play のご注文明細」 メールを Gmail から "
        "取得 → アプリ名 / 開発元 / 注文 ID / 金額を transactions に記録 (= bank='Google Play')。 "
        "Google G.K. 等の日本法人発行のものは適格事業者番号 T が付与され、 海外法人は NULL。 "
        "Gmail 認証は gmail plugin で管理"
    ),
    runner=_runner,
    env_keys=[],
    depends_on=("gmail",),
    provided_banks=("Google Play",),
    history_url="https://play.google.com/store/account/orderhistory",
    requires_otp=False,
    requires_user_accept=False,
)
