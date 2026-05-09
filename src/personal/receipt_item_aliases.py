"""レシート内訳名 → 事業者 + 適格請求書番号 マッピングの loader。

config/receipt_item_aliases.toml を読み、 OCR が抽出した raw_json.items[].name に
部分一致する alias を返す。 1 枚のレシートに複数事業者が混在するケース
(= 上水道料金 / 下水道料金 が別々の T 番号で発行される) を解決するために使う。

使い方:
    from src.personal.receipt_item_aliases import resolve_item_alias
    info = resolve_item_alias("上水道料金 ¥2,950")
    # → {"merchant": "○○市水道事業会計", "invoice_number": "T..."}
    # マッチしなければ None
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
_CFG_PATH = ROOT / "config" / "receipt_item_aliases.toml"
_LOCAL_PATH = ROOT / "config" / "receipt_item_aliases.local.toml"


def _load_aliases() -> list[dict]:
    aliases: list[dict] = []
    for p in (_CFG_PATH, _LOCAL_PATH):
        if not p.exists():
            continue
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for a in data.get("alias", []):
            kw = (a.get("keyword") or "").strip()
            if not kw:
                continue
            aliases.append({
                "keyword": kw,
                "merchant": (a.get("merchant") or "").strip(),
                "invoice_number": (a.get("invoice_number") or "").strip(),
            })
    # キーワード長で降順ソート (= 長いマッチが優先 / "下水道料金" > "水道")
    aliases.sort(key=lambda a: -len(a["keyword"]))
    return aliases


_CACHE: list[dict] | None = None


def get_aliases() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_aliases()
    return _CACHE


def invalidate_cache() -> None:
    """toml 編集時に呼んで cache を無効化。"""
    global _CACHE
    _CACHE = None


def resolve_item_alias(text: str) -> dict | None:
    """text に部分一致する alias を返す。 マッチしなければ None。"""
    if not text:
        return None
    for a in get_aliases():
        if a["keyword"] in text:
            return {
                "merchant": a["merchant"],
                "invoice_number": a["invoice_number"],
            }
    return None
