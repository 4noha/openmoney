"""
デーモン: 2週間おきに全スクレイパーを自動実行する。
  Android アプリの「今すぐ更新」ボタンから /run-now POST で即時実行も可能。
  1. VPASS を実行（Android 承認不要）
  2. Orico / MUFGAmex / Amazon を実行（OTP はサーバー経由で自動処理）
  3. Mercari: FCM で Android に Push → Accept 待機 → 実行
  4. MUFG: FCM で Android に Push → Accept 待機 → 実行
"""
import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

def _load_interval() -> float:
    if val := os.environ.get("DAEMON_INTERVAL_HOURS"):
        return float(val)
    from pathlib import Path
    import json
    cfg = Path(__file__).parent.parent / "config.json"
    if cfg.exists():
        return float(json.loads(cfg.read_text()).get("DAEMON_INTERVAL_HOURS", 336))
    return 336

INTERVAL_HOURS = _load_interval()
ACCEPT_TIMEOUT_SEC = 600  # 10 分


# ─────────────────────────────────────────────
# DB helpers (server.py と共用)
# ─────────────────────────────────────────────

def _db_set(key: str, value: str) -> None:
    from src.server import _set
    _set(key, value)


def _db_get(key: str) -> str | None:
    from src.server import _get
    return _get(key)


# ─────────────────────────────────────────────
# FCM push helpers
# ─────────────────────────────────────────────

def _push_login_request(request_id: str, title: str = "MUFG ログイン開始") -> bool:
    from src.fcm import send
    token = _db_get("device_token")
    if not token:
        print("[daemon] FCM token 未登録 — Android アプリを一度起動してください")
        return False
    return send(
        token,
        title=title,
        body="タップして承認してください",
        data={"type": "login_request", "request_id": request_id},
    )


# ─────────────────────────────────────────────
# Accept 待機
# ─────────────────────────────────────────────

def _wait_for_accept(request_id: str) -> bool:
    deadline = time.time() + ACCEPT_TIMEOUT_SEC
    print(f"[daemon] Accept 待機中（最大 {ACCEPT_TIMEOUT_SEC // 60} 分）...")
    while time.time() < deadline:
        if _db_get("accept_status") == "accepted" and _db_get("pending_request_id") == request_id:
            _db_set("accept_status", "")
            print("[daemon] Accept 受信")
            return True
        time.sleep(5)
    print("[daemon] Accept タイムアウト")
    return False


# ─────────────────────────────────────────────
# scraper 個別の実行状態を daemon_state に記録
# ─────────────────────────────────────────────

def _is_service_enabled(name: str) -> bool:
    """service_enabled テーブルで明示的に enabled=0 なら False、それ以外 True (既定)。"""
    import sqlite3
    from src.db import DB_PATH
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = con.execute(
            "SELECT enabled FROM service_enabled WHERE name=?", (name,)
        ).fetchone()
    except sqlite3.OperationalError:
        # 起動前で table 未作成の可能性
        return True
    finally:
        con.close()
    return row is None or bool(row[0])


async def _run_with_user_accept(display_name: str) -> bool:
    """FCM プッシュ → Android アプリ側の Accept ボタン待機。
    成功で True、失敗で False。Mercari / MUFG など requires_user_accept=True
    のプラグインから呼ばれる共通フロー。"""
    request_id = uuid.uuid4().hex[:12]
    _db_set("pending_request_id", request_id)
    _db_set("accept_status", "")
    pushed = _push_login_request(request_id, title=f"{display_name} ログイン開始")
    if not pushed:
        print(f"[daemon] Push 送信失敗。{display_name} をスキップします。")
        return False
    accepted = await asyncio.get_event_loop().run_in_executor(
        None, _wait_for_accept, request_id
    )
    if not accepted:
        print(f"[daemon] 承認されませんでした。{display_name} をスキップします。")
        return False
    return True


