"""
tests/utils/core/test_crypto.py

测试 utils/core/crypto.py（共享 Fernet 加密工具）。

测试通过 monkeypatch 把密钥路径指向临时文件，并重置模块级 _fernetCache，
避免触碰真实的 data/.chatKey，也避免用例之间互相串扰缓存。
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken

import utils.core.crypto as crypto


@pytest.fixture
def tmpKey(tmp_path, monkeypatch):
    """把密钥指向临时文件并清空缓存，返回密钥路径。"""
    keyPath = str(tmp_path / ".chatKey")
    monkeypatch.setattr(crypto, "KEY_PATH", keyPath)
    monkeypatch.setattr(crypto, "_fernetCache", None)
    return keyPath


# ============================================================================
# loadOrCreateKey
# ============================================================================

def test_load_or_create_key_generates_when_missing(tmpKey):
    """密钥文件不存在时自动生成有效的 Fernet 密钥。"""
    import os
    assert not os.path.exists(tmpKey)

    key = crypto.loadOrCreateKey()

    assert os.path.exists(tmpKey)
    # 生成的密钥可直接构造 Fernet（格式合法）
    Fernet(key)


def test_load_or_create_key_reuses_existing(tmpKey):
    """密钥文件已存在时复用，不重新生成。"""
    first = crypto.loadOrCreateKey()
    second = crypto.loadOrCreateKey()
    assert first == second


# ============================================================================
# getFernet 缓存
# ============================================================================

def test_get_fernet_is_cached(tmpKey):
    """getFernet 返回同一个缓存实例。"""
    f1 = crypto.getFernet()
    f2 = crypto.getFernet()
    assert f1 is f2


# ============================================================================
# 加解密往返
# ============================================================================

def test_encrypt_text_roundtrip(tmpKey):
    """encryptText / decryptText 往返还原明文。"""
    plaintext = "几点钟要写作业喵"
    token = crypto.encryptText(plaintext)

    # 密文应为 bytes 且不等于明文
    assert isinstance(token, bytes)
    assert token != plaintext.encode("utf-8")

    assert crypto.decryptText(token) == plaintext


def test_encrypt_bytes_roundtrip(tmpKey):
    """encrypt / decrypt 在字节层面往返还原。"""
    data = b"\x00\x01binary payload\xff"
    token = crypto.encrypt(data)
    assert crypto.decrypt(token) == data


def test_encrypt_empty_string(tmpKey):
    """空字符串也能正确往返。"""
    token = crypto.encryptText("")
    assert crypto.decryptText(token) == ""


def test_decrypt_invalid_token_raises(tmpKey):
    """非法密文应抛 InvalidToken。"""
    with pytest.raises(InvalidToken):
        crypto.decryptText(b"not-a-valid-fernet-token")


def test_decrypt_with_different_key_raises(tmpKey, tmp_path, monkeypatch):
    """用 A 密钥加密、切到 B 密钥解密应失败（验证密钥确实生效）。"""
    token = crypto.encryptText("secret")

    # 切换到另一把全新密钥
    monkeypatch.setattr(crypto, "KEY_PATH", str(tmp_path / ".otherKey"))
    monkeypatch.setattr(crypto, "_fernetCache", None)

    with pytest.raises(InvalidToken):
        crypto.decryptText(token)