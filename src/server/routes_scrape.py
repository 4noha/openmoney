"""Scraper 強制実行 API。

daemon プロセス内で実行されるため、 .env はマスターパスワードで復号化済 →
plugin スクレイパーが平文 env を参照できる (= 直接 `python -m plugins.<name>.scraper`
だと暗号化値のままで login に失敗する問題を回避)。

利用例:
  curl -X POST http://localhost:8765/api/scrape/mufg
  curl -X POST http://localhost:8765/api/scrape/vpass
"""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import HTTPException

from src.server import DB_PATH, app


def _scrape_state(con: sqlite3.Connection, name: str) -> dict:
    """daemon_state から指定 scraper の状態を返す。"""
    out: dict = {}
    for r in con.execute(
        "SELECT key, value FROM daemon_state WHERE key LIKE ?",
        (f"scrape:{name}:%",),
    ).fetchall():
        suffix = r[0].split(":", 2)[-1]
        out[suffix] = r[1]
    return out


@app.get("/api/scrape/registry")
async def api_scrape_registry():
    """登録済 plugin 一覧 + 各々の最終 scrape 状態。"""
    from src.plugins_loader import get_registry
    registry = get_registry()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        out = []
        for name, spec in sorted(registry.items()):
            out.append({
                "name": name,
                "display_name": spec.display_name,
                "env_keys": [k.key for k in (spec.env_keys or [])],
                "requires_user_accept": spec.requires_user_accept,
                "state": _scrape_state(con, name),
            })
        return {"scrapers": out}
    finally:
        con.close()


@app.post("/api/scrape/{name}")
async def api_scrape_trigger(name: str, headless: bool | None = None,
                              force_start: str | None = None,
                              force_end: str | None = None):
    """指定 plugin のスクレイパーを daemon プロセス内で background 実行。

    .env は daemon 起動時に復号化済 → 平文 env で login できる。
    結果は scrape:<name>:* (daemon_state) に記録され、 GET で取得可能。

    headless パラメータ:
    - 省略 (None): 各 plugin のデフォルト
    - false: 強制 GUI (目視デバッグ用)
    - true: 強制 headless

    force_start / force_end (YYYY-MM-DD):
    - 取得期間を強制指定 (= ログイン直後にこの期間だけ取得して終了)
    - 切り分け用: ?force_start=2026-03-01&force_end=2026-03-31
    - mufg のみ対応 (run() に force_start/force_end を渡せる scraper)
    """
    from src.daemon import _run_scraper
    from src.plugins_loader import get_registry
    from datetime import date as _date
    registry = get_registry()
    spec = registry.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not registered")

    env_key = spec.env_keys[0].key if spec.env_keys else None

    # 期間指定をパース (YYYY-MM-DD)
    fs = _date.fromisoformat(force_start) if force_start else None
    fe = _date.fromisoformat(force_end) if force_end else None

    # headless / 期間指定があれば、 plugin 直 import で run(...) に渡す
    if headless is not None or fs or fe:
        try:
            import importlib
            mod = importlib.import_module(f"plugins.{name}.scraper")
            if not hasattr(mod, "run"):
                raise HTTPException(status_code=400,
                                     detail=f"plugin '{name}' has no run()")
        except ImportError as e:
            raise HTTPException(status_code=404, detail=f"import failed: {e}")

        async def _runner_override():
            kw = {}
            if headless is not None:
                kw["headless"] = headless
            if fs is not None:
                kw["force_start"] = fs
            if fe is not None:
                kw["force_end"] = fe
            return await mod.run(**kw)
        runner = _runner_override
    else:
        runner = spec.runner

    async def _bg():
        try:
            await _run_scraper(
                spec.name, env_key, runner,
                requires_user_accept=spec.requires_user_accept,
                display_name=spec.display_name,
            )
        except Exception as e:
            print(f"[api_scrape] {name} failed: {e}")

    asyncio.create_task(_bg())
    return {
        "status": "started",
        "name": name,
        "display_name": spec.display_name,
        "headless": headless,
        "note": "進捗は GET /api/scrape/registry または daemon_state テーブルで確認",
    }
