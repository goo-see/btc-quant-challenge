"""Upbit JWT authentication (docs.upbit.com/kr/reference/auth, checked 2026-09-24).

payload = {access_key, nonce(uuid4), query_hash(sha512 hex), query_hash_alg="SHA512"}
alg = HS512; secret used raw. query_hash is computed over the EXACT query string sent,
without sorting. Arrays use repeated keys: states[]=wait&states[]=watch.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import jwt

QueryValue = str | int | float | Sequence[str]


def build_query_string(params: Mapping[str, Any]) -> str:
    """Deterministic (insertion-ordered) query string matching what we send on the wire.

    Lists are encoded as repeated `key[]=v` pairs. Values are str()'d — callers must pass
    Decimal amounts already formatted as strings to avoid float artefacts.
    """
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            k = key if key.endswith("[]") else f"{key}[]"
            parts.extend(f"{k}={v}" for v in value)
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def query_hash(query_string: str) -> str:
    return hashlib.sha512(query_string.encode("utf-8")).hexdigest()


def create_jwt(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> str:
    payload: dict[str, str] = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    if params:
        qs = build_query_string(params)
        if qs:
            payload["query_hash"] = query_hash(qs)
            payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, secret_key, algorithm="HS512")
    return token


def auth_header(
    access_key: str, secret_key: str, params: Mapping[str, Any] | None = None
) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_jwt(access_key, secret_key, params)}"}
