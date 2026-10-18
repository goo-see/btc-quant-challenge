from __future__ import annotations

import hashlib

import jwt

from app.exchange.upbit.auth import build_query_string, create_jwt, query_hash


def test_query_string_preserves_insertion_order_and_arrays() -> None:
    qs = build_query_string(
        {"market": "KRW-BTC", "states": ["wait", "watch"], "limit": 100, "skip": None}
    )
    assert qs == "market=KRW-BTC&states[]=wait&states[]=watch&limit=100"


def test_query_hash_is_sha512_hex_of_exact_string() -> None:
    qs = "market=KRW-BTC&count=3"
    assert query_hash(qs) == hashlib.sha512(qs.encode()).hexdigest()


def test_jwt_payload_fields_and_hs512() -> None:
    token = create_jwt("AK", "SK", {"market": "KRW-BTC"})
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "HS512"
    payload = jwt.decode(token, "SK", algorithms=["HS512"])
    assert payload["access_key"] == "AK"
    assert len(payload["nonce"]) == 36
    assert payload["query_hash_alg"] == "SHA512"
    assert payload["query_hash"] == query_hash("market=KRW-BTC")


def test_jwt_without_params_has_no_query_hash() -> None:
    payload = jwt.decode(create_jwt("AK", "SK"), "SK", algorithms=["HS512"])
    assert "query_hash" not in payload


def test_nonce_unique_per_request() -> None:
    a = jwt.decode(create_jwt("AK", "SK"), "SK", algorithms=["HS512"])["nonce"]
    b = jwt.decode(create_jwt("AK", "SK"), "SK", algorithms=["HS512"])["nonce"]
    assert a != b
