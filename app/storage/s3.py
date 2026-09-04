"""Files in S3, or anything that speaks its API.

Covers Amazon S3, MinIO, Cloudflare R2, DigitalOcean Spaces, Backblaze B2 and
the rest, because they all implement the same protocol -- the only difference
is an endpoint URL.

``aioboto3`` is imported lazily. A deployment on local disk should not have to
install an AWS SDK, and a missing optional dependency should produce a sentence
explaining what to install rather than an ImportError at startup.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from app.core.errors import ConfigError
from app.storage.base import (
    CHUNK_SIZE,
    BaseFileStore,
    FileNotFound,
    StorageError,
    UploadPolicy,
    register_store,
)

#: Uploads below this go in one request; above it, a multipart upload.
MULTIPART_THRESHOLD = 8 * 1024 * 1024


def _require_aioboto3():
    try:
        import aioboto3
    except ImportError as exc:
        raise ConfigError(
            "The S3 file store needs the 'aioboto3' package. "
            "Install it with:  uv add aioboto3"
        ) from exc
    return aioboto3


class S3FileStore(BaseFileStore):
    """Stores files in an S3-compatible bucket."""

    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        region: str = "",
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        prefix: str = "",
        policy: UploadPolicy | None = None,
        base_url: str = "",
        #: Serve files through signed URLs straight from the bucket rather than
        #: proxying the bytes through this application.
        signed_urls: bool = True,
        acl: str = "",
        session_token: str = "",
    ) -> None:
        super().__init__(policy=policy, base_url=base_url)
        if not bucket:
            raise ConfigError("the S3 file store needs a bucket name")
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url or None
        self.access_key = access_key or None
        self.secret_key = secret_key or None
        self.session_token = session_token or None
        self.prefix = prefix.strip("/")
        self.signed_urls = signed_urls
        self.acl = acl
        self._session: Any = None

    # -- plumbing -----------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            self._session = _require_aioboto3().Session()
        return self._session

    def _client(self):
        """A client context manager.

        Credentials are only passed when configured; omitting them lets the SDK
        find them the usual ways -- instance role, environment, shared config --
        which is how a deployment avoids putting keys in a file at all.
        """
        kwargs: dict[str, Any] = {"service_name": "s3"}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
            if self.session_token:
                kwargs["aws_session_token"] = self.session_token
        return self.session.client(**kwargs)

    def _object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    # -- the backend --------------------------------------------------------

    async def _write(self, key: str, data: AsyncIterator[bytes]) -> dict[str, Any]:
        object_key = self._object_key(key)
        extra: dict[str, Any] = {"ACL": self.acl} if self.acl else {}

        # Buffer up to the multipart threshold. Most CRM attachments are well
        # under it, and a single PutObject is one request rather than three.
        buffer = bytearray()
        chunks: list[bytes] = []
        async for chunk in data:
            buffer.extend(chunk)
            if len(buffer) > MULTIPART_THRESHOLD:
                chunks.append(bytes(buffer))
                buffer = bytearray()
                break
        else:
            async with self._client() as client:
                try:
                    await client.put_object(
                        Bucket=self.bucket, Key=object_key, Body=bytes(buffer), **extra
                    )
                except Exception as exc:
                    raise StorageError(f"could not store {key!r} in S3: {exc}") from exc
            return {"bucket": self.bucket, "key": object_key}

        # Larger than the threshold: upload in parts so the whole file is never
        # held in memory at once.
        async with self._client() as client:
            try:
                created = await client.create_multipart_upload(
                    Bucket=self.bucket, Key=object_key, **extra
                )
                upload_id = created["UploadId"]
                parts: list[dict[str, Any]] = []
                number = 1
                pending = bytearray(b"".join(chunks))

                async def flush(final: bool = False) -> None:
                    nonlocal pending, number
                    while len(pending) >= MULTIPART_THRESHOLD or (final and pending):
                        piece = bytes(pending[:MULTIPART_THRESHOLD])
                        pending = pending[MULTIPART_THRESHOLD:]
                        response = await client.upload_part(
                            Bucket=self.bucket, Key=object_key,
                            PartNumber=number, UploadId=upload_id, Body=piece,
                        )
                        parts.append({"ETag": response["ETag"], "PartNumber": number})
                        number += 1

                await flush()
                async for chunk in data:
                    pending.extend(chunk)
                    await flush()
                await flush(final=True)

                await client.complete_multipart_upload(
                    Bucket=self.bucket, Key=object_key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            except Exception as exc:
                # Abandoned parts are billed until they are cleaned up, so an
                # aborted upload is tidied even on the failure path. The tidy-up
                # itself failing must not mask the original error.
                with contextlib.suppress(Exception):
                    await client.abort_multipart_upload(
                        Bucket=self.bucket, Key=object_key, UploadId=upload_id
                    )
                raise StorageError(f"could not store {key!r} in S3: {exc}") from exc

        return {"bucket": self.bucket, "key": object_key, "multipart": True}

    def _read(self, key: str) -> AsyncIterator[bytes]:
        return self._stream(key)

    async def _stream(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as client:
            try:
                response = await client.get_object(
                    Bucket=self.bucket, Key=self._object_key(key)
                )
            except Exception as exc:
                if "NoSuchKey" in str(exc) or "404" in str(exc):
                    raise FileNotFound(f"no stored file at {key!r}") from exc
                raise StorageError(f"could not read {key!r} from S3: {exc}") from exc

            stream = response["Body"]
            while True:
                chunk = await stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    async def _remove(self, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.delete_object(Bucket=self.bucket, Key=self._object_key(key))
            except Exception as exc:
                raise StorageError(f"could not delete {key!r} from S3: {exc}") from exc
        return True

    async def _exists(self, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            except Exception:
                return False
        return True

    async def url(self, key: str, *, expires: int = 3600) -> str:
        """A time-limited link straight to the bucket.

        Serving the bytes from S3 rather than through this process is the whole
        reason to use it, so signed URLs are the default. A deployment that
        needs every download to pass its own permission checks sets
        ``signed_urls: false`` and gets the proxying route instead.
        """
        if not self.signed_urls:
            return await super().url(key, expires=expires)
        async with self._client() as client:
            try:
                return await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": self._object_key(key)},
                    ExpiresIn=expires,
                )
            except Exception as exc:
                raise StorageError(f"could not sign a URL for {key!r}: {exc}") from exc

    async def health(self) -> tuple[bool, str]:
        try:
            async with self._client() as client:
                await client.head_bucket(Bucket=self.bucket)
        except ConfigError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        where = self.endpoint_url or f"s3 ({self.region or 'default region'})"
        return True, f"bucket {self.bucket!r} reachable at {where}"


@register_store("s3")
def build_s3(**options: Any) -> S3FileStore:
    bucket = options.pop("bucket", "")
    from app.storage.local import _policy_from

    return S3FileStore(
        bucket,
        region=options.pop("region", ""),
        endpoint_url=options.pop("endpoint_url", ""),
        access_key=options.pop("access_key", ""),
        secret_key=options.pop("secret_key", ""),
        session_token=options.pop("session_token", ""),
        prefix=options.pop("prefix", ""),
        signed_urls=bool(options.pop("signed_urls", True)),
        acl=options.pop("acl", ""),
        base_url=options.pop("base_url", ""),
        policy=_policy_from(options.pop("limits", None)),
    )
