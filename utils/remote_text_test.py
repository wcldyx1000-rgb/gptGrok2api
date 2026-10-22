import socket
import unittest
from unittest.mock import patch

from utils import remote_image
from utils.remote_image import RemoteFetchError, fetch_public_text


def _addrinfo(*addresses: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]


class _Resp:
    def __init__(self, status_code=200, headers=None, chunks=(b"{}",)):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = list(chunks)

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        pass


class _Session:
    responses: dict = {}
    calls: list = []

    def __init__(self, curl_options=None, **kwargs):
        pass

    def get(self, url, **kwargs):
        _Session.calls.append(url)
        return _Session.responses[url]

    def close(self):
        pass


def _fetch(url, **kw):
    return fetch_public_text(url, max_bytes=kw.pop("max_bytes", 1024), timeout=3, user_agent="t", **kw)


class FetchPublicTextTest(unittest.TestCase):
    def setUp(self):
        _Session.responses = {}
        _Session.calls = []
        patcher = patch.object(remote_image, "Session", _Session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_private_loopback_metadata_and_bad_scheme(self):
        for url in (
            "http://127.0.0.1/x",
            "http://10.0.0.5/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/x",
            "http://localhost/x",
            "file:///etc/passwd",
            "ftp://example.com/x",
        ):
            with self.subTest(url=url), self.assertRaises(RemoteFetchError):
                _fetch(url)
        self.assertEqual(_Session.calls, [])

    def test_rejects_domain_resolving_to_private_address(self):
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("10.1.2.3")):
            with self.assertRaises(RemoteFetchError):
                _fetch("http://internal.example.com/x")

    def test_rejects_redirect_to_private_address(self):
        _Session.responses["http://pub.example.com/a"] = _Resp(302, {"location": "http://169.254.169.254/"})
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(RemoteFetchError):
                _fetch("http://pub.example.com/a")
        self.assertEqual(_Session.calls, ["http://pub.example.com/a"])

    def test_rejects_too_many_redirects(self):
        _Session.responses["http://pub.example.com/a"] = _Resp(302, {"location": "http://pub.example.com/a"})
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(RemoteFetchError):
                _fetch("http://pub.example.com/a", max_redirects=2)
        self.assertEqual(len(_Session.calls), 3)

    def test_rejects_oversize_streaming_and_content_length(self):
        _Session.responses["http://pub.example.com/big"] = _Resp(chunks=(b"x" * 600, b"x" * 600, b"x" * 600))
        _Session.responses["http://pub.example.com/cl"] = _Resp(headers={"content-length": "999999"})
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            for url in ("http://pub.example.com/big", "http://pub.example.com/cl"):
                with self.subTest(url=url), self.assertRaises(RemoteFetchError):
                    _fetch(url)

    def test_returns_body_for_public_source(self):
        _Session.responses["https://pub.example.com/p.json"] = _Resp(chunks=(b'{"a":', b"1}"))
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            result = _fetch("https://pub.example.com/p.json")
        self.assertEqual(result.data, b'{"a":1}')
        self.assertEqual(result.content_type, "application/json")


if __name__ == "__main__":
    unittest.main()
