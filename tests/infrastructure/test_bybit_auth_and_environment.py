"""Private Bybit authentication and demo-profile safety tests."""

from dataclasses import FrozenInstanceError

import pytest

from eth_credit_hedge.infrastructure.bybit.auth import (
    ApiCredentials,
    BybitV5Signer,
    SecretStr,
)
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile


def credentials() -> ApiCredentials:
    return ApiCredentials(
        api_key=SecretStr("demo-key"),
        api_secret=SecretStr("demo-secret"),
    )


def test_secret_values_and_credential_repr_are_redacted() -> None:
    secret = SecretStr("never-print-me")
    api_credentials = ApiCredentials(
        api_key=SecretStr("key-never-print-me"),
        api_secret=secret,
    )

    assert secret.get_secret_value() == "never-print-me"
    assert "never-print-me" not in str(secret)
    assert "never-print-me" not in repr(secret)
    assert "key-never-print-me" not in repr(api_credentials)
    assert "never-print-me" not in repr(api_credentials)
    with pytest.raises(FrozenInstanceError):
        setattr(secret, "_value", "replacement")


def test_demo_profile_seals_credentials_to_exact_demo_hosts() -> None:
    profile = BybitDemoProfile(credentials())

    assert profile.rest_base_url == "https://api-demo.bybit.com"
    assert profile.private_websocket_url == (
        "wss://stream-demo.bybit.com/v5/private"
    )
    assert "demo-key" not in repr(profile)
    assert "demo-secret" not in repr(profile)
    with pytest.raises(FrozenInstanceError):
        profile.credentials = credentials()  # type: ignore[misc]


def test_get_signature_uses_exact_query_string_and_redacts_headers() -> None:
    signer = BybitV5Signer(credentials(), receive_window_ms=5000)

    signed = signer.sign_get(
        timestamp_ms=1658384314791,
        query_string="category=linear&symbol=ETHUSDT",
    )

    assert signed.as_http_headers() == {
        "X-BAPI-API-KEY": "demo-key",
        "X-BAPI-TIMESTAMP": "1658384314791",
        "X-BAPI-SIGN": (
            "6218060011e0e26babfe2ff0076cd4abc"
            "5be7c0f93ea3fc44c769555738b9ab5"
        ),
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json",
    }
    assert signed.redacted() == {
        "X-BAPI-API-KEY": "<redacted>",
        "X-BAPI-TIMESTAMP": "1658384314791",
        "X-BAPI-SIGN": "<redacted>",
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json",
    }
    assert "demo-key" not in repr(signed)
    assert "621806" not in repr(signed)


def test_post_signature_uses_body_bytes_without_json_reencoding() -> None:
    signer = BybitV5Signer(credentials(), receive_window_ms=5000)
    compact_body = '{"category":"linear","symbol":"ETHUSDT"}'
    spaced_body = '{"category": "linear", "symbol": "ETHUSDT"}'

    compact = signer.sign_post(
        timestamp_ms=1658385579423,
        body=compact_body,
    )
    spaced = signer.sign_post(
        timestamp_ms=1658385579423,
        body=spaced_body,
    )

    assert compact.as_http_headers()["X-BAPI-SIGN"] == (
        "61d0ec395b38993cb425aaaebef3953fa"
        "7b84d864f3f271ce1d64a070e0175e4"
    )
    assert spaced.as_http_headers()["X-BAPI-SIGN"] != (
        compact.as_http_headers()["X-BAPI-SIGN"]
    )


def test_private_websocket_auth_uses_get_realtime_signature() -> None:
    signer = BybitV5Signer(credentials())

    auth = signer.sign_websocket_auth(expires_at_ms=1658386000000)

    assert auth.as_auth_args() == (
        "demo-key",
        1658386000000,
        "733225aa8e16ae37fb747cc065e42615b192fc8875980ce589329ab563f1f2b4",
    )
    assert "demo-key" not in repr(auth)
    assert "733225" not in repr(auth)


@pytest.mark.parametrize("receive_window_ms", [0, -1])
def test_receive_window_must_be_positive(receive_window_ms: int) -> None:
    with pytest.raises(ValueError, match="receive window"):
        BybitV5Signer(credentials(), receive_window_ms=receive_window_ms)


def test_timestamp_must_be_positive() -> None:
    signer = BybitV5Signer(credentials())

    with pytest.raises(ValueError, match="timestamp"):
        signer.sign_get(timestamp_ms=0, query_string="")
