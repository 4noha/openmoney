"""master password 変更のテスト。

.env を一時ファイルに用意し、bootstrap → encrypt → change_master_password →
新パスワードでも正しく復号できる、を担保する。
"""
from __future__ import annotations

import pytest

from src import security


@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """空 master 状態の security モジュール + テスト用 .env パス。"""
    monkeypatch.setattr(security, "_master_password", None)
    monkeypatch.setattr(security, "_ui_locked", False)
    p = tmp_path / ".env"
    p.write_text(
        "AMAZON_EMAIL=test@example.com\n"
        "AMAZON_PW=secret_pw\n"
        "VPASS_ID=vpid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(security, "ENV_PATH", p)
    return p


def test_change_password_rewrites_all_encrypted_entries(fresh_env):
    """bootstrap で 3 件暗号化 → change → 全件 new で復号できる。"""
    security.unlock("oldpw", path=fresh_env)
    # 暗号化済 entry 数を確認
    enc_count_before = sum(1 for e in security.parse_env(fresh_env) if e["encrypted"])
    assert enc_count_before == 3

    n = security.change_master_password("oldpw", "newpw_xyz", path=fresh_env)
    assert n == 3

    # 全 entry が new で復号可能
    for e in security.parse_env(fresh_env):
        if e["encrypted"]:
            plain = security.decrypt_value(e["value"], "newpw_xyz")
            assert plain in ("test@example.com", "secret_pw", "vpid")

    # _master_password が new に切り替わっている
    assert security.get_master() == "newpw_xyz"


def test_change_password_rejects_wrong_old(fresh_env):
    """old_password が違うと PermissionError。"""
    security.unlock("realpw", path=fresh_env)
    with pytest.raises(PermissionError, match="old_password mismatch"):
        security.change_master_password("WRONG", "anything", path=fresh_env)
    # 値は元のまま (newpw で復号できない / oldpw で復号できる)
    for e in security.parse_env(fresh_env):
        if e["encrypted"]:
            security.decrypt_value(e["value"], "realpw")  # 例外なら fail


def test_change_password_rejects_empty_new(fresh_env):
    security.unlock("xx", path=fresh_env)
    with pytest.raises(ValueError):
        security.change_master_password("xx", "", path=fresh_env)


def test_change_password_rejects_empty_old(fresh_env):
    security.unlock("xx", path=fresh_env)
    with pytest.raises(ValueError):
        security.change_master_password("", "yy", path=fresh_env)


def test_change_password_rejects_same_old_and_new(fresh_env):
    security.unlock("samepw", path=fresh_env)
    with pytest.raises(ValueError, match="identical"):
        security.change_master_password("samepw", "samepw", path=fresh_env)


def test_change_password_requires_unlocked(fresh_env):
    """master 未ロード状態だと PermissionError。"""
    # bootstrap せずに呼ぶ
    with pytest.raises(PermissionError, match="master not loaded"):
        security.change_master_password("any", "thing", path=fresh_env)


def test_change_password_updates_keychain_if_present(fresh_env, monkeypatch):
    """Keychain に保存されている場合、new で上書きされる。"""
    security.unlock("oldpw", path=fresh_env)

    # keychain_has_master を True に、set_master の呼び出しを記録
    monkeypatch.setattr(security, "keychain_has_master", lambda: True)
    captured = []
    monkeypatch.setattr(security, "keychain_set_master",
                        lambda pw: captured.append(pw) or True)

    security.change_master_password("oldpw", "newpw", path=fresh_env)
    assert captured == ["newpw"]


def test_change_password_does_not_touch_keychain_if_absent(fresh_env, monkeypatch):
    """Keychain に未保存ならば set_master を呼ばない。"""
    security.unlock("oldpw", path=fresh_env)
    monkeypatch.setattr(security, "keychain_has_master", lambda: False)
    called = []
    monkeypatch.setattr(security, "keychain_set_master",
                        lambda pw: called.append(pw) or True)
    security.change_master_password("oldpw", "newpw", path=fresh_env)
    assert called == []
