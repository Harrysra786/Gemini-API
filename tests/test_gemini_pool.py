import asyncio
import tempfile
import unittest
from pathlib import Path

from gemini_pool_client import GeminiPoolClient
from gemini_pool_service import GeminiPool, PoolAccounts, PoolHttpServer


class _Image:
    title = "sample"
    alt = "sample alt"

    async def save(self, path, filename, **_kwargs):
        result = Path(path) / filename
        result.write_bytes(b"image")
        return str(result)


class _Response:
    text = "done"
    images = [_Image()]
    videos = []
    media = []


class _Client:
    client = object()

    async def generate_content(self, *_args, **_kwargs):
        await asyncio.sleep(0)
        return _Response()


class _Accounts:
    def __init__(self):
        self.released = []

    async def acquire(self, _account):
        return "gemini_02", _Client()

    def release(self, alias):
        self.released.append(alias)

    async def status(self):
        return [{"alias": "gemini_02", "ready": True}]


class _PoolClient:
    def __init__(self, *_args, **_kwargs):
        self.client = object()

    async def init(self, **_kwargs):
        return None

    async def list_models(self):
        return []


class GeminiPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_submission_returns_before_background_retrieval_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            accounts = _Accounts()
            pool = GeminiPool(accounts, Path(temp_dir))

            submitted = await pool.submit_media("image", "make a test image")
            self.assertEqual(submitted["status"], "generating")
            self.assertEqual(submitted["account"], "pending")

            completed = await pool.wait_for_job(submitted["job_id"])
            self.assertEqual(completed["status"], "ready")
            self.assertEqual(completed["account"], "gemini_02")
            self.assertEqual(len(completed["files"]), 1)
            self.assertTrue(Path(completed["files"][0]["path"]).is_file())
            self.assertEqual(accounts.released, ["gemini_02"])

    async def test_job_status_survives_a_new_pool_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pool = GeminiPool(_Accounts(), Path(temp_dir))
            job_id = (await pool.submit_media("image", "make a test image"))["job_id"]
            await pool.wait_for_job(job_id)

            restored = GeminiPool(_Accounts(), Path(temp_dir)).get_job(job_id)
            self.assertEqual(restored["status"], "ready")

    async def test_pool_accounts_assigns_distinct_idle_accounts_to_concurrent_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in (1, 2):
                (root / f"gemini_{index:02d}.json").write_text(
                    '{"secure_1psid":"test","secure_1psidts":"","authuser":0}', encoding="utf-8"
                )
            accounts = PoolAccounts(root, client_factory=_PoolClient)

            first_alias, _ = await accounts.acquire()
            second_alias, _ = await accounts.acquire()

            self.assertEqual((first_alias, second_alias), ("gemini_01", "gemini_02"))
            accounts.release(first_alias)
            accounts.release(second_alias)

    async def test_loopback_api_submits_and_reads_a_persisted_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pool = GeminiPool(_Accounts(), root / "state")
            token_file = root / "token"
            token_file.write_text("test-token", encoding="utf-8")
            server = await asyncio.start_server(PoolHttpServer(pool, "test-token").handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            client = GeminiPoolClient(token_file, port=port)
            try:
                job = await client.submit_media("image", "make a test image")
                await pool.wait_for_job(job["job_id"])
                result = await client.get_job(job["job_id"])
            finally:
                server.close()
                await server.wait_closed()

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["account"], "gemini_02")