async def _run_scraper(
    name: str,
    env_var: str | None,
    scraper_call,
    *,
    requires_user_accept: bool = False,
    display_name: str | None = None,
):
    """scraper を呼び出し、成否・件数・所要時間を scrape:<name>:* に記録する。
    env_var が None でない場合、その環境変数が空なら skipped 扱い。
    scraper_call は await で結果（list / tuple）を返す callable。

    requires_user_accept=True なら Android Accept フローを先に走らせる。

    マスターパスワード未設定 (.env が暗号化済みだが unlock されていない) なら
    scrape は走らせず locked 扱い。設定画面から /api/security/unlock を経由して
    unlock してから再実行する想定。

    service_enabled で disabled な service もスキップする。

    キー命名は src.scrape_state（スクレイパー側 run() 末尾でも書き込む）と統一。
    """
    from src.scrape_state import (
        mark_scrape_started, mark_scrape_done, mark_scrape_error,
    )
    from src import security as _security
    # 設定 UI で無効化されていれば実行しない
    if not _is_service_enabled(name):
        _db_set(f"scrape:{name}:status", "disabled")
        _db_set(f"scrape:{name}:last_done_at", datetime.now().isoformat())
        print(f"[daemon] {name} 無効化済 (設定画面で OFF)")
        return None
    # 暗号化済 .env だが master が一度もロードされていない → スクレイプ不可。
    # UI ロック中 (is_ui_unlocked=False, is_master_loaded=True) でも
    # スクレイプは継続する仕様 (master 自体はメモリに残してある)。
    if _security.has_encrypted_email() and not _security.is_master_loaded():
        _db_set(f"scrape:{name}:status", "locked")
        _db_set(f"scrape:{name}:last_done_at", datetime.now().isoformat())
        print(f"[daemon] {name} ロック中 (master 未ロード)")
        return None
    if env_var is not None and not os.environ.get(env_var):
        _db_set(f"scrape:{name}:status", "skipped")
        _db_set(f"scrape:{name}:last_done_at", datetime.now().isoformat())
        print(f"[daemon] {name} スキップ ({env_var} 未設定)")
        return None

    if requires_user_accept:
        ok = await _run_with_user_accept(display_name or name)
        if not ok:
            _db_set(f"scrape:{name}:status", "user_declined")
            _db_set(f"scrape:{name}:last_done_at", datetime.now().isoformat())
            return None

    start = time.time()
    mark_scrape_started(name)
    try:
        result = await scraper_call()
        # 件数推定（list / tuple-of-lists / その他）
        if isinstance(result, list):
            count = len(result)
        elif isinstance(result, tuple):
            count = sum(len(x) for x in result if hasattr(x, "__len__"))
        else:
            count = 0
        elapsed = int(time.time() - start)
        # scraper.run() 内でも mark_scrape_done を呼ぶが、ここでも上書きして
        # daemon 経由実行であることを elapsed 等に残す
        mark_scrape_done(name, count=count, extra={"elapsed_sec": elapsed})
        print(f"[daemon] {name} 完了: {count} 件 ({elapsed}s)")
        return result
    except Exception as e:
        elapsed = int(time.time() - start)
        msg = str(e)[:200]
        mark_scrape_error(name, msg)
        _db_set(f"scrape:{name}:elapsed_sec", str(elapsed))
        print(f"[daemon] {name} エラー ({elapsed}s): {msg}")
        return None


# ─────────────────────────────────────────────
# 1 回分の実行
# ─────────────────────────────────────────────

async def _run_once() -> None:
    print(f"\n[daemon] ===== 実行開始 {datetime.now():%Y-%m-%d %H:%M} =====")

    # 試行した時点で last_run を更新しておく（失敗してもタイトループを防ぐ）
    _db_set("last_run", datetime.now().isoformat())

    try:
        # 全プラグインを registry 経由で順次実行。実行順は SCRAPE_ORDER で固定し
        # （依存関係・ヤフオク/Yahoo!共通セッション等のため）、それ以外は末尾で
        # alphabetical に流す。requires_user_accept=True のものは _run_scraper
        # 内部で FCM Accept フローを通過してから scrape する。
        from src.plugins_loader import get_registry
        SCRAPE_ORDER = (
            "makuake",
            "campfire",
            "rakuten",
            "vpass",
            "amazon",           # OTP は FCM + /amazon-otp で自動処理
            "aliexpress",       # 初回/セッション切れは headed
            "mercari",          # メルカリ + メルカード (パスキー requires_user_accept=True)
            "mufg",             # Android 承認: requires_user_accept=True
        )
        registry = get_registry()
        ran: set[str] = set()
        for name in SCRAPE_ORDER:
            spec = registry.get(name)
            if spec is None:
                continue
            env_key = spec.env_keys[0].key if spec.env_keys else None
            await _run_scraper(
                spec.name, env_key, spec.runner,
                requires_user_accept=spec.requires_user_accept,
                display_name=spec.display_name,
            )
            ran.add(name)
        # SCRAPE_ORDER 未掲載の追加プラグインも末尾で流す
        for name, spec in sorted(registry.items()):
            if name in ran:
                continue
            env_key = spec.env_keys[0].key if spec.env_keys else None
            await _run_scraper(
                spec.name, env_key, spec.runner,
                requires_user_accept=spec.requires_user_accept,
                display_name=spec.display_name,
            )

    finally:
        # 突合エンジン: ショップ↔カード、紙レシート↔カードなど
        # 個別失敗で他を止めないよう各エンジンを独立にラップ
        from src.matching import (
            run_matching, run_shop_matching,
            match_receipts_to_cards, run_amazon_multi_shipment_matching,
            match_amazon_preorders,
        )
        for label, fn, kwargs in (
            ("card_bank",            run_matching,                              {}),
            ("shop_card",            run_shop_matching,                         {}),
            ("amazon_multi_ship",    run_amazon_multi_shipment_matching,       {"verbose": False}),
            ("amazon_preorders",     match_amazon_preorders,                   {"verbose": False}),
            ("receipts_card",        match_receipts_to_cards,                  {"verbose": False}),
        ):
            try:
                fn(**kwargs)
                _db_set(f"matching:{label}:status", "success")
                _db_set(f"matching:{label}:last_run", datetime.now().isoformat())
            except Exception as e:
                _db_set(f"matching:{label}:status", "error")
                _db_set(f"matching:{label}:last_error", str(e)[:200])
                _db_set(f"matching:{label}:last_run", datetime.now().isoformat())
                print(f"[daemon] matching {label} エラー: {e}")

        # 確定申告 経費明細を最新の transactions に同期（金額・日付・支払方法）
        try:
            import requests
            r = requests.post("http://127.0.0.1:8765/api/tax/sync", timeout=30)
            if r.ok:
                d = r.json()
                if d.get("items_added") or d.get("items_updated"):
                    print(f"[daemon] tax sync: +{d['items_added']} 更新{d.get('items_updated', 0)}")
        except Exception as e:
            print(f"[daemon] tax sync エラー: {e}")

        # スクレイピング結果（成功・失敗・スキップ問わず）キャッシュをバックグラウンドで再構築
        from src.server import _rebuild_tx_cache
        threading.Thread(target=_rebuild_tx_cache, daemon=True).start()
        print("[daemon] キャッシュ再構築をバックグラウンドで開始")


