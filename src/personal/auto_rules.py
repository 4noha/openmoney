"""経費自動判断ルールエンジン (#34 / docs/personal_layer_plan.md Step 3)。

`config/auto_rules.toml` (テンプレート) と `config/auto_rules.local.toml`
(個人差分) を読み込み、4 種類のルール型 (field_match / keyword_match /
pattern_match / propagate) を順次適用する。

旧 `src/server/categorize.py` の auto_categorize_outgoing /
auto_categorize_amazon_zero / auto_categorize_income / propagate_fee_categories
の挙動を全てカバーする。複雑な auto_categorize_returned_purchases や
auto_categorize_from_history は config 化に馴染まないので Python 関数のまま残す。
"""
from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent.parent.parent
_CONFIG_DIR = ROOT / "config"


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# ─────────────────────────────────────────────
# Rule dataclasses
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class _BaseRule:
    id: str
    description: str = ""
    where_unset_only: bool = True


@dataclass(frozen=True)
class FieldMatchRule(_BaseRule):
    when: dict = field(default_factory=dict)
    set_category: str = ""


@dataclass(frozen=True)
class KeywordMatchRule(_BaseRule):
    when: dict = field(default_factory=dict)
    keyword_list: str = ""        # config/keywords.toml の section 名
    field_name: str = "description_normalized"
    set_category: str = ""


@dataclass(frozen=True)
class PatternMatchRule(_BaseRule):
    when: dict = field(default_factory=dict)
    pattern: str = ""
    field_name: str = "description_normalized"
    set_category: str = ""


@dataclass(frozen=True)
class PropagateRule(_BaseRule):
    target_patterns: tuple = ()
    field_name: str = "description_normalized"
    inherit_from: str = "prev_same_bank_same_date"


# ─────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────


def load_rules() -> list:
    """defaults + local の concat。各セクション (`[[field_match]]` 等) を
    対応する dataclass に変換して 1 本のリストで返す。順序は defaults → local。"""
    defaults = _load_toml(_CONFIG_DIR / "auto_rules.toml")
    local = _load_toml(_CONFIG_DIR / "auto_rules.local.toml")
    rules: list = []
    for src in (defaults, local):
        for r in src.get("field_match", []):
            rules.append(FieldMatchRule(
                id=r.get("id", ""),
                description=r.get("description", ""),
                where_unset_only=r.get("where_unset_only", True),
                when=dict(r.get("when", {})),
                set_category=r.get("set_category", ""),
            ))
        for r in src.get("keyword_match", []):
            rules.append(KeywordMatchRule(
                id=r.get("id", ""),
                description=r.get("description", ""),
                where_unset_only=r.get("where_unset_only", True),
                when=dict(r.get("when", {})),
                keyword_list=r.get("keyword_list", ""),
                field_name=r.get("field", "description_normalized"),
                set_category=r.get("set_category", ""),
            ))
        for r in src.get("pattern_match", []):
            rules.append(PatternMatchRule(
                id=r.get("id", ""),
                description=r.get("description", ""),
                where_unset_only=r.get("where_unset_only", True),
                when=dict(r.get("when", {})),
                pattern=r.get("pattern", ""),
                field_name=r.get("field", "description_normalized"),
                set_category=r.get("set_category", ""),
            ))
        for r in src.get("propagate", []):
            rules.append(PropagateRule(
                id=r.get("id", ""),
                description=r.get("description", ""),
                where_unset_only=r.get("where_unset_only", True),
                target_patterns=tuple(r.get("target_patterns", [])),
                field_name=r.get("field", "description_normalized"),
                inherit_from=r.get("inherit_from", "prev_same_bank_same_date"),
            ))
    return rules


# ─────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────

# when の許可キーと SQL 演算子のマップ
_WHEN_OPS = {
    "bank":       "= ?",
    "debit":      "= ?",
    "debit_gt":   "> ?",
    "debit_lt":   "< ?",
    "credit":     "= ?",
    "credit_gt":  "> ?",
    "credit_lt":  "< ?",
}
_WHEN_COL = {
    "bank": "bank",
    "debit": "debit", "debit_gt": "debit", "debit_lt": "debit",
    "credit": "credit", "credit_gt": "credit", "credit_lt": "credit",
}


def _build_when_clause(when: dict) -> tuple[str, list]:
    """when dict を (sql_fragment, params) に変換。空なら ('1=1', []) を返す。"""
    parts: list[str] = []
    params: list = []
    for key, val in when.items():
        if key not in _WHEN_OPS:
            raise ValueError(f"未対応の when キー: {key}")
        parts.append(f"{_WHEN_COL[key]} {_WHEN_OPS[key]}")
        params.append(val)
    return (" AND ".join(parts) if parts else "1=1"), params


