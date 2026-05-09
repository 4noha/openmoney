"""全 plugins/*/scraper.py の transactions INSERT が description_normalized を
含むことを保証する regression test。

過去 (2026/03 MUFG 30 件未分類問題): MUFG scraper が description_normalized を
埋めずに INSERT していたため、 起動時バックフィルに依存。 新規取り込みは空のまま
保存され auto_categorize_from_history が学習ルールを適用できず未分類のまま残った。

このテストは plugins/*/scraper.py を機械的に走査し、 transactions テーブルに
INSERT する全 SQL 文に description_normalized カラムが含まれているかチェックする。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PLUGINS_DIR = Path(__file__).parent.parent / "plugins"

# transactions に書き込むタイプの SQL 文 (INSERT INTO transactions / UPDATE transactions)
_INSERT_PATTERN = re.compile(
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO)\s+transactions\b",
    re.IGNORECASE,
)


def _scraper_files() -> list[Path]:
    out = []
    for d in sorted(_PLUGINS_DIR.iterdir()):
        if not d.is_dir():
            continue
        for cand in (d / "scraper.py", d / "scraper" / "__init__.py"):
            if cand.exists():
                out.append(cand)
                break
    return out


def _extract_transactions_inserts(path: Path) -> list[str]:
    """ファイル内の全文字列リテラルを連結して INSERT INTO transactions 文を抜き出す。

    INSERT 文は複数の文字列リテラルを連結して書かれることが多いため、
    文字列の連結ペアを探して 1 つの SQL として扱う。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 文字列リテラル (連結含む) を 1 まとめにする helper
    def _collect_str(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            # f-string は対象外 (動的)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            l = _collect_str(node.left)
            r = _collect_str(node.right)
            if l is not None and r is not None:
                return l + r
        return None

    found: list[str] = []
    for node in ast.walk(tree):
        # con.execute("INSERT INTO transactions ...", (...))
        if isinstance(node, ast.Call) and node.args:
            sql = _collect_str(node.args[0])
            if sql and _INSERT_PATTERN.search(sql):
                found.append(sql)
        # 文字列リテラルが式として登場するケース (= execute の中の implicit concat)
        # ast.Constant に値があり transactions INSERT を含むなら一応拾う
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _INSERT_PATTERN.search(node.value):
                found.append(node.value)
    return found


@pytest.mark.parametrize("scraper_path", _scraper_files(), ids=lambda p: p.parent.name)
def test_transactions_insert_has_description_normalized(scraper_path: Path):
    """全 scraper の transactions INSERT 文に description_normalized が含まれている。

    description_normalized が無いと auto_categorize_from_history が学習ルール
    (bank, description_normalized) → category を適用できず未分類のまま残る。
    """
    inserts = _extract_transactions_inserts(scraper_path)
    if not inserts:
        pytest.skip(f"{scraper_path.parent.name}: INSERT INTO transactions 文なし")

    missing: list[str] = []
    for sql in inserts:
        if "description_normalized" not in sql:
            missing.append(sql.strip()[:120])

    assert not missing, (
        f"{scraper_path.parent.name}: description_normalized を含まない INSERT 文がある\n"
        + "\n".join(f"  - {m}" for m in missing)
        + "\n\nヒント: scraper の INSERT に description_normalized カラムを追加し、"
        " src.server.categorize.normalize_desc(description) を値として渡すこと。"
    )


def test_at_least_one_scraper_found():
    """sanity check: scraper ファイルが少なくとも 1 つ見つかる。"""
    files = _scraper_files()
    assert files, f"plugins/*/scraper.py が見つからない (検索 root: {_PLUGINS_DIR})"
