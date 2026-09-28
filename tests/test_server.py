import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import AppHandler


class StaticServerSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_public_asset_has_security_headers(self):
        with urlopen(f"{self.base_url}/app.js", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_private_files_are_not_served(self):
        for path in ("/.env", "/server.py", "/.git/config", "/data/playball.db"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{self.base_url}{path}", timeout=2)
                self.assertEqual(error.exception.code, 404)

    def test_head_cannot_probe_private_files(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(f"{self.base_url}/.env", method="HEAD"), timeout=2)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
