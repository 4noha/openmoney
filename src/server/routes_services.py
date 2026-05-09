"""プラグイン (サービス) 管理用 API。
- 一覧 (登録済プラグイン + 有効化状態 + .env 入力状況)
- 有効化トグル
- env キー保存 (security 経由で暗号化)
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel

from src.server import app, _tx_db
from src import security
from src.plugins_loader import get_registry


class ToggleBody(BaseModel):
    enabled: bool


class EnvBody(BaseModel):
    values: dict[str, str]   # {KEY: VALUE} — VALUE が空文字なら何もしない


def _enabled_map() -> dict[str, bool]:
    con = _tx_db()
    rows = con.execute("SELECT name, enabled FROM service_enabled").fetchall()
    con.close()
    return {r["name"]: bool(r["enabled"]) for r in rows}


def _bank_data_summary(banks: tuple[str, ...]) -> dict:
    """指定 bank 群の transactions 件数 + 最新 / 最古日付。"""
    if not banks:
        return {"row_count": 0, "latest": None, "oldest": None}
    con = _tx_db()
    placeholders = ",".join("?" * len(banks))
    row = con.execute(
        f"SELECT COUNT(*), MAX(date), MIN(date) FROM transactions "
        f"WHERE bank IN ({placeholders})",
        banks,
    ).fetchone()
    con.close()
    return {
        "row_count": row[0] or 0,
        "latest": row[1],
        "oldest": row[2],
    }


@app.get("/api/services")
async def api_services():
    """プラグイン一覧 + 有効化状態 + 各 env キーの保存状態 + データ統計。"""
    reg = get_registry()
    enabled = _enabled_map()
    env_entries = {e["key"]: e for e in security.parse_env()}

    services = []
    known_names = set(reg.keys())
    for name, spec in sorted(reg.items()):
        keys = []
        for k in spec.env_keys:
            entry = env_entries.get(k.key)
            if not entry or not entry["value"]:
                status = "missing"
            elif entry["encrypted"]:
                status = "encrypted"
            else:
                status = "plaintext"
            keys.append({
                "key": k.key, "label": k.label, "type": k.type,
                "required": k.required, "placeholder": k.placeholder,
                "help": k.help, "status": status,
                "pattern": k.pattern, "pattern_error": k.pattern_error,
            })
        # 「自分を depends_on に含む有効プラグイン」= OFF にすると壊れる依存
        dependents = [
            other.name for other in reg.values()
            if name in other.depends_on and enabled.get(other.name, True)
        ]
        services.append({
            "name": spec.name,
            "display_name": spec.display_name,
            "description": spec.description,
            "enabled": enabled.get(spec.name, True),
            "requires_otp": spec.requires_otp,
            "requires_user_accept": spec.requires_user_accept,
            "depends_on": list(spec.depends_on),
            "dependents": dependents,
            "provided_banks": list(spec.provided_banks),
            "data": _bank_data_summary(spec.provided_banks),
            "env_keys": keys,
        })

    # registry に存在しないが service_enabled に残っている orphan
    orphans = []
    for name, en in enabled.items():
        if name not in known_names:
            orphans.append({"name": name, "enabled": en})

    return {"services": services, "orphans": orphans}


@app.delete("/api/services/{name}")
async def api_service_delete_orphan(name: str):
    """registry から消えたプラグインの service_enabled 行を削除する。"""
    if name in get_registry():
        raise HTTPException(status_code=400, detail="active plugin cannot be deleted")
    con = _tx_db()
    con.execute("DELETE FROM service_enabled WHERE name=?", (name,))
    con.commit()
    con.close()
    return {"status": "ok"}


class ToggleBody2(BaseModel):
    enabled: bool
    force: bool = False


@app.post("/api/services/{name}/toggle")
async def api_service_toggle(name: str, body: ToggleBody2):
    """OFF にする時は依存しているプラグイン (dependents) があれば
    force=True が無いと拒否する。"""
    reg = get_registry()
    if name not in reg:
        raise HTTPException(status_code=404, detail="unknown plugin")
    if not body.enabled and not body.force:
        # 自分を depends_on に持つ有効プラグインを検査
        enabled_map = _enabled_map()
        blockers = [
            other.name for other in reg.values()
            if name in other.depends_on and enabled_map.get(other.name, True)
        ]
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"{', '.join(blockers)} がこのプラグインに依存しています",
                    "blockers": blockers,
                },
            )
    con = _tx_db()
    con.execute(
        "INSERT OR REPLACE INTO service_enabled (name, enabled, updated_at) VALUES (?,?,?)",
        (name, 1 if body.enabled else 0, datetime.now().isoformat()),
    )
    con.commit()
    con.close()
    return {"status": "ok", "enabled": body.enabled}


@app.post("/api/services/{name}/env")
async def api_service_set_env(name: str, body: EnvBody):
    """指定サービスの env キーを保存。解錠中なら即暗号化、未解錠なら平文書込
    （後で設定画面のダイアログから一括暗号化）。"""
    spec = get_registry().get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown plugin")
    import re
    key_specs = {k.key: k for k in spec.env_keys}
    saved = []
    for key, value in body.values.items():
        if key not in key_specs:
            continue
        ks = key_specs[key]
        # bool 型で空文字列 = OFF → .env から削除 + os.environ 削除
        if ks.type == "bool" and not value:
            security.write_env_value(key, "")
            import os as _os
            _os.environ.pop(key, None)
            saved.append(key)
            continue
        if not value:
            continue
        # pattern バリデーション (= EnvKeySpec.pattern が空なら skip)
        if ks.pattern and not re.fullmatch(ks.pattern, value):
            raise HTTPException(
                status_code=400,
                detail={
                    "key": key,
                    "message": ks.pattern_error or f"{ks.label} の形式が不正です (期待: {ks.pattern})",
                },
            )
        # 平文を .env に書込
        security.write_env_value(key, value)
        # os.environ にも反映
        import os
        os.environ[key] = value
        saved.append(key)
        # 解錠中なら即暗号化
        if security.is_unlocked():
            try:
                security.encrypt_env_key(key)
            except Exception:
                pass
    return {"status": "ok", "saved": saved}
