from __future__ import annotations
from src.plugin_api import ServiceSpec
from plugins.deepl.scraper import run as _run


async def _runner():
    return await _run(days=365)


PLUGIN = ServiceSpec(
    name="deepl",
    display_name="DeepL Pro",
    description=(
        "no-reply@deepl.com からの DeepL SE 領収書メールを Gmail から取得 → "
        "PDF 保存 + 金額・通貨・領収書番号を transactions に記録 "
        "(= bank='DeepL'、 通貨は JPY なら円、 USD/EUR ならセント単位)。 "
        "Gmail 認証は gmail plugin で管理"
    ),
    runner=_runner,
    env_keys=[],
    depends_on=("gmail",),
    provided_banks=("DeepL",),
    history_url="https://www.deepl.com/account",
    requires_otp=False,
    requires_user_accept=False,
)
