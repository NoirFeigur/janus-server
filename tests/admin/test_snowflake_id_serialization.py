"""Snowflake-ID serialization regressions for admin read schemas (M3-10).

Snowflake IDs are 64-bit integers that exceed JS ``Number.MAX_SAFE_INTEGER``
(2^53 - 1). Serialized as JSON numbers they silently round on the frontend, so
every ID field on a read schema must serialize to a STRING. These tests pin that
contract for the rate-limit and observability read models (the catalog read
models already carried serializers). Optional ID fields must stay ``None`` (never
the string ``"None"``).
"""

from __future__ import annotations

from src.admin.observability.schemas import GatewayLogRead
from src.admin.rate_limits.schemas import RateLimitRuleRead

# A value larger than 2^53 so a numeric round-trip would visibly corrupt it.
_BIG_ID = 7299160320000000001


def test_rate_limit_rule_read_serializes_all_snowflake_ids_as_strings() -> None:
    dumped = RateLimitRuleRead(
        id=_BIG_ID,
        subject_type="user",
        subject_id=_BIG_ID + 1,
        logical_model_id=_BIG_ID + 2,
        rpm_limit=60,
    ).model_dump(mode="json")

    assert dumped["id"] == str(_BIG_ID)
    assert dumped["subject_id"] == str(_BIG_ID + 1)
    assert dumped["logical_model_id"] == str(_BIG_ID + 2)


def test_rate_limit_rule_read_optional_ids_stay_none() -> None:
    dumped = RateLimitRuleRead(
        id=_BIG_ID, subject_type="global", subject_id=None, logical_model_id=None
    ).model_dump(mode="json")

    assert dumped["id"] == str(_BIG_ID)
    assert dumped["subject_id"] is None  # never the string "None"
    assert dumped["logical_model_id"] is None


def test_gateway_log_read_serializes_all_snowflake_ids_as_strings() -> None:
    dumped = GatewayLogRead(
        id=_BIG_ID,
        request_id="req-1",
        user_id=_BIG_ID + 1,
        api_key_id=_BIG_ID + 2,
        logical_model_id=_BIG_ID + 3,
        channel_id=_BIG_ID + 4,
    ).model_dump(mode="json")

    assert dumped["id"] == str(_BIG_ID)
    assert dumped["user_id"] == str(_BIG_ID + 1)
    assert dumped["api_key_id"] == str(_BIG_ID + 2)
    assert dumped["logical_model_id"] == str(_BIG_ID + 3)
    assert dumped["channel_id"] == str(_BIG_ID + 4)


def test_gateway_log_read_optional_ids_stay_none() -> None:
    dumped = GatewayLogRead(id=_BIG_ID, request_id="req-1").model_dump(mode="json")

    assert dumped["id"] == str(_BIG_ID)
    assert dumped["user_id"] is None
    assert dumped["api_key_id"] is None
    assert dumped["logical_model_id"] is None
    assert dumped["channel_id"] is None
