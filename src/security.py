"""
.env のサービス個別パスワード暗号化。

形式: <base64(salt(16) + nonce(12) + ciphertext + tag(16))>🔒
- KDF: scrypt(N=2**16, r=8, p=1)
- AEAD: AES-256-GCM

マスターパスワードはメモリのみに保持。プロセス再起動で要再入力。
セッションタイムアウト無し。デーモン側はマスター未入力なら scrape 停止。

「初回マスターパスワード設定」という概念は無い。検証は、暗号化済みの
*_EMAIL キーを復号して中身に `@` が含まれるかどうかで判定する:
  - メールアドレスらしき復号成功 → 解錠
  - 復号失敗 / @ 無し          → ログイン失敗
  - .env に *_EMAIL が無い      → 「.env に email 登録してください」

使い方:
- パスワードを取得: src.security.get_secret("AMAZON_PW")
- 平文値を暗号化して .env に書き戻し: encrypt_env_key("AMAZON_PW")
- 解錠: unlock(pw) — 失敗で PermissionError
"""
from __future__ import annotations

import base64
import os
import re
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENV_PATH = Path(__file__).parent.parent / ".env"
SUFFIX = "🔒"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_lock = threading.Lock()
# プロセス起動以降、最初の unlock で設定される。プロセス終了まで保持。
# UI ロックでは絶対に消さない（スクレイプは引き続き動かすため）。
_master_password: str | None = None
# UI セッションのロック状態。lock_ui_session() でのみ True/False 切替。
_ui_locked: bool = False


# ─────────────────────────────────────────────
# 暗号プリミティブ
# ─────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**16, r=8, p=1)
    return kdf.derive(password.encode())


def encrypt_value(plaintext: str, password: str) -> str:
    """値を暗号化して 'b64(salt+nonce+ct+tag)🔒' の形式で返す。"""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    blob = base64.b64encode(salt + nonce + ct).decode()
    return blob + SUFFIX


def decrypt_value(ciphertext_with_marker: str, password: str) -> str:
    """暗号化された値を復号。形式不正・パスワード違いは例外。"""
    if not ciphertext_with_marker.endswith(SUFFIX):
        raise ValueError("not encrypted (no 🔒 suffix)")
    blob = base64.b64decode(ciphertext_with_marker[:-len(SUFFIX)].encode())
    if len(blob) < 16 + 12 + 16:
        raise ValueError("encrypted blob too short")
    salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode()
    except Exception as e:
        raise PermissionError("decryption failed (wrong password?)") from e


def is_encrypted(value: str) -> bool:
    return bool(value) and value.endswith(SUFFIX)


# ─────────────────────────────────────────────
# .env パース・書込
# ─────────────────────────────────────────────

def parse_env(path: Path = ENV_PATH) -> list[dict]:
    """.env を読み、key/value/encrypted を返す。コメント行・空行は除外。"""
    result: list[dict] = []
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        result.append({"key": key, "value": val, "encrypted": is_encrypted(val)})
    return result


def env_keys_status(path: Path = ENV_PATH) -> list[dict]:
    """設定画面用: key と encrypted フラグだけ返す（値は返さない）。"""
    return [
        {"key": e["key"], "encrypted": e["encrypted"]}
        for e in parse_env(path)
    ]


def write_env_value(key: str, new_value: str, path: Path = ENV_PATH) -> bool:
    """指定キーの値を新しい値に書き換える。既存無ければ末尾に追記。
    コメント・他行は保持する。書き換え成功で True。
    """
    lines = path.read_text().splitlines() if path.exists() else []
    out = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        cur_key, _, _ = stripped.partition("=")
        if cur_key.strip() == key:
            out.append(f"{key}={new_value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={new_value}")
    path.write_text("\n".join(out) + "\n")
    return True


# ─────────────────────────────────────────────
# マスターパスワード管理（検証は *_EMAIL の復号で行う）
# ─────────────────────────────────────────────

def _email_keys(entries: list[dict]) -> list[dict]:
    """*_EMAIL / EMAIL 系のキーだけ抽出（暗号化済 / 平文どちらも）。"""
    return [e for e in entries if e["key"].endswith("EMAIL")]


def has_email_in_env(path: Path = ENV_PATH) -> bool:
    """.env に email 系キー（平文 or 暗号化）が 1 件以上あるか。"""
    return any(e["value"] for e in _email_keys(parse_env(path)))


def has_encrypted_email(path: Path = ENV_PATH) -> bool:
    """.env に暗号化済の email 系キーがあるか（解錠が意味を持つかどうか）。"""
    return any(e["encrypted"] for e in _email_keys(parse_env(path)))


def has_plaintext_secrets(path: Path = ENV_PATH) -> bool:
    """暗号化対象になり得る平文値が残っているか（暗号化推奨ダイアログ用）。"""
    return any(
        not e["encrypted"] and e["value"]
        for e in parse_env(path)
    )


