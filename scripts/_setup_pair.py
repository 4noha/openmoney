"""setup.sh から呼ばれる内部ヘルパー。

POST /api/pair/redeem を叩いて uid / pc_token / ai_token を取得し:
  1. .env        → OPENMONEY_* + ダミー EMAIL (= security bootstrap 用)
  2. tools/.claude/settings.local.json → env.ANTHROPIC_* + model
     (= security の bootstrap 暗号化対象外にするため .env には書かない。
        CLAUDE_CONFIG_DIR=tools/.claude で開発用 ~/.claude/ も汚染しない。)

Usage (setup.sh から):
    python3 scripts/_setup_pair.py <backend_url> <session_id> <device_label>
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CLAUDE_SETTINGS = REPO_ROOT / "tools" / ".claude" / "settings.local.json"
ENV_PATH = REPO_ROOT / ".env"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

def _redeem(backend_url: str, session_id: str, device_label: str) -> dict:
    url = f"{backend_url.rstrip('/')}/api/pair/redeem"
    payload = json.dumps(
        {"session_id": session_id, "device_label": device_label}
    ).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except Exception:
            detail = body
        print(f"エラー ({e.code}): {detail}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"接続エラー: {e}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────
# .env (OPENMONEY_* のみ)
# ─────────────────────────────────────────────

def _upsert_env(updates: dict[str, str]) -> None:
    """既存 .env に updates を追記 / 上書き。コメント・空行は保持。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []

    # 既存キーの行番号インデックス
    key_line: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            key_line[k] = i

    # in-place 上書き
    for k, v in updates.items():
        if k in key_line:
            lines[key_line[k]] = f"{k}={v}"

    # 新規は末尾に追記 (OpenMoney セクション)
    new_items = {k: v for k, v in updates.items() if k not in key_line}
    if new_items:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# OpenMoney backend (= setup.sh により自動生成)")
        for k, v in new_items.items():
            lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────
# .claude/settings.local.json (ANTHROPIC_* + model)
# ─────────────────────────────────────────────

def _update_claude_settings(backend_url: str, ai_token: str) -> None:
    """settings.local.json の env / model を追加。既存の permissions は保持。"""
    if CLAUDE_SETTINGS.exists():
        try:
            cfg = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}

    cfg.setdefault("env", {})
    cfg["env"]["ANTHROPIC_BASE_URL"]  = backend_url
    cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = ai_token
    cfg["model"] = HAIKU_MODEL

    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: _setup_pair.py <backend_url> <session_id> <device_label>",
              file=sys.stderr)
        sys.exit(2)

    backend_url  = sys.argv[1].rstrip("/")
    session_id   = sys.argv[2].strip().upper()
    device_label = sys.argv[3]

    print(f"  → {backend_url}/api/pair/redeem  ...")
    result = _redeem(backend_url, session_id, device_label)

    uid              = result["uid"]
    pc_token         = result["pc_token"]
    ai_token         = result["ai_token"]
    ai_token_prefix  = result.get("ai_token_prefix", ai_token[:12])
    actual_url       = result.get("backend_url") or backend_url

    # 1. .env に OPENMONEY_* + ダミー EMAIL を書く
    #    *_EMAIL キーが無いと security の bootstrap が起動できない。
    #    OPENMONEY_USER_EMAIL はダミーとして機能し、後でユーザが
    #    実際の銀行 *_EMAIL を入力すると実用的な email に置き換わる。
    env_updates = {
        "OPENMONEY_BACKEND_URL":  actual_url,
        "OPENMONEY_USER_UID":     uid,
        "OPENMONEY_PC_TOKEN":     pc_token,
        "OPENMONEY_DEVICE_LABEL": device_label,
        # security bootstrap 用プレースホルダ (= *_EMAIL パターンに一致)
        "OPENMONEY_USER_EMAIL":   f"{uid}@openmoney.io",
    }
    _upsert_env(env_updates)
    print(f"  ✓ .env 書込 ({ENV_PATH.name})")

    # 2. tools/.claude/settings.local.json に ANTHROPIC_* を書く
    #    .env は security bootstrap で暗号化されるため、
    #    Claude Code の認証情報は settings.local.json 側に置く。
    #    CLAUDE_CONFIG_DIR=tools/.claude で参照される (= setup.sh / start.sh で export)
    _update_claude_settings(actual_url, ai_token)
    print(f"  ✓ tools/.claude/settings.local.json 書込")

    print()
    print(f"  UID           : {uid}")
    print(f"  ai_token      : {ai_token_prefix}...")
    print(f"  backend_url   : {actual_url}")
    print(f"  model         : {HAIKU_MODEL}")


if __name__ == "__main__":
    main()
