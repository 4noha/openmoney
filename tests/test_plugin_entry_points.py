"""外部プラグイン受入 (entry_points 経由) のテスト。

importlib.metadata.entry_points() を monkeypatch で偽装し、3rd-party plugin が
ServiceSpec を載せた entry を提供したときに get_registry() に取り込まれることを確認。
"""
from __future__ import annotations

import pytest

from src.plugin_api import EnvKeySpec, ServiceSpec
from src import plugins_loader


async def _dummy_runner():
    return None


def _make_spec(name: str = "demo_bank", display: str = "デモ銀行") -> ServiceSpec:
    return ServiceSpec(
        name=name,
        display_name=display,
        runner=_dummy_runner,
        env_keys=[EnvKeySpec(f"{name.upper()}_EMAIL", "メール", type="email")],
        provided_banks=(name,),
        extra_schema=(
            f"CREATE TABLE IF NOT EXISTS {name}_orders (id INTEGER PRIMARY KEY)",
        ),
    )


class _FakeEntryPoint:
    def __init__(self, name: str, spec: ServiceSpec):
        self.name = name
        self._spec = spec

    def load(self):
        return self._spec


def test_discover_entry_points_loads_external_plugin(monkeypatch):
    """entry_points('mf2.plugins') が ServiceSpec を返したら discover_entry_points が拾う。"""
    spec = _make_spec()

    def fake_entry_points(group: str | None = None):
        assert group == plugins_loader.ENTRY_POINTS_GROUP
        return [_FakeEntryPoint("ep_demo", spec)]

    monkeypatch.setattr(
        "importlib.metadata.entry_points", fake_entry_points
    )
    out = plugins_loader.discover_entry_points()
    assert "demo_bank" in out
    assert out["demo_bank"].display_name == "デモ銀行"
    assert out["demo_bank"].extra_schema[0].startswith("CREATE TABLE")


def test_discover_entry_points_skips_non_servicespec(monkeypatch, capsys):
    """ServiceSpec 以外を返す entry は無視 + 警告ログ。"""
    def fake_entry_points(group: str | None = None):
        return [_FakeEntryPoint("bogus", "not a ServiceSpec")]
    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    out = plugins_loader.discover_entry_points()
    assert out == {}
    err = capsys.readouterr().out
    assert "ServiceSpec 型ではない" in err


def test_discover_entry_points_handles_load_failure(monkeypatch, capsys):
    """load() で例外が出ても他の entry の取り込みは継続。"""
    spec_ok = _make_spec("good_bank", "正常銀行")

    class _BrokenEP:
        name = "broken"
        def load(self):
            raise ImportError("missing dep")

    def fake_entry_points(group: str | None = None):
        return [_BrokenEP(), _FakeEntryPoint("good", spec_ok)]
    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    out = plugins_loader.discover_entry_points()
    assert "good_bank" in out
    assert "broken" not in out
    assert "load 失敗" in capsys.readouterr().out


def test_get_registry_merges_external_after_builtin(monkeypatch):
    """組み込みと外部が併存。同名衝突は組み込み優先で外部側は無視 + 警告。"""
    spec_external = _make_spec("vpass", display="外部 VPASS の偽物")
    spec_unique = _make_spec("third_party", "3rd Party Bank")

    def fake_entry_points(group: str | None = None):
        return [
            _FakeEntryPoint("ep_vpass", spec_external),
            _FakeEntryPoint("ep_third", spec_unique),
        ]
    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    reg = plugins_loader.get_registry()
    # 組み込み vpass はそのまま (display_name は "VPASS (三井住友カード)")
    assert reg["vpass"].display_name != "外部 VPASS の偽物"
    # 外部の独自プラグインは取り込まれる
    assert "third_party" in reg
    assert reg["third_party"].display_name == "3rd Party Bank"


def test_external_plugin_extra_schema_applied(monkeypatch, tmp_path):
    """外部プラグインの extra_schema が ensure_schema 経由で実 DB に反映される。"""
    import sqlite3
    import src.db as _db

    spec = _make_spec("external_demo", "外部デモ")

    def fake_entry_points(group: str | None = None):
        return [_FakeEntryPoint("ep_ext", spec)]
    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    db_path = tmp_path / "ext.db"
    con = sqlite3.connect(db_path)
    _db._initialized.clear()  # キャッシュリセット
    _db.ensure_schema(con)

    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "external_demo_orders" in tables, f"外部 plugin の table が無い: {tables}"
    con.close()