def plaintext_keys(path: Path = ENV_PATH) -> list[str]:
    return [
        e["key"] for e in parse_env(path)
        if not e["encrypted"] and e["value"]
    ]


def is_master_loaded() -> bool:
    """master password がプロセスメモリに居るか（= 復号値が os.environ に在る）。
    daemon 側のスクレイプ可否はこれで判定する。"""
    return _master_password is not None


def is_ui_unlocked() -> bool:
    """UI 経由のアクセスが許可されている状態か。lock_ui_session() で False に。"""
    return _master_password is not None and not _ui_locked


# 互換ラッパ: 既存呼び出しは UI 解錠状態として動作
def is_unlocked() -> bool:
    return is_ui_unlocked()


def get_master() -> str:
    """master を返す。未ロード（プロセス起動後 unlock 未実施）なら PermissionError。
    UI ロック中でもスクレイプ用には返す（master は消えない仕様）。"""
    pw = _master_password
    if pw is None:
        raise PermissionError("master not loaded")
    return pw


def lock_ui_session() -> None:
    """UI セッションのみロック。スクレイプは引き続き走るため
    _master_password / os.environ には触らない。"""
    global _ui_locked
    with _lock:
        _ui_locked = True


# 後方互換エイリアス
lock_master = lock_ui_session


def unlock(password: str, path: Path = ENV_PATH) -> None:
    """パスワードで .env を解錠する（UI ロック解除も含む）。
    - 暗号化された *_EMAIL がある → 復号して email 形式なら成功 (verify mode)
    - 全部平文 → bootstrap: このパスワードで全平文を暗号化して保存し解錠
    - そもそも *_EMAIL が無い → RuntimeError (.env 登録を促す)
    既に master ロード済 (UI ロック中など) ならパスワード照合のみ実施。
    解錠成功時、暗号化値を os.environ に復号注入し、UI ロックを解除する。
    """
    global _master_password, _ui_locked
    if not password:
        raise ValueError("empty password")
    # 既に master ロード済（UI ロック解除のみ）
    if _master_password is not None:
        if password != _master_password:
            raise PermissionError("master password mismatch")
        with _lock:
            _ui_locked = False
        return
    entries = parse_env(path)
    email_entries = _email_keys(entries)
    if not email_entries or not any(e["value"] for e in email_entries):
        raise RuntimeError("no *_EMAIL value in .env")

    enc_emails = [e for e in email_entries if e["encrypted"]]
    if enc_emails:
        # Verify モード: 暗号化済 *_EMAIL を復号して @ 一致なら OK
        last_err: Exception | None = None
        for e in enc_emails:
            try:
                plain = decrypt_value(e["value"], password)
            except Exception as exc:
                last_err = exc
                continue
            if _EMAIL_RE.match(plain):
                with _lock:
                    _master_password = password
                    _ui_locked = False
                _apply_decrypted_to_environ(path)
                return
            else:
                last_err = ValueError(f"{e['key']} 復号結果が email 形式ではない")
        raise PermissionError(f"unlock failed: {last_err}")

    # Bootstrap モード: まだ何も暗号化されてない。平文 email が email 形式か
    # 検証してから、入力された password で全平文を一括暗号化。
    plain_email = next(e for e in email_entries if e["value"])
    if not _EMAIL_RE.match(plain_email["value"]):
        raise ValueError(
            f"{plain_email['key']} の値が email 形式ではない (.env を確認)"
        )
    with _lock:
        _master_password = password
        _ui_locked = False
    for e in entries:
        if e["value"] and not e["encrypted"]:
            try:
                enc = encrypt_value(e["value"], password)
                write_env_value(e["key"], enc, path)
            except Exception as exc:
                print(f"[security] {e['key']} 暗号化失敗: {exc}")
    _apply_decrypted_to_environ(path)


def _apply_decrypted_to_environ(path: Path = ENV_PATH) -> None:
    """unlock 時に呼ぶ。.env の暗号化値を復号して os.environ に注入。"""
    pw = _master_password
    if pw is None:
        return
    for e in parse_env(path):
        if not e["encrypted"]:
            continue
        try:
            os.environ[e["key"]] = decrypt_value(e["value"], pw)
        except Exception as exc:
            print(f"[security] {e['key']} 復号失敗: {exc}")


# ─────────────────────────────────────────────
# 公開 API
# ─────────────────────────────────────────────

def get_secret(key: str) -> str:
    """環境変数の値を取得。暗号化されていれば復号して返す。
    暗号化されていて未解錠なら PermissionError。
    """
    val = os.environ.get(key, "")
    if not val:
        return val
    if is_encrypted(val):
        return decrypt_value(val, get_master())
    return val


# ─────────────────────────────────────────────
# OS Keychain 連携 (#24)
# 起動時に master password を OS Keychain から取り出すことで
# 「毎回 master password を打ち込まなくても scrape が回る」運用を可能にする。
# 開発用 --master-password-file に代わる安全な保管先。
# - macOS: keyring.backends.macOS.Keyring (Keychain Services)
# - Linux: SecretService / kwallet があれば。無ければ keyring.errors を投げる
# - Windows: WinVault Credential Manager
# 全部 keyring パッケージが backend を選ぶ。
# ─────────────────────────────────────────────

