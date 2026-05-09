from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.vidiq.scraper import run as _run


async def _runner():
    return await _run(headless=True)


PLUGIN = ServiceSpec(
    name="vidiq",
    display_name="vidIQ",
    description=(
        "vidIQ (https://app.vidiq.com) にログインして領収書ページを探索し、 各 invoice "
        "を PDF 化 → transactions に bank='vidIQ' で記録 (= USD subscription、 米国法人で "
        "適格事業者番号なし)。 VPASS の「PAYPAL *VIDIQ」 行に receipt を流用 link"
    ),
    runner=_runner,
    env_keys=[
        EnvKeySpec(
            "VIDIQ_EMAIL", "vidIQ ログイン Email", type="email",
            placeholder="user@example.com",
        ),
        EnvKeySpec(
            "VIDIQ_PW", "vidIQ パスワード", type="password",
        ),
    ],
    provided_banks=("vidIQ",),
    history_url="https://app.vidiq.com/account/billing",
    requires_otp=False,
    requires_user_accept=False,
)
