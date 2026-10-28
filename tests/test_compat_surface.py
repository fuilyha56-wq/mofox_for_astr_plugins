"""AstrBot 4.29.3 import surface and strict message compatibility tests."""

from __future__ import annotations

import importlib

import pytest

from mofox_for_astr_plugins.errors import UnsupportedMessageSegmentError
from mofox_for_astr_plugins.sdk import (
    ASTRBOT_COMPAT_RELEASE,
    ASTRBOT_COMPAT_RELEASE_COMMIT,
    ASTRBOT_COMPAT_UPSTREAM_HEAD,
    ASTRBOT_COMPAT_VERSION,
    install,
)
from mofox_for_astr_plugins.sdk.filter import EventMessageType, PlatformAdapterType
from mofox_for_astr_plugins.sdk.message_components import AtAll, Plain, chain_to_onebot_segments


@pytest.fixture(autouse=True)
def installed_compat_sdk() -> None:
    """Install the bridge shim for each surface test."""
    install()


def test_release_snapshot_is_pinned() -> None:
    assert ASTRBOT_COMPAT_VERSION == "4.29.3"
    assert ASTRBOT_COMPAT_RELEASE == "v4.29.3"
    assert len(ASTRBOT_COMPAT_RELEASE_COMMIT) == 40
    assert len(ASTRBOT_COMPAT_UPSTREAM_HEAD) == 40


@pytest.mark.parametrize(
    "module_name",
    [
        "astrbot.api",
        "astrbot.api.all",
        "astrbot.api.event",
        "astrbot.api.event.filter",
        "astrbot.api.message_components",
        "astrbot.api.platform",
        "astrbot.api.provider",
        "astrbot.api.star",
        "astrbot.api.util",
        "astrbot.api.web",
        "astrbot.core.platform.astrbot_message",
        "astrbot.core.platform.platform",
        "astrbot.core.provider.entities",
        "astrbot.core.star.filter.event_message_type",
        "astrbot.core.star.filter.platform_adapter_type",
    ],
)
def test_public_import_path_is_available(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_updated_enums_match_astrbot_4293() -> None:
    assert EventMessageType.ALL & EventMessageType.GROUP_MESSAGE
    assert PlatformAdapterType.AIOCQHTTP
    assert PlatformAdapterType.WEBCHAT


def test_at_all_preserves_onebot_segment() -> None:
    assert chain_to_onebot_segments([AtAll(), Plain("hi")]) == [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": "hi"}},
    ]


def test_unsupported_component_does_not_degrade_to_text() -> None:
    from mofox_for_astr_plugins.sdk.message_components import Poke

    with pytest.raises(UnsupportedMessageSegmentError):
        chain_to_onebot_segments([Poke()])
