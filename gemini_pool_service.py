"""Standalone, loopback-only Gemini media pool.

This process deliberately has no dependency on Codex or MCP worker lifetime.
It owns the Gemini clients, account locks, persisted media jobs, and media
downloads.  App-facing MCP adapters can come and go without losing this state.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

class PoolAccounts:
    """One Gemini client and one async lock per enrolled alias."""

    def __init__(
        self,
        credentials_root: Path,
        active_aliases: set[str] | None = None,
        client_factory: Any = None,
    ):
        self.credentials_root = credentials_root
        self.active_aliases = active_aliases
        self.client_factory = client_factory
        self.clients: dict[str, GeminiClient] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.models: dict[str, list[str]] = {}

    def aliases(self) -> list[str]:
        aliases = sorted(item.stem for item in self.credentials_root.glob("*.json"))
        if self.active_aliases is not None:
            aliases = [alias for alias in aliases if alias in self.active_aliases]
        return aliases

    def _lock(self, alias: str) -> asyncio.Lock:
        return self.locks.setdefault(alias, asyncio.Lock())

    def _credentials(self, alias: str) -> tuple[str, str, int]:
        payload = json.loads((self.credentials_root / f"{alias}.json").read_text(encoding="utf-8"))
        return payload["secure_1psid"], payload.get("secure_1psidts", ""), int(payload["authuser"])

    async def acquire(self, requested: str = "auto") -> tuple[str, GeminiClient]:
        aliases = [requested] if requested != "auto" else self.aliases()
        if not aliases:
            raise RuntimeError("No Gemini accounts are enabled for this pool.")
        for alias in sorted(aliases, key=lambda item: (self._lock(item).locked(), item)):
            lock = self._lock(alias)
            if lock.locked() and requested == "auto":
                continue
            await lock.acquire()
            try:
                if alias not in self.clients:
                    psid, psidts, authuser = self._credentials(alias)
                    if self.client_factory is None:
                        from gemini_webapi import GeminiClient

                        self.client_factory = GeminiClient
                    client = self.client_factory(psid, psidts, authuser=authuser)
                    await client.init(timeout=60, auto_close=False, auto_refresh=True)
                    self.clients[alias] = client
                models = self.clients[alias].list_models()
                self.models[alias] = [str(getattr(item, "name", item)) for item in (await models if inspect.isawaitable(models) else models)]
                return alias, self.clients[alias]
            except Exception:
                lock.release()
                if requested != "auto":
                    raise
        raise RuntimeError("No Gemini account is currently available.")

    def release(self, alias: str) -> None:
        lock = self._lock(alias)
        if lock.locked():
            lock.release()

    async def status(self) -> list[dict[str, Any]]:
        return [{"alias": alias, "ready": alias in self.clients, "busy": self._lock(alias).locked(), "models": self.models.get(alias, [])} for alias in self.aliases()]


class GeminiPool:
    def __init__(self, accounts: Any, state_root: Path):
        self.accounts = accounts
        self.state_root = state_root
        self.jobs_root = state_root / "jobs"
        self.outputs_root = state_root / "outputs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.outputs_root.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_root / f"{job_id}.json"

    def _store(self, job: dict[str, Any]) -> None:
        temporary = self._job_path(job["job_id"]).with_suffix(".tmp")
        temporary.write_text(json.dumps(job), encoding="utf-8")
        temporary.replace(self._job_path(job["job_id"]))

    def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._job_path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError("Unknown media job.") from exc

    async def submit_media(self, kind: str, prompt: str, *, files: list[str] | None = None, model: str | None = None, account: str = "auto") -> dict[str, Any]:
        if kind not in {"image", "video"}:
            raise ValueError("Media kind must be image or video.")
        job = {"job_id": uuid.uuid4().hex, "kind": kind, "prompt": prompt, "files": files or [], "model": model, "requested_account": account, "account": "pending", "status": "generating", "submitted_at": time.time(), "completed_at": None, "files_result": [], "text": "", "error": None}
        self._store(job)
        self.tasks[job["job_id"]] = asyncio.create_task(self._run_media(job["job_id"]))
        return self._public(job)

    async def wait_for_job(self, job_id: str) -> dict[str, Any]:
        task = self.tasks.get(job_id)
        if task:
            await task
        return self._public(self.get_job(job_id))

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": job["job_id"], "kind": job["kind"], "status": job["status"], "account": job["account"], "files": job["files_result"], "text": job["text"], "error": job["error"]}

    async def _run_media(self, job_id: str) -> None:
        job = self.get_job(job_id)
        alias: str | None = None
        try:
            alias, client = await self.accounts.acquire(job["requested_account"])
            job["account"] = alias
            self._store(job)
            response = await client.generate_content(job["prompt"], files=job["files"], model=job["model"])
            directory = self.outputs_root / alias / f"{datetime.now():%Y-%m-%d}" / f"{job['kind']}_{job_id}"
            directory.mkdir(parents=True, exist_ok=True)
            job["files_result"] = await self._save_response(job["kind"], response, directory, client.client)
            job["text"] = getattr(response, "text", "")
            job["status"] = "ready" if job["files_result"] else "no_media_returned"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if alias:
                self.accounts.release(alias)
            job["completed_at"] = time.time()
            self._store(job)

    async def _save_response(self, kind: str, response: Any, directory: Path, request_client: Any) -> list[dict[str, Any]]:
        if kind == "image":
            results = []
            for index, image in enumerate(getattr(response, "images", []) or []):
                path = await image.save(path=str(directory), filename=f"image_{index + 1}.png", client=request_client)
                results.append({"path": str(path), "title": getattr(image, "title", ""), "alt": getattr(image, "alt", "")})
            return results
        items = list(getattr(response, "videos", []) or [])
        items.extend(item for item in (getattr(response, "media", []) or []) if getattr(item, "url", None))
        results = []
        for index, video in enumerate(items):
            paths = await video.save(path=str(directory), filename=f"video_{index + 1}.mp4", client=request_client, poll_interval=2.0)
            results.append({"path": paths.get("video"), "thumbnail": paths.get("video_thumbnail"), "title": getattr(video, "title", "")})
        return results


class PoolHttpServer:
    """Minimal dependency-free HTTP API; it only accepts loopback clients."""

    def __init__(self, pool: GeminiPool, token: str):
        self.pool, self.token = pool, token

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("ascii").strip().split()
            headers: dict[str, str] = {}
            while line := await reader.readline():
                decoded = line.decode("ascii").strip()
                if not decoded:
                    break
                key, value = decoded.split(":", 1)
                headers[key.lower()] = value.strip()
            if headers.get("authorization") != f"Bearer {self.token}":
                await self.reply(writer, 401, {"error": "unauthorized"})
                return
            body = await reader.readexactly(int(headers.get("content-length", "0"))) if headers.get("content-length") else b""
            method, target = request_line[0], request_line[1]
            if method == "GET" and target == "/v1/accounts":
                await self.reply(writer, 200, {"accounts": await self.pool.accounts.status()})
            elif method == "GET" and target.startswith("/v1/jobs/"):
                await self.reply(writer, 200, self.pool._public(self.pool.get_job(target.rsplit("/", 1)[-1])))
            elif method == "POST" and target == "/v1/media":
                payload = json.loads(body or b"{}")
                job = await self.pool.submit_media(payload["kind"], payload["prompt"], files=payload.get("files"), model=payload.get("model"), account=payload.get("account", "auto"))
                await self.reply(writer, 202, job)
            else:
                await self.reply(writer, 404, {"error": "not found"})
        except Exception as exc:
            await self.reply(writer, 400, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def reply(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
        await writer.drain()


async def serve(credentials_root: Path, state_root: Path, token_file: Path, port: int) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    token = token_file.read_text(encoding="utf-8").strip()
    active = {item.strip() for item in os.environ.get("GEMINI_ACTIVE_ACCOUNTS", "").split(",") if item.strip()}
    pool = GeminiPool(PoolAccounts(credentials_root, active or None), state_root)
    server = await asyncio.start_server(PoolHttpServer(pool, token).handle, "127.0.0.1", port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("serve")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    asyncio.run(serve(args.credentials, args.state, args.token_file, args.port))


if __name__ == "__main__":
    main()
