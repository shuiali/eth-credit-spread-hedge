"""Bybit demo credentials loaded without permitting endpoint overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

from eth_credit_hedge.infrastructure.bybit.auth import ApiCredentials, SecretStr
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile


DEMO_API_KEY_ENV = "BYBIT_API_KEY_DEMO"
DEMO_API_SECRET_ENV = "BYBIT_API_SECRET_DEMO"


def bybit_demo_profile_from_env(values: Mapping[str, str]) -> BybitDemoProfile:
    """Bind only explicitly demo-named credentials to sealed demo endpoints."""
    api_key = values.get(DEMO_API_KEY_ENV, "").strip()
    api_secret = values.get(DEMO_API_SECRET_ENV, "").strip()
    if not api_key or not api_secret:
        raise ValueError(
            "BYBIT_API_KEY_DEMO and BYBIT_API_SECRET_DEMO are required"
        )
    return BybitDemoProfile(
        ApiCredentials(
            api_key=SecretStr(api_key),
            api_secret=SecretStr(api_secret),
        )
    )


def load_bybit_demo_profile(
    dotenv_path: str | Path = ".env",
) -> BybitDemoProfile:
    """Load a local dotenv without overriding exported environment values."""
    load_dotenv(dotenv_path=dotenv_path, override=False)
    api_key = os.environ.get(DEMO_API_KEY_ENV, "").strip()
    api_secret = os.environ.get(DEMO_API_SECRET_ENV, "").strip()
    if not api_key or not api_secret:
        raise ValueError(
            "BYBIT_API_KEY_DEMO and BYBIT_API_SECRET_DEMO are required"
        )
    return BybitDemoProfile(
        ApiCredentials(
            api_key=SecretStr(api_key),
            api_secret=SecretStr(api_secret),
        )
    )
