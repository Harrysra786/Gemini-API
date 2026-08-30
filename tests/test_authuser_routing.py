import unittest
from unittest.mock import AsyncMock, MagicMock

from gemini_webapi.utils.get_access_token import _send_request


class AuthuserRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_request_includes_account_index(self):
        client = MagicMock()
        client.cookies = MagicMock()
        response = MagicMock()
        client.get = AsyncMock(return_value=response)

        await _send_request(client, {"__Secure-1PSID": "test"}, authuser=4)

        self.assertEqual(client.get.await_args.kwargs["params"], {"authuser": "4"})


if __name__ == "__main__":
    unittest.main()
