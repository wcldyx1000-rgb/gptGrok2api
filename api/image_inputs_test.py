import socket
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import image_inputs
from utils import remote_image


def _addrinfo(*addresses: str):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=(b"png-bytes",)):
        self.status_code = status_code
        self.headers = headers or {"content-type": "image/png"}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


class _FakeSession:
    """按 URL 返回预设响应，并记录每次请求用到的 curl_options。"""

    responses: dict = {}
    calls: list = []

    def __init__(self, curl_options=None, **kwargs):
        self.curl_options = curl_options or {}

    def get(self, url, **kwargs):
        _FakeSession.calls.append((url, self.curl_options, kwargs))
        return _FakeSession.responses[url]

    def close(self):
        pass


class DownloadImageUrlSsrfTests(unittest.TestCase):
    def setUp(self):
        _FakeSession.responses = {}
        _FakeSession.calls = []
        session_patch = patch.object(remote_image, "Session", _FakeSession)
        proxy_patch = patch.object(image_inputs.proxy_settings, "build_session_kwargs", return_value={})
        session_patch.start()
        proxy_patch.start()
        self.addCleanup(session_patch.stop)
        self.addCleanup(proxy_patch.stop)

    def assert_rejected(self, url: str, message: str):
        with self.assertRaises(HTTPException) as ctx:
            image_inputs._download_image_url(url)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(message, ctx.exception.detail["error"])

    def test_rejects_private_loopback_and_metadata_literals(self):
        for url in (
            "http://127.0.0.1/a.png",
            "http://10.0.0.5/a.png",
            "http://192.168.1.1/a.png",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/a.png",
            "http://localhost/a.png",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "not publicly reachable")
        self.assertEqual(_FakeSession.calls, [])

    def test_rejects_hostname_resolving_to_private_address(self):
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34", "10.1.2.3")):
            self.assert_rejected("https://evil.example/a.png", "not publicly reachable")
        self.assertEqual(_FakeSession.calls, [])

    def test_fake_ip_allowed_only_for_resolved_hostnames(self):
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("198.18.0.47")):
            parsed, addresses = remote_image.validate_public_image_url("https://cdn.example/a.png")
        self.assertEqual(addresses, ("198.18.0.47",))
        self.assert_rejected("http://198.18.0.47/a.png", "not publicly reachable")

    def test_rejects_credentials_and_non_http_schemes(self):
        self.assert_rejected("https://user:pass@example.com/a.png", "must not include credentials")
        self.assert_rejected("file:///etc/passwd", "http or https")

    def test_rejects_redirect_to_internal_address(self):
        _FakeSession.responses["https://cdn.example/a.png"] = _FakeResponse(
            302, {"location": "http://169.254.169.254/latest/meta-data"}
        )
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assert_rejected("https://cdn.example/a.png", "not publicly reachable")
        self.assertEqual(len(_FakeSession.calls), 1)
        self.assertFalse(_FakeSession.calls[0][2]["allow_redirects"])

    def test_rejects_too_many_redirects(self):
        for index in range(remote_image.MAX_IMAGE_REDIRECTS + 1):
            _FakeSession.responses[f"https://cdn.example/{index}"] = _FakeResponse(
                302, {"location": f"/{index + 1}"}
            )
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assert_rejected("https://cdn.example/0", "too many redirects")

    def test_rejects_oversized_body_without_content_length(self):
        chunk = b"x" * (1024 * 1024)
        chunks = [chunk] * (image_inputs.MAX_IMAGE_REFERENCE_BYTES // len(chunk) + 1)
        _FakeSession.responses["https://cdn.example/big.png"] = _FakeResponse(200, {"content-type": "image/png"}, chunks)
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assert_rejected("https://cdn.example/big.png", "exceeds 50MB limit")

    def test_rejects_oversized_content_length(self):
        _FakeSession.responses["https://cdn.example/big.png"] = _FakeResponse(
            200, {"content-type": "image/png", "content-length": str(image_inputs.MAX_IMAGE_REFERENCE_BYTES + 1)}
        )
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assert_rejected("https://cdn.example/big.png", "exceeds 50MB limit")

    def test_public_url_downloads_and_pins_resolved_address(self):
        _FakeSession.responses["https://cdn.example/start"] = _FakeResponse(302, {"location": "/images/cat.jpg"})
        _FakeSession.responses["https://cdn.example/images/cat.jpg"] = _FakeResponse(
            200, {"content-type": "image/jpeg"}, (b"abc", b"def")
        )
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            data, filename, mime_type = image_inputs._download_image_url("https://cdn.example/start")
        self.assertEqual(data, b"abcdef")
        self.assertEqual(filename, "cat.jpg")
        self.assertEqual(mime_type, "image/jpeg")
        resolve_options = [call[1].get(remote_image.CurlOpt.RESOLVE) for call in _FakeSession.calls]
        self.assertEqual(resolve_options, [["cdn.example:443:93.184.216.34"]] * 2)

    def test_data_url_skips_network(self):
        data, _filename, mime_type = image_inputs._download_image_url("data:image/png;base64,aGVsbG8=")
        self.assertEqual((data, mime_type), (b"hello", "image/png"))
        self.assertEqual(_FakeSession.calls, [])


class MessageImageUrlSsrfTests(unittest.TestCase):
    def test_chat_message_image_url_rejects_internal_address(self):
        from utils import helper

        with self.assertRaises(HTTPException) as ctx:
            helper._decode_message_image_url({"url": "http://169.254.169.254/latest/meta-data"})
        self.assertIn("not publicly reachable", ctx.exception.detail["error"])


class GrokAssetUploadSsrfTests(unittest.IsolatedAsyncioTestCase):
    async def test_third_party_url_is_validated_and_sent_without_sso_headers(self):
        from app.dataplane.reverse.transport import asset_upload
        from app.platform.errors import ValidationError

        class _AsyncSession:
            def __init__(self):
                self.calls = []

            async def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return _FakeResponse(302, {"location": "http://10.0.0.1/secret"})

        session = _AsyncSession()
        sso_headers = {"Cookie": "sso=secret", "User-Agent": "UA"}
        with patch.object(remote_image.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(ValidationError):
                await asset_upload._fetch_input_url(session, "https://cdn.example/a.png", sso_headers)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("Cookie", session.calls[0][1]["headers"])
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    async def test_first_party_url_keeps_sso_headers(self):
        from app.dataplane.reverse.transport import asset_upload

        self.assertTrue(asset_upload._is_first_party_url("https://assets.grok.com/users/x/img.png"))
        self.assertFalse(asset_upload._is_first_party_url("https://grok.com.evil.example/a.png"))


if __name__ == "__main__":
    unittest.main()
