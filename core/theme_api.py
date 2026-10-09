from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Optional

import aiohttp


@dataclass(frozen=True)
class ThemeAssetBytes:
    data: bytes
    content_type: str
    sha256: str


class ThemeAssetApiClient:
    """Authenticated bridge to API-owned R2 Theme assets.

    The runtime never accepts arbitrary storage keys or internet URLs. Asset
    identity is resolved by the API from the guild/application contract.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = 15.0,
    ):
        raw_base = base_url if base_url is not None else os.getenv("BOT_API_BASE_URL", "")
        self.base_url = str(raw_base or "").strip().rstrip("/")
        raw_token = token if token is not None else os.getenv("BOT_STATUS_API_TOKEN", "")
        self.token = str(raw_token or "").strip()
        self.timeout_seconds = max(float(timeout_seconds), 1.0)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def upload_rollback(
        self,
        guild_id: int,
        application_id: int,
        asset_type: str,
        data: bytes,
        content_type: str,
    ) -> dict:
        self._require_enabled()
        digest = hashlib.sha256(data).hexdigest()
        payload = {
            "assetType": str(asset_type).upper(),
            "contentType": content_type,
            "base64Data": base64.b64encode(data).decode("ascii"),
            "sha256": digest,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._url(
                    f"/internal/themes/{int(guild_id)}/applications/"
                    f"{int(application_id)}/rollback-assets"
                ),
                json=payload,
                headers=self._headers(),
            ) as response:
                body = await self._json_or_error(response)
                if response.status != 200:
                    raise RuntimeError(
                        f"theme_rollback_asset_upload_failed:{response.status}:"
                        f"{body.get('message') or body.get('error') or 'unknown'}"
                    )
                return body

    async def fetch_desired(
        self,
        guild_id: int,
        application_id: int,
        asset_type: str,
    ) -> ThemeAssetBytes:
        return await self._fetch(
            guild_id,
            application_id,
            "desired-assets",
            asset_type,
        )

    async def fetch_rollback(
        self,
        guild_id: int,
        application_id: int,
        asset_type: str,
    ) -> ThemeAssetBytes:
        return await self._fetch(
            guild_id,
            application_id,
            "rollback-assets",
            asset_type,
        )

    async def _fetch(
        self,
        guild_id: int,
        application_id: int,
        resource: str,
        asset_type: str,
    ) -> ThemeAssetBytes:
        self._require_enabled()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                self._url(
                    f"/internal/themes/{int(guild_id)}/applications/"
                    f"{int(application_id)}/{resource}/{str(asset_type).upper()}"
                ),
                headers=self._headers(),
            ) as response:
                if response.status != 200:
                    try:
                        body = await response.json()
                    except Exception:
                        body = {}
                    raise RuntimeError(
                        f"theme_asset_fetch_failed:{response.status}:"
                        f"{body.get('message') or body.get('error') or 'unknown'}"
                    )
                data = await response.read()
                content_type = (
                    response.headers.get("Content-Type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                )
                expected = response.headers.get("X-Content-SHA256", "").strip().lower()
                actual = hashlib.sha256(data).hexdigest()
                if not expected or expected != actual:
                    raise RuntimeError("theme_asset_hash_mismatch")
                return ThemeAssetBytes(
                    data=data,
                    content_type=content_type,
                    sha256=actual,
                )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _url(self, path: str) -> str:
        return self.base_url + path

    async def _json_or_error(self, response: aiohttp.ClientResponse) -> dict:
        try:
            value = await response.json()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("theme_asset_api_not_configured")
