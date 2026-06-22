"""Tests for the object-storage (MinIO/S3) abstraction.

The real S3 client is never contacted — a fake aioboto3 session records calls and
returns canned values, so these tests prove our *call contract* (which bucket /
key / params we pass, how presign/upload/delete are wired) without network.
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from src.core.oss import ObjectStorage


class _FakeEvents:
    """Records ``before-send`` hook registrations (no real dispatch)."""

    def __init__(self, recorder: dict[str, Any]) -> None:
        self._rec = recorder

    def register(self, event_name: str, handler: Any) -> None:
        self._rec.setdefault("events", []).append(event_name)


class _FakeMeta:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self.events = _FakeEvents(recorder)


class _FakeS3Client:
    """Records the S3 operations the ObjectStorage issues."""

    def __init__(self, recorder: dict[str, Any]) -> None:
        self._rec = recorder
        self.meta = _FakeMeta(recorder)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._rec["put_object"] = kwargs
        return {"ETag": "fake-etag"}

    async def generate_presigned_url(
        self, operation: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self._rec["presign"] = {
            "operation": operation,
            "Params": Params,
            "ExpiresIn": ExpiresIn,
        }
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._rec["delete_object"] = kwargs
        return {}


class _FakeSession:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._rec = recorder

    def client(self, service: str, **kwargs: Any) -> _FakeS3Client:
        self._rec["client_kwargs"] = {"service": service, **kwargs}
        return _FakeS3Client(self._rec)


def _make_storage(recorder: dict[str, Any]) -> ObjectStorage:
    storage = ObjectStorage(
        endpoint_url="http://minio.test:9000",
        region="us-east-1",
        bucket="private",
        access_key="AK",
        secret_key="SK",
        presign_ttl_seconds=900,
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
    )
    storage._session = lambda: _FakeSession(recorder)  # type: ignore[method-assign]
    return storage


async def test_upload_passes_bucket_key_body_and_content_type() -> None:
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)

    await storage.upload(
        object_key="avatar/2026/06/1.webp",
        data=b"webp-bytes",
        content_type="image/webp",
    )

    assert rec["put_object"] == {
        "Bucket": "private",
        "Key": "avatar/2026/06/1.webp",
        "Body": b"webp-bytes",
        "ContentType": "image/webp",
    }


async def test_client_built_with_configured_endpoint_and_credentials() -> None:
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)

    await storage.upload(object_key="k", data=b"x", content_type="image/webp")

    kwargs = rec["client_kwargs"]
    assert kwargs["service"] == "s3"
    assert kwargs["endpoint_url"] == "http://minio.test:9000"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["aws_access_key_id"] == "AK"
    assert kwargs["aws_secret_access_key"] == "SK"


async def test_presign_get_uses_get_object_bucket_key_and_ttl() -> None:
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)

    url = await storage.presign_get("avatar/2026/06/1.webp")

    assert rec["presign"] == {
        "operation": "get_object",
        "Params": {"Bucket": "private", "Key": "avatar/2026/06/1.webp"},
        "ExpiresIn": 900,
    }
    assert url.startswith("https://signed.example/avatar/2026/06/1.webp")


async def test_delete_passes_bucket_and_key() -> None:
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)

    await storage.delete("avatar/2026/06/1.webp")

    assert rec["delete_object"] == {
        "Bucket": "private",
        "Key": "avatar/2026/06/1.webp",
    }


async def test_presign_get_with_force_download() -> None:
    """force_download=True bakes Content-Disposition: attachment into the signed params."""
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)

    url = await storage.presign_get("attachment/2026/06/doc.pdf", force_download=True)

    assert rec["presign"]["Params"]["ResponseContentDisposition"] == "attachment"
    assert url.startswith("https://signed.example/attachment/2026/06/doc.pdf")


def test_bucket_property_exposes_configured_bucket() -> None:
    rec: dict[str, Any] = {}
    storage = _make_storage(rec)
    assert storage.bucket == "private"


def test_get_object_storage_optional_returns_none_when_no_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional dep returns None instead of raising when OSS is unconfigured."""

    from src.config import get_settings
    from src.core.oss import get_object_storage_optional

    settings = get_settings()
    monkeypatch.setattr(settings, "oss_access_key", None)
    monkeypatch.setattr(settings, "oss_secret_key", None)
    assert get_object_storage_optional() is None


def test_get_object_storage_optional_returns_instance_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional dep returns ObjectStorage when credentials are present."""
    from pydantic import SecretStr

    from src.config import get_settings
    from src.core.oss import get_object_storage_optional

    settings = get_settings()
    monkeypatch.setattr(settings, "oss_access_key", SecretStr("AK"))
    monkeypatch.setattr(settings, "oss_secret_key", SecretStr("SK"))
    result = get_object_storage_optional()
    assert result is not None
    assert result.bucket == settings.oss_bucket


def test_missing_credentials_raise_at_construction() -> None:
    with pytest.raises(ValueError, match="credentials"):
        ObjectStorage(
            endpoint_url="http://minio.test:9000",
            region="us-east-1",
            bucket="private",
            access_key=None,
            secret_key=None,
            presign_ttl_seconds=900,
            connect_timeout_seconds=5.0,
            read_timeout_seconds=10.0,
        )


def test_config_carries_network_timeouts_and_disables_retries() -> None:
    """The botocore config must cap connect/read time and disable retry amplification.

    A hung storage backend on the request hot path must fail at the timeout rather
    than pin a worker for the aioboto3 default (60s each), and botocore's silent
    retries must not multiply that wait. Asserts the wrapper threads the configured
    timeouts through and pins ``max_attempts`` to 1.
    """
    storage = ObjectStorage(
        endpoint_url="http://minio.test:9000",
        region="us-east-1",
        bucket="private",
        access_key="AK",
        secret_key="SK",
        presign_ttl_seconds=900,
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
    )
    config = storage._config()
    assert config.connect_timeout == 5.0
    assert config.read_timeout == 10.0
    assert config.retries == {"max_attempts": 1}
