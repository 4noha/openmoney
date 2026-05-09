from __future__ import annotations
from src.plugin_api import ServiceSpec
from plugins.discord.scraper import run as _run


async def _runner():
    return await _run(days=365)


PLUGIN = ServiceSpec(
    name="discord",
    display_name="Discord",
    description=(
        "noreply@discord.com の領収書メールを Gmail から取得 → 金額・日付・プラン名を "
        "transactions に記録 (= bank='Discord')。 Discord は米国法人なので適格事業者番号は "
        "発行されず、 仕入税額控除対象外 (= /invoices には出ない)。 Gmail 認証は gmail plugin で管理"
    ),
    runner=_runner,
    env_keys=[],
    depends_on=("gmail",),
    provided_banks=("Discord",),
    history_url="https://discord.com/billing",
    requires_otp=False,
    requires_user_accept=False,
)
