"""Gmail 経由で取り込む送信者リスト loader。

config/gmail_sources.toml (= テンプレート) + gmail_sources.local.toml
(= 個人差分、 .gitignore 対象) を読み込んで [[source]] エントリを返す。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
_DEFAULT = ROOT / "config" / "gmail_sources.toml"
_LOCAL = ROOT / "config" / "gmail_sources.local.toml"


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return list(data.get("source", []))


def load_gmail_sources() -> list[dict]:
    """全 source エントリを返す (= defaults + local)。

    エントリ key:
      - name: 取り込み時の bank 識別子 prefix
      - from: 送信者アドレス
      - subject: 件名キーワード (任意)
      - parser_hint: LLM パース時のヒント
    """
    out: list[dict] = []
    out.extend(_load(_DEFAULT))
    out.extend(_load(_LOCAL))
    return [s for s in out if (s.get("from") or "").strip()]
