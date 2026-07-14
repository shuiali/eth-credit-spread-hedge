"""Demo credential loading and environment-isolation tests."""

from pathlib import Path

import pytest

from eth_credit_hedge.config.bybit import (
    bybit_demo_profile_from_env,
    load_bybit_demo_profile,
)


def test_demo_profile_uses_only_demo_named_credentials_and_fixed_hosts() -> None:
    profile = bybit_demo_profile_from_env(
        {
            "BYBIT_API_KEY_DEMO": "demo-key",
            "BYBIT_API_SECRET_DEMO": "demo-secret",
            "BYBIT_API_KEY_MAIN": "main-key-must-not-be-used",
            "BYBIT_API_SECRET_MAIN": "main-secret-must-not-be-used",
            "BYBIT_REST_BASE_URL": "https://api.bybit.com",
        }
    )

    assert profile.rest_base_url == "https://api-demo.bybit.com"
    assert profile.private_websocket_url == (
        "wss://stream-demo.bybit.com/v5/private"
    )
    assert profile.credentials.api_key.get_secret_value() == "demo-key"
    assert "demo-key" not in repr(profile)
    assert "demo-secret" not in repr(profile)
    assert "main-key-must-not-be-used" not in repr(profile)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"BYBIT_API_KEY_DEMO": "demo-key"},
        {"BYBIT_API_SECRET_DEMO": "demo-secret"},
        {
            "BYBIT_API_KEY_DEMO": " ",
            "BYBIT_API_SECRET_DEMO": "demo-secret",
        },
    ],
)
def test_demo_profile_requires_both_nonempty_demo_values(
    values: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="BYBIT_API_.*_DEMO"):
        bybit_demo_profile_from_env(values)


def test_dotenv_loader_does_not_override_exported_demo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "BYBIT_API_KEY_DEMO=file-key\n"
        "BYBIT_API_SECRET_DEMO=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BYBIT_API_KEY_DEMO", "exported-key")
    monkeypatch.setenv("BYBIT_API_SECRET_DEMO", "exported-secret")

    profile = load_bybit_demo_profile(dotenv_path)

    assert profile.credentials.api_key.get_secret_value() == "exported-key"
    assert profile.credentials.api_secret.get_secret_value() == "exported-secret"
