"""inspect スクリプトの共通起動ヘルパー。

inspect_*.py は load_dotenv() だけでは os.environ に **暗号化された** ID/PW が
残ったまま (security.unlock を呼ばないため)、scraper の login() が
`os.environ["VPASS_ID"]` 等を素で page.fill() しているのでログインフォームに
暗号文字列がそのまま送信されてしまう。

本ヘルパーを呼ぶことで:
- master password を getpass で安全に読む
- security.unlock() で .env の暗号化キーを復号して os.environ に注入
- 復号失敗ならエラー終了

inspect スクリプト末尾は ``if __name__ == "__main__":`` ガード必須。
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src import security  # noqa: E402


def init_inspect() -> None:
    """inspect スクリプト main() の冒頭で必ず呼ぶ。"""
    load_dotenv()
    has_encrypted = any(e["encrypted"] for e in security.parse_env())
    if not has_encrypted:
        # .env が全部平文ならそのまま使える
        return
    pw = getpass.getpass("master password: ")
    try:
        security.unlock(pw)
    except Exception as e:
        print(f"[inspect] unlock 失敗: {e}", file=sys.stderr)
        sys.exit(1)
