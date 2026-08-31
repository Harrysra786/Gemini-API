import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gemini_pool_client import GeminiPoolClient


class PoolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_submits_media_to_the_loopback_service(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = await reader.read(1024)
            self.assertIn(b'"kind": "image"', body)
            response = b'{"job_id":"job-1","status":"generating"}'
            writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\nContent-Length: " + str(len(response)).encode() + b"\r\n\r\n" + response)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "token"
            token_file.write_text("test-token", encoding="utf-8")
            result = await GeminiPoolClient(token_file, port=port).submit_media("image", "test")
        server.close()
        await server.wait_closed()
        self.assertEqual(result["job_id"], "job-1")
