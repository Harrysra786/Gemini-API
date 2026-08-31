"""Async client for the local standalone Gemini pool service."""

from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
from typing import Any


class GeminiPoolClient:
    def __init__(self, token_file: Path, host: str = "127.0.0.1", port: int = 8767):
        self.token_file = token_file
        self.host = host
        self.port = port

    async def submit_media(self, kind: str, prompt: str, **options: Any) -> dict[str, Any]:
        return await self._request("POST", "/v1/media", {"kind": kind, "prompt": prompt, **options})

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/jobs/{job_id}")

    async def accounts_status(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/accounts")

    async def begin_enrollment(self, account: str = "all") -> dict[str, Any]:
        return await self._request("POST", "/v1/enroll", {"account": account})

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.token_file.read_text(encoding="utf-8").strip()
        return await asyncio.to_thread(self._request_sync, method, path, payload, token)

    def _request_sync(self, method: str, path: str, payload: dict[str, Any] | None, token: str) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            parsed = json.loads(response.read())
        finally:
            connection.close()
        if response.status >= 400:
            raise RuntimeError(parsed.get("error", f"Gemini pool HTTP {response.status}"))
        return parsed