# ─────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────

def _should_run_now() -> bool:
    if _db_get("run_now") == "1":
        _db_set("run_now", "")
        return True
    last = _db_get("last_run")
    if not last:
        return True
    elapsed = datetime.now() - datetime.fromisoformat(last)
    return elapsed >= timedelta(hours=INTERVAL_HOURS)


def _seconds_until_next_run() -> float:
    last = _db_get("last_run")
    if not last:
        return 0
    elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
    wait = INTERVAL_HOURS * 3600 - elapsed
    return max(0.0, wait)


async def _sleep_interruptible(wait_sec: float) -> None:
    """60 秒ごとに run_now フラグを確認しながらスリープ。"""
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if _db_get("run_now") == "1":
            return
        remaining = deadline - time.time()
        await asyncio.sleep(min(60, max(0, remaining)))


async def main() -> None:
    # HTTP サーバーをバックグラウンドスレッドで起動
    from src.server import run_server
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    print(f"[daemon] HTTP サーバー起動 port={os.environ.get('DAEMON_PORT', 8765)}")

    # Firestore リレーをバックグラウンドスレッドで起動
    from src.firestore_relay import run_relay
    relay_thread = threading.Thread(target=run_relay, daemon=True)
    relay_thread.start()

    # OpenMoney backend に Tailscale URL + LAN URL を申告
    # (= 起動直後 1 回 + 1h 周期で再検出。 LAN は IP 変動 (DHCP) を拾うため定期更新)
    def _network_self_loop():
        from src.openmoney_client import register_tailscale_self, register_lan_self
        for fn, label in ((register_tailscale_self, "tailscale"), (register_lan_self, "lan")):
            try:
                fn()
            except Exception as e:
                print(f"[{label}] 起動時申告で例外: {type(e).__name__}: {e}")
        while True:
            time.sleep(3600)
            for fn, label in ((register_tailscale_self, "tailscale"), (register_lan_self, "lan")):
                try:
                    fn()
                except Exception as e:
                    print(f"[{label}] 周期申告で例外: {type(e).__name__}: {e}")

    threading.Thread(target=_network_self_loop, daemon=True).start()

    print(f"[daemon] デーモン開始 (間隔={INTERVAL_HOURS}h)")

    while True:
        if _should_run_now():
            await _run_once()
        wait_sec = _seconds_until_next_run()
        next_run = datetime.now() + timedelta(seconds=wait_sec)
        print(f"[daemon] 次回実行: {next_run:%Y-%m-%d %H:%M} ({wait_sec / 3600:.1f}h 後)")
        await _sleep_interruptible(wait_sec)


def _maybe_unlock_from_arg() -> None:
    """開発用: --master-password-file <path> でマスターパスワードを起動時注入。
    指定ファイルから読み込んで src.security.unlock() を呼び、ログインゲートを
    通過済み状態にしてから daemon を回す。本番では絶対に使わない想定。"""
    import argparse
    from pathlib import Path
    from src import security
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--master-password-file", type=Path, default=None,
                        help="開発用: 起動時にこのファイルからマスターパスワードを読んで解錠")
    args, _ = parser.parse_known_args()
    if not args.master_password_file:
        return
    try:
        pw = args.master_password_file.read_text().strip()
    except Exception as e:
        print(f"[daemon] パスワードファイル読込失敗: {e}")
        return
    if not pw:
        print("[daemon] パスワードファイルが空")
        return
    try:
        security.unlock(pw)
        print(f"[daemon] 開発用マスターパスワードで解錠 ({args.master_password_file.name})")
    except Exception as e:
        print(f"[daemon] 解錠失敗: {e}")


def _maybe_unlock_from_keychain() -> None:
    """OS Keychain に master password が保存されていれば自動解錠する (#24)。
    --master-password-file が無く、Keychain にあるときに動作。"""
    from src import security
    if security.is_master_loaded():
        return  # 既に他の手段で解錠済 (--master-password-file 等)
    if security.try_unlock_from_keychain():
        print("[daemon] OS Keychain から master password を取得して解錠")


if __name__ == "__main__":
    _maybe_unlock_from_arg()
    _maybe_unlock_from_keychain()
    asyncio.run(main())
