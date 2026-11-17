"""_gunzip_bounded: .gz evidence is decompressed, and gzip bombs and garbage are refused."""

import gzip

import pytest

from routes.incidents_routes import _gunzip_bounded

_LIMIT = 1024


def test_valid_gzip_is_decompressed():
    assert _gunzip_bounded(gzip.compress(b"ERROR OOMKilled\n"), _LIMIT) == b"ERROR OOMKilled\n"


def test_gzip_bomb_is_refused():
    bomb = gzip.compress(b"a" * (_LIMIT + 1))  # a few bytes compressed, over the limit expanded
    assert len(bomb) < 100
    with pytest.raises(ValueError):
        _gunzip_bounded(bomb, _LIMIT)


def test_garbage_is_refused():
    with pytest.raises(OSError):
        _gunzip_bounded(b"this is not gzip", _LIMIT)