def _add_unset_filter(where_sql: str, unset_only: bool) -> str:
    if not unset_only:
        return where_sql
    return where_sql + " AND (category IS NULL OR category = '')"


# ─────────────────────────────────────────────
# 各 rule type の適用関数
# ─────────────────────────────────────────────


def apply_field_match(con: sqlite3.Connection, rule: FieldMatchRule) -> int:
    where_sql, params = _build_when_clause(rule.when)
    where_sql = _add_unset_filter(where_sql, rule.where_unset_only)
    cur = con.execute(
        f"UPDATE transactions SET category=? WHERE {where_sql}",
        [rule.set_category, *params],
    )
    return cur.rowcount


def apply_keyword_match(
    con: sqlite3.Connection,
    rule: KeywordMatchRule,
    keyword_resolver: Callable[[str], list[str]],
) -> int:
    keywords = keyword_resolver(rule.keyword_list)
    if not keywords:
        return 0
    where_sql, params = _build_when_clause(rule.when)
    where_sql = _add_unset_filter(where_sql, rule.where_unset_only)
    rows = con.execute(
        f"SELECT id, {rule.field_name} AS f FROM transactions WHERE {where_sql}",
        params,
    ).fetchall()
    updated = 0
    for r in rows:
        text = r["f"] or ""
        if any(kw in text for kw in keywords):
            con.execute("UPDATE transactions SET category=? WHERE id=?",
                        (rule.set_category, r["id"]))
            updated += 1
    return updated


def apply_pattern_match(con: sqlite3.Connection, rule: PatternMatchRule) -> int:
    where_sql, params = _build_when_clause(rule.when)
    where_sql = _add_unset_filter(where_sql, rule.where_unset_only)
    cur = con.execute(
        f"UPDATE transactions SET category=? "
        f"WHERE {where_sql} AND {rule.field_name} LIKE ?",
        [rule.set_category, *params, rule.pattern],
    )
    return cur.rowcount


def apply_propagate(con: sqlite3.Connection, rule: PropagateRule) -> int:
    if rule.inherit_from != "prev_same_bank_same_date":
        raise ValueError(f"未対応の inherit_from: {rule.inherit_from}")
    if not rule.target_patterns:
        return 0
    pat_sql = " OR ".join([f"{rule.field_name} LIKE ?"] * len(rule.target_patterns))
    where_unset = ""
    if rule.where_unset_only:
        where_unset = " AND (category IS NULL OR category = '')"
    rows = con.execute(
        f"SELECT id, bank, date FROM transactions WHERE ({pat_sql}){where_unset}",
        list(rule.target_patterns),
    ).fetchall()
    updated = 0
    for fee in rows:
        prev = con.execute(
            "SELECT category FROM transactions WHERE bank=? AND date=? AND id<? "
            "AND category IS NOT NULL AND category != '' ORDER BY id DESC LIMIT 1",
            (fee["bank"], fee["date"], fee["id"]),
        ).fetchone()
        if prev and prev[0]:
            con.execute("UPDATE transactions SET category=? WHERE id=?",
                        (prev[0], fee["id"]))
            updated += 1
    return updated


# ─────────────────────────────────────────────
# Top-level dispatcher
# ─────────────────────────────────────────────


def apply_all(
    con: sqlite3.Connection,
    invalidate_cache: Callable[[], None],
    keyword_resolver: Callable[[str], list[str]] | None = None,
) -> dict[str, int]:
    """全ルールを順次適用。各ルールの更新行数を {id: count} で返す。
    1 件でも更新があれば commit + invalidate_cache を 1 回呼ぶ。"""
    if keyword_resolver is None:
        # default: src.personal の <NAME>_KEYWORDS module 変数を引く
        from src import personal as P
        def keyword_resolver(name: str) -> list[str]:
            return getattr(P, f"{name}_KEYWORDS", [])
    rules = load_rules()
    counts: dict[str, int] = {}
    total = 0
    for r in rules:
        if isinstance(r, FieldMatchRule):
            n = apply_field_match(con, r)
        elif isinstance(r, KeywordMatchRule):
            n = apply_keyword_match(con, r, keyword_resolver)
        elif isinstance(r, PatternMatchRule):
            n = apply_pattern_match(con, r)
        elif isinstance(r, PropagateRule):
            n = apply_propagate(con, r)
        else:
            continue
        counts[r.id] = n
        total += n
    if total:
        con.commit()
        invalidate_cache()
    return counts
