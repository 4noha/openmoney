"""src.security の AES-256-GCM + scrypt 暗号化と unlock フローのテスト。"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def reset_security_state(monkeypatch):
    """各テストで src.security を re-import し、グローバル状態を初期化。"""
    import src.security as sec
    monkeypatch.setattr(sec, "_master_password", None, raising=False)
    monkeypatch.setattr(sec, "_ui_locked", False, raising=False)
    yield


def test_encrypt_decrypt_roundtrip():
    from src import security
    pw = "hunter2"
    plain = "topsecret@example.com"
    enc = security.encrypt_value(plain, pw)
    assert enc.endswith(security.SUFFIX)
    assert security.is_encrypted(enc)
    assert not security.is_encrypted(plain)
    out = security.decrypt_value(enc, pw)
    assert out == plain


def test_decrypt_with_wrong_password_raises():
    from src import security
    enc = security.encrypt_value("secret", "right")
    with pytest.raises(PermissionError):
        security.decrypt_value(enc, "wrong")


def test_unlock_bootstrap_encrypts_plaintext(tmp_env_path):
    """初回 unlock: 平文 .env が encrypted に書き換わり、master が乗る。"""
    from src import security
    tmp_env_path.write_text(
        "AMAZON_EMAIL=user@example.com\n"
        "AMAZON_PW=plainpass\n"
    )
    security.unlock("masterpw", path=tmp_env_path)
    assert security.is_master_loaded()
    assert security.is_ui_unlocked()
    text = tmp_env_path.read_text()
    # email も pw も 🔒 サフィックスで暗号化済
    for line in text.splitlines():
        if line.startswith("AMAZON_"):
            assert line.endswith(security.SUFFIX), line


def test_unlock_verify_with_correct_password(tmp_env_path):
    """暗号化済 .env を正しい PW で再 unlock できる。"""
    from src import security
    tmp_env_path.write_text("AMAZON_EMAIL=u@v.com\nAMAZON_PW=p\n")
    security.unlock("pw1", path=tmp_env_path)
    # ロック → 再 unlock
    security.lock_ui_session()
    assert not security.is_ui_unlocked()
    assert security.is_master_loaded()  # master は残る
    security.unlock("pw1", path=tmp_env_path)
    assert security.is_ui_unlocked()


def test_unlock_verify_with_wrong_password(tmp_env_path):
    from src import security
    tmp_env_path.write_text("AMAZON_EMAIL=u@v.com\nAMAZON_PW=p\n")
    security.unlock("right", path=tmp_env_path)
    # ロック → 別プロセス想定で master 消去
    security._master_password = None
    security._ui_locked = False
    with pytest.raises(PermissionError):
        security.unlock("wrong", path=tmp_env_path)


def test_unlock_no_email_in_env(tmp_env_path):
    from src import security
    tmp_env_path.write_text("AMAZON_PW=onlypw\n")
    with pytest.raises(RuntimeError):
        security.unlock("anything", path=tmp_env_path)


def test_unlock_email_format_validation(tmp_env_path):
    from src import security
    tmp_env_path.write_text("AMAZON_EMAIL=not-an-email\n")
    with pytest.raises(ValueError):
        security.unlock("pw", path=tmp_env_path)


def test_lock_ui_keeps_master_loaded(tmp_env_path):
    """UI ロックは master を消さない (scrape 継続可)。"""
    from src import security
    tmp_env_path.write_text("AMAZON_EMAIL=u@v.com\n")
    security.unlock("pw", path=tmp_env_path)
    assert security.is_master_loaded()
    security.lock_ui_session()
    assert security.is_master_loaded()
    assert not security.is_ui_unlocked()
    # get_master() は UI ロック中でも値を返す (scrape 用)
    assert security.get_master() == "pw"


def test_parse_env_skips_comments_and_blanks(tmp_env_path):
    from src import security
    tmp_env_path.write_text(
        "# comment\n"
        "\n"
        "AMAZON_EMAIL=user@example.com\n"
        "  # indented comment\n"
        "EMPTY=\n"
        "QUOTED=\"value with spaces\"\n"
    )
    entries = security.parse_env(tmp_env_path)
    keys = [e["key"] for e in entries]
    assert "AMAZON_EMAIL" in keys
    assert "EMPTY" in keys  # 値空でも entry には残る (UI 表示用)
    assert "QUOTED" in keys
