from __future__ import annotations

from app.core.logging import redact


def test_redacts_secret_keys_and_jwt_values() -> None:
    out = redact(
        {
            "UPBIT_SECRET_KEY": "abc",
            "Authorization": "Bearer x",
            "nested": {"access_key": "k", "price": 1},
            "msg": "token eyJhbGciOiJIUzUxMiJ9.eyJhY2Nlc3Nfa2V5IjoiQUsifQ.c2lnbmF0dXJlc2lnbmF0dXJl ok",
            "list": [{"secret": 1}],
        }
    )
    assert out["UPBIT_SECRET_KEY"] == "***REDACTED***"
    assert out["Authorization"] == "***REDACTED***"
    assert out["nested"]["access_key"] == "***REDACTED***" and out["nested"]["price"] == 1
    assert "eyJ" not in out["msg"] and "***JWT***" in out["msg"]
    assert out["list"][0]["secret"] == "***REDACTED***"
