"""Cloudflare R2 storage adapter (S3-compatible).

Config keys:
    account_id   (required) Cloudflare account ID
    bucket       (required) R2 bucket name
    endpoint     (optional) Custom endpoint URL. Default: https://<account_id>.r2.cloudflarestorage.com

Credential (vault):
    'ACCESS_KEY_ID:SECRET_ACCESS_KEY' colon-separated (R2 API token pair).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from xiosync.subsystems.storage.adapters.base import StorageAdapter, AccessInfo


class CloudflareR2Adapter(StorageAdapter):

    def validate_config(self) -> None:
        for key in ("account_id", "bucket"):
            if key not in self.config:
                raise ValueError(f"cloudflare_r2 provider requires config.{key}")

    def _endpoint(self) -> str:
        return self.config.get(
            "endpoint",
            f"https://{self.config['account_id']}.r2.cloudflarestorage.com"
        )

    def _presign(self, object_key: str, method: str, expires: int = 3600) -> str:
        """Generate a presigned S3-compatible URL for R2.

        Uses AWS Signature Version 4 (which R2 accepts).
        """
        if not self.credential:
            raise ValueError("R2 adapter requires credential: 'ACCESS_KEY_ID:SECRET_ACCESS_KEY'")

        try:
            access_key, secret_key = self.credential.split(":", 1)
        except ValueError:
            raise ValueError("R2 credential must be 'ACCESS_KEY_ID:SECRET_ACCESS_KEY'")

        bucket = self.config["bucket"]
        region = "auto"
        service = "s3"
        endpoint = self._endpoint()

        now = datetime.now(UTC)
        date_str  = now.strftime("%Y%m%d")
        time_str  = now.strftime("%Y%m%dT%H%M%SZ")

        credential_scope = f"{date_str}/{region}/{service}/aws4_request"
        credential_str   = f"{access_key}/{credential_scope}"

        host = endpoint.replace("https://", "").replace("http://", "")
        canonical_uri = f"/{bucket}/{quote(object_key, safe='/')}"

        query = (
            f"X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential={quote(credential_str, safe='')}"
            f"&X-Amz-Date={time_str}"
            f"&X-Amz-Expires={expires}"
            f"&X-Amz-SignedHeaders=host"
        )
        canonical_request = (
            f"{method}\n{canonical_uri}\n{query}\n"
            f"host:{host}\n\nhost\nUNSIGNED-PAYLOAD"
        )
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{time_str}\n{credential_scope}\n"
            + hashlib.sha256(canonical_request.encode()).hexdigest()
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        signing_key = _sign(
            _sign(_sign(_sign(f"AWS4{secret_key}".encode(), date_str), region), service),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return f"{endpoint}/{bucket}/{object_key}?{query}&X-Amz-Signature={signature}"

    def get_access_info(self, object_key: str, operation: str) -> AccessInfo:
        method_map = {"upload": "PUT", "download": "GET", "delete": "DELETE"}
        http_method = method_map.get(operation, "GET")
        url = self._presign(object_key, http_method)
        return AccessInfo(
            provider_type="cloudflare_r2",
            operation=operation,
            url=url,
            method=http_method,
            metadata={"bucket": self.config["bucket"], "object_key": object_key},
        )

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """Stub — actual listing requires S3 ListObjectsV2 from worker."""
        return []
