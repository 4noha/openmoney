from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.gmail.scraper import run as _run


async def _runner():
    return await _run(days=30)


PLUGIN = ServiceSpec(
    name="gmail",
    display_name="Gmail (明細メール)",
    description="Gmail から各サービスの明細通知メールを取得 (IMAP + LLM パース)",
    runner=_runner,
    env_keys=[
        EnvKeySpec(
            "GMAIL_USER", "Gmail アドレス", type="text",
            placeholder="example@gmail.com",
            pattern=r"[^@\s]+@[^@\s]+\.[^@\s]+",
            pattern_error="メールアドレス形式で入力してください (例: example@gmail.com)",
        ),
        EnvKeySpec(
            "GMAIL_APP_PASSWORD", "Gmail アプリパスワード", type="password",
            placeholder="xxxx xxxx xxxx xxxx (16 文字)",
            help="https://myaccount.google.com/apppasswords で発行した 16 文字英小文字 (= 通常の Google パスワードでは接続不可)",
            pattern=r"[a-z]{4}\s?[a-z]{4}\s?[a-z]{4}\s?[a-z]{4}",
            pattern_error="アプリパスワードは英小文字 16 文字です。 https://myaccount.google.com/apppasswords で発行してください",
        ),
    ],
    provided_banks=("Gmail",),  # 'Gmail:楽天カード' のように prefix される
    history_url="https://myaccount.google.com/apppasswords",
    requires_otp=False,
    requires_user_accept=False,
)