KEYCHAIN_SERVICE = "openmoney"
KEYCHAIN_USER = "master_password"


def keychain_available() -> bool:
    """OS Keychain backend が動作するか (set/get でテスト)。
    Linux で D-Bus が無い等のケースでは False。"""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        if isinstance(keyring.get_keyring(), FailKeyring):
            return False
        return True
    except Exception:
        return False


def keychain_has_master() -> bool:
    """OS Keychain に master password が保存されているか。"""
    if not keychain_available():
        return False
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER) is not None
    except Exception:
        return False


def keychain_get_master() -> str | None:
    """OS Keychain から master password を取得。無ければ None。"""
    if not keychain_available():
        return None
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    except Exception as e:
        print(f"[security] Keychain 読取失敗: {e}")
        return None


def keychain_set_master(password: str) -> bool:
    """OS Keychain に master password を保存 (上書き)。"""
    if not keychain_available():
        return False
    if not password:
        return False
    try:
        import keyring
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, password)
        return True
    except Exception as e:
        print(f"[security] Keychain 書込失敗: {e}")
        return False


def keychain_delete_master() -> bool:
    """OS Keychain から master password を削除 (UI から外したい時用)。"""
    if not keychain_available():
        return False
    try:
        import keyring
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except Exception as e:
        print(f"[security] Keychain 削除失敗: {e}")
        return False


def try_unlock_from_keychain() -> bool:
    """起動時に呼ぶ。Keychain に master があれば unlock を試みる。
    成功で True、失敗 / Keychain 不在で False (= 通常の /login フローに戻る)。
    Keychain の値が古い (.env の email と不整合) ケースは PermissionError で
    捕捉して False。
    """
    pw = keychain_get_master()
    if not pw:
        return False
    try:
        unlock(pw)
        return True
    except Exception as e:
        print(f"[security] Keychain で解錠失敗 (.env と不整合?): {e}")
        return False


def encrypt_env_key(key: str, path: Path = ENV_PATH) -> bool:
    """指定キーの平文値を暗号化して .env に書き戻す。
    既に暗号化済みなら何もしない。マスター未解錠なら PermissionError。
    成功時 True / 値が空 or 既暗号化なら False。
    """
    pw = get_master()
    entries = parse_env(path)
    target = next((e for e in entries if e["key"] == key), None)
    if not target or not target["value"]:
        return False
    if target["encrypted"]:
        return False
    enc = encrypt_value(target["value"], pw)
    write_env_value(key, enc, path)
    # メモリ上の os.environ も平文のままにしておく（既に scraper が使ってる可能性）
    return True


# ─────────────────────────────────────────────
# master password 変更
# ─────────────────────────────────────────────

def change_master_password(old_password: str, new_password: str,
                            path: Path = ENV_PATH) -> int:
    """master password を変更する。
    1. old_password が現在の master と一致するか検証
    2. .env の暗号化済 entry を全て old で復号 → new で暗号化 → 書戻し
    3. _master_password を new に差し替え
    4. Keychain に master が保存されていれば new で上書き

    戻り値: 再暗号化した entry 数
    例外:
      ValueError: empty new_password / old_password
      PermissionError: old_password が違う / master 未ロード / 既存値復号失敗
    """
    global _master_password
    if not new_password:
        raise ValueError("empty new_password")
    if not old_password:
        raise ValueError("empty old_password")
    cur = _master_password
    if cur is None:
        raise PermissionError("master not loaded")
    if old_password != cur:
        raise PermissionError("old_password mismatch")
    if old_password == new_password:
        raise ValueError("new_password is identical to old_password")

    # 1. すべての暗号化値を old で復号 (どれかが失敗したら何も書き戻さず中止)
    entries = parse_env(path)
    decrypted: list[tuple[str, str]] = []
    for e in entries:
        if not e["encrypted"]:
            continue
        try:
            plain = decrypt_value(e["value"], old_password)
        except Exception as exc:
            raise PermissionError(
                f"既存値の復号に失敗 ({e['key']}): {exc}"
            ) from exc
        decrypted.append((e["key"], plain))

    # 2. new で全部暗号化して書戻し (write_env_value が全行書き換え)
    n_rewritten = 0
    for key, plain in decrypted:
        enc = encrypt_value(plain, new_password)
        write_env_value(key, enc, path)
        n_rewritten += 1

    # 3. プロセス内 master を差し替え (os.environ の平文値は変わらず継続動作)
    with _lock:
        _master_password = new_password

    # 4. Keychain に保存されていれば new で上書き (失敗しても続行)
    try:
        if keychain_has_master():
            keychain_set_master(new_password)
    except Exception as exc:
        print(f"[security] Keychain 更新失敗 (継続): {exc}")

    return n_rewritten
