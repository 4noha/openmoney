"""
OpenMoney デスクトップアプリ

既存の FastAPI サーバー + デーモンをバックグラウンドスレッドで起動し、
pywebview のネイティブウィンドウで UI を表示する。

起動:
    uv run python desktop.py

デバッグ:
    ウィンドウ内で右クリック → 「要素を検証」 (DevTools)
    ターミナルにサーバーログが流れる

Linux の前提パッケージ (Ubuntu/Debian):
    sudo apt install python3-gi python3-gi-cairo \
        gir1.2-gtk-3.0 gir1.2-webkit2-4.1
"""
import asyncio
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()


def _start_daemon() -> None:
    from src.daemon import (
        _maybe_unlock_from_arg,
        _maybe_unlock_from_keychain,
        main,
    )
    _maybe_unlock_from_arg()
    _maybe_unlock_from_keychain()
    asyncio.run(main())


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> None:
    try:
        import webview  # noqa: F401
    except ImportError:
        print(
            "[desktop] pywebview が見つかりません。\n"
            "  uv add pywebview  # または pip install pywebview\n"
            "Linux では追加で以下のシステムパッケージが必要です:\n"
            "  sudo apt install python3-gi python3-gi-cairo "
            "gir1.2-gtk-3.0 gir1.2-webkit2-4.1",
            file=sys.stderr,
        )
        sys.exit(1)

    from src.server import PORT

    # daemon (server + Firestore relay + スクレイピングループ) をバックグラウンドで起動
    t = threading.Thread(target=_start_daemon, daemon=True)
    t.start()

    print(f"[desktop] サーバー起動待機中 (port {PORT})...")
    if not _wait_for_server(PORT):
        print(
            f"[desktop] サーバーの起動確認に失敗しました (port {PORT})。\n"
            "ブラウザで http://127.0.0.1:{PORT}/ui を直接開いてください。",
            file=sys.stderr,
        )

    # pywebview は Linux では GTK/WebKit2GTK を使うためメインスレッドで実行する
    window = webview.create_window(
        "OpenMoney",
        f"http://127.0.0.1:{PORT}/ui",
        width=1280,
        height=900,
        min_size=(800, 600),
    )  # noqa: F841 (window is used implicitly by webview.start)

    # debug=True で右クリック → 「要素を検証」 (DevTools) が使える
    webview.start(debug=True)


if __name__ == "__main__":
    main()
