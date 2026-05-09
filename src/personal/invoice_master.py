"""インボイスマスター loader (#35 / docs/personal_layer_plan.md Step 4)。

`config/invoice_master.toml` (テンプレート、main 共有) と
`config/invoice_master.local.toml` (個人差分) を読み込んで
`invoice_vendors` テーブルに **INSERT OR IGNORE** で取り込む。

ポリシー:
  - service_name をキーとした application-level uniqueness。
    既存の service_name エントリは **絶対に上書きしない** (UI で
    user が手動編集した company_name / invoice_number を保護)。
  - 新規 service_name のみ INSERT。
  - main 共有テンプレートに新規の大手プラットフォームを追加すると、
    起動時に自動取込される。

呼び出し:
  起動時の lifespan で `sync_invoice_vendors(con)` を 1 回呼ぶ。
"""
from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
_CONFIG_DIR = ROOT / "config"


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_master_vendors() -> list[dict]:
    """defaults + local の vendors を 1 本のリストにして返す。
    重複 service_name は defaults を残す (local のものは IGNORE される)。"""
    defaults = _load_toml(_CONFIG_DIR / "invoice_master.toml")
    local = _load_toml(_CONFIG_DIR / "invoice_master.local.toml")
    return list(defaults.get("vendors", [])) + list(local.get("vendors", []))


def sync_invoice_vendors(con: sqlite3.Connection) -> int:
    """invoice_vendors テーブルに master 取込 (新規のみ)。

    戻り値: INSERT した件数。0 なら既に全部取り込み済 or master 自体が空。
    既存 service_name は touch しない (user 編集を保護)。
    """
    n_inserted = 0
    for v in load_master_vendors():
        service_name = (v.get("service_name") or "").strip()
        if not service_name:
            continue
        existing = con.execute(
            "SELECT id FROM invoice_vendors WHERE service_name=?", (service_name,)
        ).fetchone()
        if existing:
            continue
        con.execute(
            "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) "
            "VALUES (?, ?, ?)",
            (service_name,
             (v.get("company_name") or "").strip(),
             (v.get("invoice_number") or "").strip()),
        )
        n_inserted += 1
    if n_inserted:
        con.commit()
    return n_inserted
