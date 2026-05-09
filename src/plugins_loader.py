"""
プラグインの検出・ロード。

組み込みプラグイン (`plugins/<name>/`) はビルド済 `plugins._generated_registry.REGISTRY`
を直 import。未ビルド or 開発中は `plugins/` を動的走査するフォールバック。

外部プラグインは Python パッケージの `[project.entry-points."mf2.plugins"]`
で公開された `ServiceSpec` を読み込む。`pip install some-mf2-plugin-xyz` した
だけで自動的に registry に取り込まれる。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from src.plugin_api import ServiceSpec

ROOT = Path(__file__).parent.parent
PLUGINS_DIR = ROOT / "plugins"

ENTRY_POINTS_GROUP = "mf2.plugins"


def discover_dynamic() -> dict[str, ServiceSpec]:
    """plugins/<name>/plugin.py を全て走査して PLUGIN を集める。
    開発中・ビルド未実施時のフォールバック。重い分インデクシング無し。
    """
    out: dict[str, ServiceSpec] = {}
    if not PLUGINS_DIR.exists():
        return out
    # plugins/ 自体を import path に追加 (pyproject に含めない場合のフォールバック)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for d in sorted(PLUGINS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        plugin_py = d / "plugin.py"
        if not plugin_py.exists():
            continue
        mod_name = f"plugins.{d.name}.plugin"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            print(f"[plugins] {d.name}: import 失敗 {e}")
            continue
        spec = getattr(mod, "PLUGIN", None)
        if not isinstance(spec, ServiceSpec):
            print(f"[plugins] {d.name}: PLUGIN 未定義 or 型不一致")
            continue
        if spec.name != d.name:
            print(f"[plugins] {d.name}: PLUGIN.name='{spec.name}' とディレクトリ名が不一致")
            continue
        out[spec.name] = spec
    return out


def discover_entry_points() -> dict[str, ServiceSpec]:
    """importlib.metadata の entry_points(group='mf2.plugins') から ServiceSpec を集める。

    外部 plugin パッケージは pyproject.toml で次のように公開する:

        [project.entry-points."mf2.plugins"]
        my_bank = "my_pkg.plugin:PLUGIN"

    `PLUGIN` は ServiceSpec インスタンス。`pip install` で配布できる。
    """
    out: dict[str, ServiceSpec] = {}
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return out
    try:
        eps = entry_points(group=ENTRY_POINTS_GROUP)
    except Exception as e:
        print(f"[plugins] entry_points 取得失敗: {e}")
        return out
    for ep in eps:
        try:
            obj = ep.load()
        except Exception as e:
            print(f"[plugins] entry_point {ep.name}: load 失敗 {e}")
            continue
        if not isinstance(obj, ServiceSpec):
            print(f"[plugins] entry_point {ep.name}: ServiceSpec 型ではない (got {type(obj).__name__})")
            continue
        out[obj.name] = obj
    return out


def get_registry() -> dict[str, ServiceSpec]:
    """組み込み plugin (ビルド済 registry / 動的検出) と外部 plugin (entry_points) を
    マージして返す。組み込みが優先 (同名キーは外部側を無視して警告)。"""
    # 1) 組み込み: ビルド済 registry を優先、無ければ動的検出
    try:
        from plugins._generated_registry import REGISTRY  # type: ignore
        registry = dict(REGISTRY)
    except ImportError:
        registry = discover_dynamic()

    # 2) 外部: entry_points で公開されたものを merge
    for name, spec in discover_entry_points().items():
        if name in registry:
            print(f"[plugins] entry_point '{name}' は既存プラグインと衝突するため無視 "
                  f"(組み込み優先)")
            continue
        registry[name] = spec
    return registry


def list_plugin_dirs() -> list[str]:
    """plugins/ 直下のディレクトリ名一覧（plugin.py の有無は問わず）。"""
    if not PLUGINS_DIR.exists():
        return []
    return sorted(
        d.name for d in PLUGINS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
    )
