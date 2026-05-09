"""ユーザタグ設定 API。

GET /api/user-tags
  → { all_genres: [tag_id...], disabled: [tag_id...], custom: [{id,label,keywords}...] }

POST /api/user-tags
  body: { disabled: [...], custom: [...] }
  → 設定を config/user_tags.toml に書き出して即時反映
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from src.server import app, _invalidate_user_tags_cache
from src.personal.user_tags import load_user_tags, save_user_tags


# tag-bar に並べる builtin の表示順 + 表示ラベル (ui.html の tag-btn と一致)
_BUILTIN_GENRES = [
    ("car", "🚗 クルマ"),
    ("facility", "🏬 商業施設"),
    ("invest", "💹 投資"),
    ("insurance", "🛡 保険"),
    ("service", "📱 サービス"),
    ("fantasy", "🎨 FANTASY"),
    ("book", "📚 本"),
    ("drink", "☕ お茶"),
    ("convenience", "🏪 コンビニ"),
    ("supermarket", "🛒 スーパー"),
    ("restaurant", "🍽 飲食店"),
    ("activity", "🎡 アクティビティ"),
    ("homecenter", "🔨 ホームセンター"),
    ("medical", "🏥 医療費"),
    ("landlord", "🏠 大家業"),
]

# ショップタグ (= store: prefix の bank/keyword ベース)。 STORE_TAG_BANKS と
# STORE_TAGS keywords (= keywords.toml) で transactions に動的付与される。
# id は "store:<bank>" 形式で /ui の tag-btn と一致
_BUILTIN_SHOPS = [
    ("store:Amazon", "Amazon"),
    ("store:Yahoo!ショッピング", "Yahoo!ショッピング"),
    ("store:楽天市場", "楽天市場"),
    ("store:ヤフオク購入", "ヤフオク購入"),
    ("store:Makuake", "Makuake"),
    ("store:CAMPFIRE", "CAMPFIRE"),
    ("store:Mercari", "メルカリ"),
    ("store:AliExpress", "AliExpress"),
    ("store:povo", "povo"),
    ("store:Discord", "Discord"),
    ("store:Google Play", "Google Play"),
    ("store:vidIQ", "vidIQ"),
    ("store:AWS", "AWS"),
]

# ツールタグ (= フィルタ系・状態系)。 ジャンル / ショップとは性格が違う
# 「決済状態 / 突合状態 / 警告系」 を集める。 ID は ui.html の tag-btn と一致
_BUILTIN_TOOLS = [
    ("matched_months", "突合完了月"),
    ("matched", "突合済み"),
    ("amazon_points_full", "㌽ ポイント払い"),
    ("返品", "↩ 返品"),
    ("receipt", "🧾 レシート"),  # 証憑あり (= 紙 / PDF 問わず receipt_ids 付き)
    ("conflict", "⚠ 仕訳ブレ"),
    ("no_receipt", "📭 レシートなし"),
    ("no_receipt_vendor_modal", "📊 頻出 vendor"),
]


class CustomTag(BaseModel):
    id: str
    label: str = ""
    keywords: list[str] = []


class UserTagsBody(BaseModel):
    disabled: list[str] = []
    custom: list[CustomTag] = []


@app.get("/api/user-tags")
async def api_user_tags_get():
    cfg = load_user_tags()
    return {
        "all_genres": [{"id": _id, "label": _lbl} for _id, _lbl in _BUILTIN_GENRES],
        "all_shops": [{"id": _id, "label": _lbl} for _id, _lbl in _BUILTIN_SHOPS],
        "all_tools": [{"id": _id, "label": _lbl} for _id, _lbl in _BUILTIN_TOOLS],
        "disabled": cfg["disabled"],
        "custom": cfg["custom"],
    }


@app.post("/api/user-tags")
async def api_user_tags_post(body: UserTagsBody):
    # 簡易バリデーション
    seen: set[str] = set()
    custom_clean = []
    for c in body.custom:
        cid = c.id.strip()
        if not cid:
            continue
        if cid in seen:
            raise HTTPException(status_code=400, detail=f"重複 id: {cid}")
        seen.add(cid)
        custom_clean.append({
            "id": cid,
            "label": (c.label or cid).strip(),
            "keywords": [k.strip() for k in c.keywords if k.strip()],
        })
    save_user_tags([d for d in body.disabled if d], custom_clean)
    _invalidate_user_tags_cache()
    return {"status": "ok", "disabled": body.disabled, "custom": custom_clean}
