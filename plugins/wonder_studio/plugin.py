from __future__ import annotations
from src.plugin_api import ServiceSpec
from plugins.wonder_studio.scraper import run as _run


async def _runner():
    return await _run(days=365)


PLUGIN = ServiceSpec(
    name="wonder_studio",
    display_name="Wonder Studio (Wonder Dynamics)",
    description=(
        "Stripe 経由の Wonder Dynamics 領収書メールを Gmail から取得 → PDF 保存 + "
        "USD amount を transactions に記録 (= bank='Wonder Studio'、 セント単位)。 "
        "Gmail 認証は gmail plugin で管理"
    ),
    runner=_runner,
    env_keys=[],  # 認証は gmail plugin に集約
    depends_on=("gmail",),
    provided_banks=("Wonder Studio",),
    history_url="https://wonderdynamics.com/",
    requires_otp=False,
    requires_user_accept=False,
)
