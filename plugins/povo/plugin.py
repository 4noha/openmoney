from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.povo.scraper import run as _run


async def _runner():
    return await _run(days=90)


PLUGIN = ServiceSpec(
    name="povo",
    display_name="povo (KDDI)",
    description="povo の請求書 PDF (Gmail 添付) を取得 → パスワード復号 → DB 保存。Gmail 認証は gmail plugin で設定。",
    runner=_runner,
    env_keys=[
        EnvKeySpec(
            "POVO_PDF_PASSWORD",
            "povo PDF パスワード",
            type="password",
            placeholder="誕生日 YYYY-MM-DD",
            help="例: 1991 年 5 月 2 日生まれなら 1991-05-02",
            pattern=r"\d{4}-\d{2}-\d{2}",
            pattern_error="誕生日を YYYY-MM-DD 形式で入力してください (例: 1991-05-02)",
        ),
        EnvKeySpec(
            "POVO_LOCK_TOPPING_PERSONAL",
            "トッピングを個人支出に固定",
            type="bool",
            required=False,
            help="VPASS の「ｐｏｖｏご利用料金」 のうち適格証明書なし (= povo PDF と金額不一致のトッピング購入) を「今回は個人支出」 で固定し、 自動分類・手動変更どちらでも動かさない (= ハードロック)",
        ),
    ],
    depends_on=("gmail",),  # Gmail 認証 (GMAIL_USER / GMAIL_APP_PASSWORD) は gmail plugin で管理
    provided_banks=("povo",),
    history_url="https://povo.jp/",
    requires_otp=False,
    requires_user_accept=False,
)
