"""ユーザタグ設定 (使用/不使用 + カスタムタグ追加) loader/saver。

config/user_tags.toml の形式:

    disabled_tags = ["fantasy", "stripe"]   # tag-bar / バッジから隠すタグ ID

    [[custom]]
    id = "ジム"
    label = "🏋 ジム"
    keywords = ["AXIA", "ティップネス"]    # description にヒットすれば自動付与

UI から GET/POST する。書込みは TOML 形式で人間が後から読めるよう整形する。
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
USER_TAGS_PATH = ROOT / "config" / "user_tags.toml"


def load_user_tags() -> dict:
    """設定 TOML を読んで {disabled, custom} を返す (無ければ空)。"""
    if not USER_TAGS_PATH.exists():
        return {"disabled": [], "custom": []}
    try:
        with USER_TAGS_PATH.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"[user_tags] read failed: {e}")
        return {"disabled": [], "custom": []}
    disabled = list(data.get("disabled_tags", []))
    custom = []
    for c in data.get("custom", []):
        if not isinstance(c, dict):
            continue
        cid = (c.get("id") or "").strip()
        if not cid:
            continue
        custom.append({
            "id": cid,
            "label": (c.get("label") or cid).strip(),
            "keywords": [k for k in c.get("keywords", []) if k],
        })
    return {"disabled": disabled, "custom": custom}


def save_user_tags(disabled: list[str], custom: list[dict]) -> None:
    """disabled / custom を TOML に書出。既存ファイルは上書き。"""
    USER_TAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ユーザタグ設定 (UI から自動生成、手動編集も OK)",
        "# 詳細: src/personal/user_tags.py",
        "",
        f"disabled_tags = {json.dumps(disabled, ensure_ascii=False)}",
        "",
    ]
    for c in custom:
        cid = (c.get("id") or "").strip()
        if not cid:
            continue
        label = (c.get("label") or cid).strip()
        kws = [k for k in c.get("keywords", []) if k]
        lines.append("[[custom]]")
        lines.append(f"id = {json.dumps(cid, ensure_ascii=False)}")
        lines.append(f"label = {json.dumps(label, ensure_ascii=False)}")
        lines.append(f"keywords = {json.dumps(kws, ensure_ascii=False)}")
        lines.append("")
    USER_TAGS_PATH.write_text("\n".join(lines), encoding="utf-8")
