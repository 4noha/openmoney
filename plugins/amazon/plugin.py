from __future__ import annotations
from src.plugin_api import EnvKeySpec, ServiceSpec
from plugins.amazon.scraper import run as _run

async def _runner():
    return await _run(headless=True)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS amazon_order_details (
        order_id      TEXT PRIMARY KEY,
        order_date    TEXT,
        item_subtotal INTEGER DEFAULT 0,
        shipping      INTEGER DEFAULT 0,
        discount      INTEGER DEFAULT 0,
        points_used   INTEGER DEFAULT 0,
        gift_card     INTEGER DEFAULT 0,
        order_total   INTEGER DEFAULT 0,
        items_fetched INTEGER DEFAULT 0,
        fetched_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_order_items (
        order_id  TEXT NOT NULL,
        seq       INTEGER NOT NULL,
        title     TEXT,
        price     INTEGER DEFAULT 0,
        quantity  INTEGER DEFAULT 1,
        is_kindle INTEGER DEFAULT 0,
        PRIMARY KEY (order_id, seq)
    )
    """,
)

_ALTERS = (
    "ALTER TABLE amazon_order_details ADD COLUMN items_fetched INTEGER DEFAULT 0",
    "ALTER TABLE amazon_order_items ADD COLUMN quantity INTEGER DEFAULT 1",
)

PLUGIN = ServiceSpec(
    name="amazon",
    display_name="Amazon",
    description="注文履歴・領収書 PDF・Amazon Pay 提携サイト",
    runner=_runner,
    env_keys=[
        EnvKeySpec("AMAZON_EMAIL", "メール", type="email"),
        EnvKeySpec("AMAZON_PW",    "パスワード", type="password"),
    ],
    provided_banks=("Amazon", "AmazonPay",),
    history_url="https://www.amazon.co.jp/your-orders/orders",
    detail_url_template="https://www.amazon.co.jp/gp/your-account/order-details?orderID={order_id}",
    requires_otp=True,  # SMS は FCM 経由で自動処理
    extra_schema=_SCHEMA,
    extra_alters=_ALTERS,
)
