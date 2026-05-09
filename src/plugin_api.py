"""
プラグイン契約。`plugins/<name>/plugin.py` がここから import して
`PLUGIN: ServiceSpec` を公開する。型のみで実装は持たない（プラグイン側に
import の自由度を残すため）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

PLUGIN_API_VERSION = 1

# 入力欄タイプ
EnvKeyType = Literal["text", "password", "email", "phone", "number", "bool"]


@dataclass(frozen=True)
class EnvKeySpec:
    """サービスのカードに表示する .env 入力欄定義。

    pattern を指定すると、 設定保存時にサーバ + UI で正規表現バリデーションが
    かかる。 例: r"^\\d{4}-\\d{2}-\\d{2}$" で YYYY-MM-DD のみ許容。
    """
    key: str
    label: str
    type: EnvKeyType = "text"
    required: bool = True
    placeholder: str = ""
    help: str = ""
    pattern: str = ""           # 正規表現 (空なら検証スキップ)。 サーバ + UI で共通使用
    pattern_error: str = ""     # 不一致時に出すユーザ向けメッセージ (例: "誕生日 YYYY-MM-DD で入力")


@dataclass(frozen=True)
class ServiceSpec:
    """1 サービス（= 1 plugin = plugins/<name>/）のメタデータ。
    `runner` は daemon が `await runner()` で呼び出す async callable。

    `provided_banks` で transactions.bank に書き込む銀行名を宣言する。
    OFF/ON 切替時の影響範囲を UI に表示する根拠データになる。
    """
    name: str
    display_name: str
    runner: Callable[[], Awaitable[object]]
    env_keys: list[EnvKeySpec] = field(default_factory=list)
    description: str = ""
    requires_otp: bool = False
    requires_user_accept: bool = False
    interval_hours: int | None = None         # None で daemon 既定値
    depends_on: tuple[str, ...] = ()           # 他プラグイン名（OFF にすると依存先が動作不能）
    provided_banks: tuple[str, ...] = ()       # transactions.bank に書く名前
    history_url: str = ""                      # 購入/利用履歴ページの URL（任意）
    detail_url_template: str = ""              # 取引詳細ページ。{order_id} を置換 (description の [oid] から抽出)
    # プラグイン専用のテーブル / インデックス DDL (CREATE TABLE IF NOT EXISTS / CREATE INDEX)。
    # ensure_schema(con) がコア schema 適用後に各プラグインの extra_schema を自動実行する。
    # 例: amazon_order_details / amazon_order_items は plugin.amazon の固有テーブルなので
    #     コアの src/db.py には書かず plugins/amazon/plugin.py の extra_schema に置く。
    extra_schema: tuple[str, ...] = ()
    # 既存DBへの後方互換 ALTER TABLE。OperationalError は握り潰される (カラム既存等)。
    extra_alters: tuple[str, ...] = ()
    api_version: int = PLUGIN_API_VERSION
