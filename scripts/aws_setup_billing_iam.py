"""AWS Cost Explorer 用の請求専用 IAM ユーザを作成する bootstrap script。

使い方:

[A] AWS SSO (= IAM Identity Center) でログイン済の場合 (推奨):
  # 1. SSO プロファイルが未設定なら初回だけ:
  aws configure sso  # SSO start URL / region / アカウント / role を選択

  # 2. ブラウザでログイン (= 一時認証、 通常 8〜12 時間有効):
  aws sso login --profile <SSO_PROFILE_NAME>

  # 3. 本 script 実行:
  AWS_PROFILE=<SSO_PROFILE_NAME> .venv/bin/python3 -m scripts.aws_setup_billing_iam

[B] アクセスキー認証で実行する場合:
  AWS_ACCESS_KEY_ID=xxx AWS_SECRET_ACCESS_KEY=yyy \\
    .venv/bin/python3 -m scripts.aws_setup_billing_iam

何をするか:
  1. 現在の AWS 認証主体 (= caller identity) を表示し、 続行 確認
  2. IAM ユーザ "openmoney-billing-readonly" を作成 (= 既存ならスキップ)
  3. インラインポリシー「BillingReadOnly」 (= ce:GetCostAndUsage のみ) を付与
  4. Access Key を新規発行
  5. 結果を表示 → ユーザは OpenMoney の /settings → AWS plugin の env 欄に
     貼り付けて保存

注意:
  - IAM ユーザ作成権限が必要 (= AdministratorAccess または iam:CreateUser /
    iam:PutUserPolicy / iam:CreateAccessKey 権限を持つ Role / User)
  - 既に同名 IAM ユーザがあって access key も発行済みの場合、 新規 Access Key
    を追加発行 (= 1 ユーザにつき最大 2 個)。 既存キーが不要なら手動削除推奨
  - 出力した Secret Access Key は **このタイミングしか取れない** (= 後から再表示
    不可、 紛失時は再発行)
"""
from __future__ import annotations

import json
import sys

USER_NAME = "openmoney-billing-readonly"
POLICY_NAME = "BillingReadOnly"
POLICY_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["ce:GetCostAndUsage", "ce:GetCostAndUsageWithResources"],
            "Resource": "*",
        }
    ],
}


def _check_credentials_or_help(boto3) -> dict | None:
    """sts.get_caller_identity で現在の認証を確認し、 失敗時は SSO ログイン手順を案内。
    成功時 caller identity dict を返す、 失敗時 None。
    """
    import os
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        return identity
    except Exception as e:
        msg = str(e)
        print(f"✗ AWS 認証エラー: {msg[:200]}")
        print()
        print("─" * 70)
        print("AWS 認証が見つからないか期限切れです。 以下のいずれかで認証してください:")
        print()
        print("[A] SSO (= IAM Identity Center) を使う場合:")
        profile = os.environ.get("AWS_PROFILE", "<your-sso-profile>")
        if profile == "<your-sso-profile>":
            print("  # 初回のみ: SSO プロファイル設定")
            print("  aws configure sso")
            print()
        print(f"  # ブラウザでログイン (= 一時認証):")
        print(f"  aws sso login --profile {profile}")
        print()
        print(f"  # 本 script を再実行:")
        print(f"  AWS_PROFILE={profile} .venv/bin/python3 -m scripts.aws_setup_billing_iam")
        print()
        print("[B] アクセスキー認証を使う場合:")
        print("  export AWS_ACCESS_KEY_ID=xxx")
        print("  export AWS_SECRET_ACCESS_KEY=yyy")
        print("  .venv/bin/python3 -m scripts.aws_setup_billing_iam")
        print("─" * 70)
        return None


def main() -> int:
    try:
        import boto3
    except ImportError:
        print("boto3 が未インストール。 .venv/bin/pip install boto3 で導入してください")
        return 1

    # 0. 認証チェック (= SSO トークン期限切れ等を早期検知)
    identity = _check_credentials_or_help(boto3)
    if not identity:
        return 1
    print(f"現在の AWS 認証主体:")
    print(f"  Account: {identity.get('Account')}")
    print(f"  ARN:     {identity.get('Arn')}")
    print()
    ans = input("このアカウントに IAM ユーザを作成して続行しますか？ [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        print("中止しました")
        return 0

    iam = boto3.client("iam")

    # 1. IAM ユーザ作成 (= 既存ならスキップ)
    try:
        iam.create_user(UserName=USER_NAME)
        print(f"✓ IAM ユーザ作成: {USER_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"- IAM ユーザ既存: {USER_NAME} (= スキップ)")
    except Exception as e:
        print(f"✗ IAM ユーザ作成失敗: {e}")
        return 1

    # 2. インラインポリシー付与
    try:
        iam.put_user_policy(
            UserName=USER_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(POLICY_DOC),
        )
        print(f"✓ ポリシー付与: {POLICY_NAME} (= ce:GetCostAndUsage)")
    except Exception as e:
        print(f"✗ ポリシー付与失敗: {e}")
        return 1

    # 3. Access Key 発行
    try:
        existing_keys = iam.list_access_keys(UserName=USER_NAME).get("AccessKeyMetadata", [])
        if len(existing_keys) >= 2:
            print(f"⚠ Access Key 上限 (2 個) に達しています。 既存キーを削除してから再実行:")
            for k in existing_keys:
                print(f"   aws iam delete-access-key --user-name {USER_NAME} "
                      f"--access-key-id {k['AccessKeyId']}")
            return 1
        resp = iam.create_access_key(UserName=USER_NAME)
        ak = resp["AccessKey"]
        print(f"✓ Access Key 発行")
    except Exception as e:
        print(f"✗ Access Key 発行失敗: {e}")
        return 1

    # 4. 結果は .env に直接書き込む (= stdout には Secret を出さない、 流出防止)
    import os as _os
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    # 既存の AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 行を削除
    lines = [l for l in lines if not l.startswith(("AWS_ACCESS_KEY_ID=", "AWS_SECRET_ACCESS_KEY="))]
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines.append(f"AWS_ACCESS_KEY_ID={ak['AccessKeyId']}\n")
    lines.append(f"AWS_SECRET_ACCESS_KEY={ak['SecretAccessKey']}\n")
    env_path.write_text("".join(lines))
    try:
        _os.chmod(env_path, 0o600)
    except OSError:
        pass

    # masked display
    masked_id = ak["AccessKeyId"][:4] + "*" * 12 + ak["AccessKeyId"][-4:]
    print()
    print("─" * 70)
    print(f"✓ Access Key を {env_path} に書込み (chmod 600)")
    print(f"  AWS_ACCESS_KEY_ID     = {masked_id}")
    print(f"  AWS_SECRET_ACCESS_KEY = (= 表示せず .env に直接書込)")
    print()
    print("daemon を再起動すると AWS plugin がこのキーを使用開始します:")
    print("  pkill -9 -f 'src.daemon' && .venv/bin/python3 -u -m src.daemon ...")
    print()
    print("Secret Access Key は再表示不可。 紛失時は本 script 再実行で再発行。")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
