from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.aws.scraper import run as _run


async def _runner():
    # 12 ヶ月までは無料。 14 ヶ月超は AWS console で「Multi-Year Cost Allocation」
    # を有効化する必要がある (= 別途有料設定)。 安全のため既定 12 ヶ月
    return await _run(months=12)


PLUGIN = ServiceSpec(
    name="aws",
    display_name="AWS (Cost Explorer)",
    description=(
        "請求専用 IAM ユーザの Cost Explorer API (= boto3) で月次支出を取得 → "
        "transactions に bank='AWS' で記録。 受領者は AWS Japan G.K. (T1010001113295)、 "
        "適格事業者番号付きで /invoices に出る。 console ログイン不要で、 IAM ポリシーは "
        "ce:GetCostAndUsage のみ。 セットアップは admin プロファイルで "
        ".venv/bin/python3 -m scripts.aws_setup_billing_iam を実行すれば IAM ユーザ + "
        "ポリシー + Access Key を一発生成"
    ),
    runner=_runner,
    env_keys=[
        EnvKeySpec(
            "AWS_ACCESS_KEY_ID", "IAM Access Key ID", type="text",
            placeholder="AKIAXXXXXXXXXXXXXXXX",
            help="請求専用 IAM ユーザの アクセスキー (= ce:GetCostAndUsage 権限のみで OK)",
        ),
        EnvKeySpec(
            "AWS_SECRET_ACCESS_KEY", "IAM Secret Access Key", type="password",
        ),
        EnvKeySpec(
            "AWS_REGION", "AWS Region", type="text", required=False,
            placeholder="us-east-1 (= 既定)",
            help="Cost Explorer はグローバル API なので変更不要",
        ),
    ],
    provided_banks=("AWS",),
    history_url="https://console.aws.amazon.com/billing/home",
    requires_otp=False,
    requires_user_accept=False,
)
