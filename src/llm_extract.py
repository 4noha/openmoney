"""LLM ベースの構造化抽出ヘルパー (= 各 plugin から再利用)。

Claude モデル指定をここに集約することで、 povo / gmail / 将来の plugin から
共通呼び出しできる。 ANTHROPIC_API_KEY が無い環境でも plugin 側で None
ハンドリングすれば落ちずに動かせる (= regex 等のフォールバックを各 plugin で
実装)。
"""
from __future__ import annotations

import json
import os
import re

# モデル指定はここでだけ持つ。 変更時は 1 箇所だけ書き換えれば良い。
DEFAULT_MODEL = "claude-opus-4-7"


def extract_json(prompt: str, *, max_tokens: int = 512, model: str | None = None) -> dict | None:
    """Claude API で JSON を抽出。 失敗 / API key 未設定なら None。

    plugin 側は必ず None チェックして regex 等のフォールバックに進めること。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[llm_extract] API 失敗: {e}")
        return None
    text = (msg.content[0].text if msg.content else "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
