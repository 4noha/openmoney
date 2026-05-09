"""OpenMoney backend (= 中間サーバ) とのペアリングコード入力 client。

使い方:
  # 1. Android アプリ「OpenMoney Eclipse」 でログイン + 課金 → 8 桁コードを取得
  # 2. このスクリプトを実行 (= OpenMoney repo 直下で):
  .venv/bin/python3 -m scripts.openmoney_pair

  # 3. プロンプトに 8 桁コードを入力 (= 例: T5YN88PY)
  # 4. backend が pc_token + ai_token (= Claude Code 用) を返す → .env に保存される
  # 5. 後段で setup.sh が exec claude でセットアップを Claude Code に引き継ぎ

env:
  OPENMONEY_BACKEND_URL  : 中間サーバ URL (= 例 https://api.openmoney.example.com)
                           未設定なら http://localhost:8000 (= MVP の dev backend)

完了後 .env に以下が書き込まれる:
  OPENMONEY_BACKEND_URL     = backend URL (= response の backend_url 優先、 fallback で env 値)
  OPENMONEY_USER_UID        = <Firebase Auth uid>
  OPENMONEY_PC_TOKEN        = <bearer token、 以後 backend 通信に使う>
  OPENMONEY_DEVICE_LABEL    = <PC の hostname>
  ANTHROPIC_BASE_URL        = <backend URL、 Claude Code が AI Proxy に向かうため>
  ANTHROPIC_AUTH_TOKEN      = <ai_token (= 平文)、 backend 課金枠で Claude を使う>
  OPENMONEY_AI_TOKEN_ID     = <ai_token の管理 ID、 後で revoke する際の参照用>
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

DEFAULT_BACKEND = "https://openmoney-backend-282804505520.asia-northeast1.run.app"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _write_claude_settings(settings_path: Path, backend_url: str, ai_token: str) -> None:
    """ANTHROPIC_* + model を tools/.claude/settings.local.json に書く。

    既存の permissions 等は保持してマージする。
    .env ではなくここに書く理由: security の bootstrap で .env 全平文が暗号化されると
    run_claude.sh / claude コマンドが暗号化 blob を認証 token として送ってしまうため。

    CLAUDE_CONFIG_DIR=<repo>/tools/.claude を setup.sh / start.sh で export しているので
    開発用 ~/.claude/ は汚染されない。
    """
    import json as _json

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            cfg = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}

    cfg.setdefault("env", {})
    cfg["env"]["ANTHROPIC_BASE_URL"]   = backend_url
    cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = ai_token
    cfg["model"] = HAIKU_MODEL

    settings_path.write_text(
        _json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    backend_url = os.environ.get("OPENMONEY_BACKEND_URL", DEFAULT_BACKEND).rstrip("/")
    print(f"OpenMoney backend: {backend_url}")
    print()

    # コードはコマンドライン引数 or 標準入力で受け取る
    # (Lima 等の端末では Python の input() が TTY を掴めないため
    #  setup.sh 側で bash read して引数渡しするのが確実)
    if len(sys.argv) > 1:
        code = sys.argv[1].strip().upper()
        print(f"ペアリングコード: {code}")
    else:
        print("Android アプリ「OpenMoney Eclipse」 で表示された 8 桁コードを入力してください。")
        print("(コードは Android 側 → 課金確認後に発行されます)")
        print()
        code = input("ペアリングコード (= 8 桁): ").strip().upper()
    if len(code) < 6:
        print(f"✗ コードが短すぎます: {code!r}")
        return 1

    label = socket.gethostname() or "PC"
    print(f"\nデバイスラベル: {label}")

    try:
        import urllib.request
        import json
    except ImportError as e:
        print(f"✗ {e}")
        return 1

    req = urllib.request.Request(
        f"{backend_url}/api/pair/redeem",
        data=json.dumps({"session_id": code, "device_label": label}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            msg = err.get("detail", str(err))
        except Exception:
            msg = e.reason
        print(f"\n✗ ペアリング失敗 (HTTP {e.code}): {msg}")
        if e.code == 404:
            print("  → コードが間違っているか、 backend に存在しません")
        elif e.code == 400:
            print("  → コードが既に使われたか、 期限切れです")
        return 1
    except Exception as e:
        print(f"\n✗ backend 接続失敗: {e}")
        print(f"  OPENMONEY_BACKEND_URL={backend_url} を確認してください")
        return 1

    uid = body.get("uid", "")
    pc_token = body.get("pc_token", "")
    if not uid or not pc_token:
        print(f"\n✗ 不正な応答: {body}")
        return 1
    # 新 response 形式 (= backend で同時発行される ai_token を Claude Code に渡す)
    ai_token = body.get("ai_token", "")
    ai_token_id = body.get("ai_token_id", "")
    ai_token_prefix = body.get("ai_token_prefix", "")
    # response の backend_url が来れば優先 (= 本番 URL に統一できる)
    effective_backend = body.get("backend_url") or backend_url

    # .env に保存
    repo_root = Path(__file__).parent.parent
    env_path = repo_root / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    skip_keys = {
        "OPENMONEY_BACKEND_URL=",
        "OPENMONEY_USER_UID=",
        "OPENMONEY_PC_TOKEN=",
        "OPENMONEY_DEVICE_LABEL=",
        "OPENMONEY_USER_EMAIL=",
        "OPENMONEY_AI_TOKEN_ID=",
        # OCR 用 Anthropic 設定 (backend プロキシ経由)
        "ANTHROPIC_API_KEY=",
        "ANTHROPIC_BASE_URL=",
    }
    lines = [l for l in lines if not any(l.startswith(k) for k in skip_keys)]
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines.append(f"OPENMONEY_BACKEND_URL={effective_backend}\n")
    lines.append(f"OPENMONEY_USER_UID={uid}\n")
    lines.append(f"OPENMONEY_PC_TOKEN={pc_token}\n")
    lines.append(f"OPENMONEY_DEVICE_LABEL={label}\n")
    # security.py の bootstrap に必要: *_EMAIL キーがないと /login で
    # 「.env に EMAIL を登録してください」が出て初期パスワード設定ができない。
    lines.append(f"OPENMONEY_USER_EMAIL={uid}@openmoney.io\n")
    if ai_token_id:
        lines.append(f"OPENMONEY_AI_TOKEN_ID={ai_token_id}\n")
    if ai_token:
        # OCR (receipts.py) も backend プロキシ経由で Claude を呼ぶ。
        # .env に書くと security bootstrap で暗号化されるが、daemon は unlock 後に
        # 復号済み値を os.environ に展開するため receipts.py から正常に参照できる。
        lines.append(f"ANTHROPIC_API_KEY={ai_token}\n")
        lines.append(f"ANTHROPIC_BASE_URL={effective_backend}\n")
    env_path.write_text("".join(lines))
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    # ANTHROPIC_* を <repo>/tools/.claude/settings.local.json にも書く
    # (埋め込み Claude Code は CLAUDE_CONFIG_DIR=<repo>/tools/.claude で起動するため
    #  ここに置けば読まれる。 security bootstrap の暗号化対象外なので claude CLI が
    #  常にプレーンテキストのトークンを送れる。 既存 settings.local.json があれば
    #  permissions 等を保持して env のみ更新する。)
    if ai_token:
        _write_claude_settings(
            repo_root / "tools" / ".claude" / "settings.local.json",
            backend_url=effective_backend,
            ai_token=ai_token,
        )

    print(f"\n✓ ペアリング完了")
    print(f"  uid:        {uid}")
    print(f"  pc_token:   {pc_token[:8]}***{pc_token[-6:]}")
    if ai_token:
        print(f"  ai_token:   {ai_token_prefix}*** (= Claude Code が backend 経由で Sonnet/Haiku を叩ける)")
    print(f"  → .env に保存、 chmod 600")
    print()
    if ai_token:
        print("次に setup.sh が `exec claude` で Claude Code を起動します。")
        print("起動後、 銀行スクレイパーの credential 設定 (= /pw 個人鍵 + 各銀行ログイン) を")
        print("AI が会話で誘導します。")
    else:
        print("daemon を再起動すると OTP 中継が有効になります:")
        print("  pkill -9 -f 'src.daemon' && .venv/bin/python3 -u -m src.daemon ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
