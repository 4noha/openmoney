"""スクレイパー実行履歴を daemon_state に記録するヘルパー。

各スクレイパーの run() が完了する際に mark_scrape_done() を呼び出すことで、
- 取得件数 0 でも実行された事実が残る
- daemon 経由でも個別実行でも統一して記録される

参照は get_scrape_state() / get_all_scrape_states() で。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from src.db import DB_PATH


def _con() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=30)


def _set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)",
        (key, value),
    )


def mark_scrape_started(scraper: str) -> None:
    """スクレイパー run() の冒頭で呼ぶ。"""
    con = _con()
    _set(con, f"scrape:{scraper}:started_at", datetime.now().isoformat())
    _set(con, f"scrape:{scraper}:status", "running")
    con.commit()
    con.close()


def mark_scrape_done(scraper: str, count: int = 0, extra: dict | None = None) -> None:
    """スクレイパー run() の末尾で呼ぶ。
    count: 取得件数（0でも記録）
    extra: 任意の補足情報（例: {"saved": 3, "errors": 0}）
    """
    con = _con()
    now = datetime.now().isoformat()
    _set(con, f"scrape:{scraper}:last_done_at", now)
    _set(con, f"scrape:{scraper}:status", "success")
    _set(con, f"scrape:{scraper}:count", str(count))
    if extra:
        for k, v in extra.items():
            _set(con, f"scrape:{scraper}:{k}", str(v))
    con.commit()
    con.close()


def mark_scrape_error(scraper: str, error: str) -> None:
    """スクレイパーがエラーで失敗した時に呼ぶ。"""
    con = _con()
    _set(con, f"scrape:{scraper}:last_error_at", datetime.now().isoformat())
    _set(con, f"scrape:{scraper}:status", "error")
    _set(con, f"scrape:{scraper}:last_error", error[:300])
    con.commit()
    con.close()


def get_all_scrape_states() -> dict[str, dict[str, str]]:
    """すべての scrape:<name>:* キーを {name: {field: value}} 形式で返す。"""
    con = _con()
    rows = con.execute(
        "SELECT key, value FROM daemon_state WHERE key LIKE 'scrape:%'"
    ).fetchall()
    con.close()
    out: dict[str, dict[str, str]] = {}
    for key, value in rows:
        parts = key.split(":", 2)
        if len(parts) == 3:
            _, name, field = parts
            out.setdefault(name, {})[field] = value
    return out
