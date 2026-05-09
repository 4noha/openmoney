"""OS Keychain 統合 (#24) のテスト。

実 Keychain には触らず monkeypatch で keyring API を差し替えて、
security モジュールのラッパーとフロー (try_unlock_from_keychain 等) を検証。
"""
from __future__ import annotations

import pytest

from src import security


class _FakeKeyring:
    """In-memory keyring backend (テスト用)。"""
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            from keyring.errors import PasswordDeleteError
            raise PasswordDeleteError(f"missing {service}/{user}")
        del self.store[(service, user)]


@pytest.fixture
def fake_keyring(monkeypatch):
    """keyring パッケージの呼出を全て fake で受ける fixture。"""
    fk = _FakeKeyring()
    import keyring as _kr
    monkeypatch.setattr(_kr, "get_password", fk.get_password)
    monkeypatch.setattr(_kr, "set_password", fk.set_password)
    monkeypatch.setattr(_kr, "delete_password", fk.delete_password)

    # keychain_available の判定で実 backend を見にいくのも回避
    class _DummyBackend:
        pass
    monkeypatch.setattr(_kr, "get_keyring", lambda: _DummyBackend())
    return fk


def test_keychain_available_true_with_real_backend(fake_keyring):
    assert security.keychain_available() is True


def test_keychain_set_and_get(fake_keyring):
    assert security.keychain_get_master() is None
    assert security.keychain_has_master() is False
    ok = security.keychain_set_master("hunter2")
    assert ok is True
    assert security.keychain_has_master() is True
    assert security.keychain_get_master() == "hunter2"


def test_keychain_set_empty_returns_false(fake_keyring):
    """空 password は保存しない (誤操作防止)。"""
    assert security.keychain_set_master("") is False
    assert security.keychain_has_master() is False


def test_keychain_delete_returns_true_when_present(fake_keyring):
    security.keychain_set_master("xx")
    assert security.keychain_delete_master() is True
    assert security.keychain_has_master() is False


def test_keychain_delete_returns_false_when_missing(fake_keyring):
    """未保存状態での削除は False (例外にしない)。"""
    assert security.keychain_delete_master() is False


def test_try_unlock_from_keychain_returns_false_if_no_keychain(monkeypatch):
    monkeypatch.setattr(security, "keychain_available", lambda: False)
    assert security.try_unlock_from_keychain() is False


def test_try_unlock_from_keychain_returns_false_if_empty(fake_keyring):
    """Keychain は使えるが master 未保存なら False。"""
    assert security.try_unlock_from_keychain() is False


def test_try_unlock_from_keychain_calls_unlock(fake_keyring, monkeypatch):
    """Keychain から取り出した値を unlock() に渡す。"""
    security.keychain_set_master("test_master")
    called = []
    def fake_unlock(pw, *args, **kw):
        called.append(pw)
    monkeypatch.setattr(security, "unlock", fake_unlock)
    out = security.try_unlock_from_keychain()
    assert out is True
    assert called == ["test_master"]


def test_try_unlock_from_keychain_handles_unlock_failure(fake_keyring, monkeypatch):
    """Keychain の値が古くて unlock 失敗 (PermissionError) → False を返して継続可能に。"""
    security.keychain_set_master("stale_password")
    def fake_unlock(pw, *args, **kw):
        raise PermissionError("wrong password")
    monkeypatch.setattr(security, "unlock", fake_unlock)
    assert security.try_unlock_from_keychain() is False
