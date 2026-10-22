from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services import proxy_service


class ProxyUrlNormalizationTest(unittest.TestCase):
    def test_compact_proxy_list_remains_unchanged_until_sample_test(self) -> None:
        normalized = proxy_service.normalize_proxy_url_list(
            "global.rotgb.711proxy.com:10000:user:password\n"
            "global.rotgb.711proxy.com:10000:user-2:password-2"
        )

        self.assertEqual(
            normalized.splitlines(),
            [
                "global.rotgb.711proxy.com:10000:user:password",
                "global.rotgb.711proxy.com:10000:user-2:password-2",
            ],
        )

    def test_compact_proxy_list_does_not_add_http(self) -> None:
        normalized = proxy_service.normalize_proxy_url_list(
            "proxy.example:8080:user:password"
        )

        self.assertEqual(
            normalized,
            "proxy.example:8080:user:password",
        )

    def test_explicit_proxy_scheme_is_not_rewritten(self) -> None:
        proxy = "http://user:password@proxy.example:8080"
        normalized = proxy_service.normalize_proxy_url_list(proxy)

        self.assertEqual(normalized, proxy)

    def test_compact_proxy_sample_uses_authenticated_working_scheme(self) -> None:
        def fake_test(url: str, *, timeout: float = 15.0) -> dict:
            ok = url.startswith("http://")
            return {
                "ok": ok,
                "status": 200 if ok else 0,
                "latency_ms": 12,
                "error": None if ok else "authentication failed",
            }

        with (
            patch.object(proxy_service.random, "choice", side_effect=lambda values: values[0]),
            patch.object(proxy_service, "test_proxy", side_effect=fake_test),
        ):
            result = proxy_service.test_proxy_list_sample(
                "proxy.example:8080:user:password\n"
                "proxy.example:8080:user-2:password-2"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["scheme"], "http")
        self.assertEqual(result["sample_index"], 1)
        self.assertEqual(result["sample_count"], 2)
        self.assertTrue(result["normalized_changed"])
        self.assertEqual(
            result["normalized_urls"].splitlines(),
            [
                "http://user:password@proxy.example:8080",
                "http://user-2:password-2@proxy.example:8080",
            ],
        )
        self.assertEqual({item["scheme"] for item in result["attempts"]}, {"http", "socks5h", "https"})

    def test_explicit_proxy_sample_does_not_try_other_schemes(self) -> None:
        with patch.object(
            proxy_service,
            "test_proxy",
            return_value={"ok": True, "status": 403, "latency_ms": 9, "error": None},
        ) as test:
            result = proxy_service.test_proxy_list_sample(
                "socks5h://user:password@proxy.example:1080"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["scheme"], "socks5h")
        self.assertFalse(result["normalized_changed"])
        test.assert_called_once_with(
            "socks5h://user:password@proxy.example:1080",
            timeout=10.0,
        )

    def test_failed_proxy_sample_does_not_return_normalized_credentials(self) -> None:
        with patch.object(
            proxy_service,
            "test_proxy",
            return_value={"ok": False, "status": 0, "latency_ms": 8, "error": "auth rejected"},
        ):
            result = proxy_service.test_proxy_list_sample(
                "proxy.example:8080:user:password"
            )

        self.assertFalse(result["ok"])
        self.assertNotIn("normalized_urls", result)
        self.assertIn("HTTP: auth rejected", result["error"])

    def test_proxy_sample_summary_classifies_common_protocol_errors(self) -> None:
        summary = proxy_service._proxy_attempt_summary(
            [
                {"scheme": "http", "error": "CONNECT tunnel failed, response 407"},
                {"scheme": "socks5h", "error": "User was rejected by the SOCKS5 server"},
                {"scheme": "https", "error": "TLS WRONG_VERSION_NUMBER"},
            ]
        )

        self.assertEqual(
            summary,
            "HTTP: 代理认证失败；SOCKS5H: 代理认证失败；HTTPS: TLS 协议不匹配",
        )
        self.assertEqual(
            proxy_service._proxy_test_error_label("Proxy CONNECT aborted"),
            "代理连接失败",
        )


class _ConfigStore:
    def __init__(self, proxy_url: str = "http://privoxy:8118") -> None:
        self.data: dict[str, object] = {}
        self.proxy_url = proxy_url

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return {
            "enabled": True,
            "egress_mode": "single_proxy",
            "proxy_url": self.proxy_url,
            "resource_proxy_url": "",
            "skip_ssl_verify": False,
            "reset_session_status_codes": [403],
            "clearance": {},
        }

    def get_proxy_settings(self) -> str:
        return ""


class RuntimeProxyEnvironmentTest(unittest.TestCase):
    def test_host_maps_compose_privoxy_to_published_port(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore())
        with (
            patch.object(proxy_service, "_is_container_environment", return_value=False),
            patch.dict(
                os.environ,
                {"CHATGPT2API_HOST_PRIVOXY_URL": "", "PRIVOXY_PORT": "40123"},
                clear=False,
            ),
        ):
            profile = store.get_profile(upstream=True)

        self.assertEqual(profile.proxy_url, "http://127.0.0.1:40123")
        self.assertEqual(profile.proxy_source, "runtime")
        self.assertEqual(profile.egress_key, "proxy:http://127.0.0.1:40123")

    def test_container_keeps_compose_privoxy_address(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore())
        with patch.object(proxy_service, "_is_container_environment", return_value=True):
            profile = store.get_profile(upstream=True)

        self.assertEqual(profile.proxy_url, "http://privoxy:8118")
        self.assertEqual(profile.egress_key, "proxy:http://privoxy:8118")

    def test_explicit_proxy_is_not_rewritten(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore())
        with patch.object(proxy_service, "_is_container_environment", return_value=False):
            profile = store.get_profile(proxy="http://privoxy:8118", upstream=True)

        self.assertEqual(profile.proxy_url, "http://privoxy:8118")
        self.assertEqual(profile.proxy_source, "explicit")

    def test_direct_does_not_fall_back_to_runtime(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore())
        with patch.object(proxy_service, "_is_container_environment", return_value=False):
            profile = store.get_profile(proxy="direct", upstream=True)

        self.assertEqual(profile.proxy_url, "")
        self.assertEqual(profile.proxy_source, "explicit_direct")



class _LocaleConfigStore(_ConfigStore):
    def __init__(self, runtime_locale: dict[str, str] | None = None) -> None:
        super().__init__()
        self.runtime_locale = runtime_locale or {}
        self.data = {
            "proxy_groups": [
                {
                    "id": "us",
                    "nodes": [
                        {
                            "id": "ny",
                            "url": "http://ny.example:8080",
                            "timezone": "America/New_York",
                            "language": "en-US",
                        }
                    ],
                },
                {"id": "plain", "nodes": [{"id": "n1", "url": "http://plain.example:8080"}]},
            ],
            "proxy_profiles": [
                {"id": "jp", "proxy": "http://jp.example:8080", "timezone": "Asia/Tokyo", "language": "ja-JP"},
            ],
        }

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return {**super().get_proxy_runtime_settings(), "enabled": False, **self.runtime_locale}


class ProxyLocaleTest(unittest.TestCase):
    def test_group_node_locale_is_attached_to_profile(self) -> None:
        store = proxy_service.ProxySettingsStore(_LocaleConfigStore())
        profile = store.get_profile(account={"proxy": "group:us"}, upstream=True)

        self.assertEqual(profile.proxy_url, "http://ny.example:8080")
        self.assertIsNotNone(profile.locale)
        self.assertEqual(profile.locale.timezone, "America/New_York")
        self.assertEqual(profile.locale.language, "en-US")

    def test_proxy_profile_locale_is_attached(self) -> None:
        store = proxy_service.ProxySettingsStore(_LocaleConfigStore())
        profile = store.get_profile(proxy="profile:jp", upstream=True)

        self.assertEqual(profile.locale.timezone, "Asia/Tokyo")

    def test_unconfigured_egress_falls_back_to_runtime_default(self) -> None:
        store = proxy_service.ProxySettingsStore(
            _LocaleConfigStore({"timezone": "Europe/Berlin", "language": "de-DE"})
        )
        plain = store.get_profile(account={"proxy": "group:plain"}, upstream=True)
        direct = store.get_profile(upstream=True)
        node = store.get_profile(account={"proxy": "group:us"}, upstream=True)

        self.assertEqual(plain.locale.timezone, "Europe/Berlin")
        self.assertEqual(direct.locale.timezone, "Europe/Berlin")
        self.assertEqual(node.locale.timezone, "America/New_York")

    def test_direct_locale_applies_to_direct_and_custom_proxy_not_group_nodes(self) -> None:
        class _Store(_LocaleConfigStore):
            def get_direct_locale(self) -> dict[str, str]:
                return {"timezone": "Asia/Tokyo", "language": "ja-JP"}

        store = proxy_service.ProxySettingsStore(_Store({"timezone": "Europe/Berlin", "language": "de-DE"}))
        direct = store.get_profile(upstream=True)
        custom = store.get_profile(proxy="http://custom.example:8080", upstream=True)
        node = store.get_profile(account={"proxy": "group:us"}, upstream=True)

        self.assertEqual(direct.locale.timezone, "Asia/Tokyo")
        self.assertEqual(custom.locale.language, "ja-JP")
        # 代理组节点自带的地区环境优先，不被直连地区环境覆盖。
        self.assertEqual(node.locale.timezone, "America/New_York")

    def test_empty_direct_locale_keeps_previous_behavior(self) -> None:
        class _Store(_LocaleConfigStore):
            def get_direct_locale(self) -> dict[str, str]:
                return {}

        store = proxy_service.ProxySettingsStore(_Store({"timezone": "Europe/Berlin", "language": "de-DE"}))
        self.assertEqual(store.get_profile(upstream=True).locale.timezone, "Europe/Berlin")

    def test_no_locale_configured_keeps_none(self) -> None:
        store = proxy_service.ProxySettingsStore(_LocaleConfigStore())
        self.assertIsNone(store.get_profile(upstream=True).locale)


class ProxyNodeImportTest(unittest.TestCase):
    def test_import_dedupes_and_reports_invalid_lines(self) -> None:
        result = proxy_service.import_proxy_nodes(
            "\n".join(
                [
                    "http://1.2.3.4:8080 50",
                    "",
                    "http://1.2.3.4:8080",
                    "socks5://u:p@5.6.7.8:1080",
                    "http://9.9.9.9:80",
                    "1.1.1.1:3128:user:pass",
                    "http://bad host:1",
                    "http://x:1 abc",
                    "ftp://x:1",
                    "http://a:1 2 3",
                ]
            ),
            ["http://9.9.9.9"],
        )
        urls = [node["url"] for node in result["nodes"]]
        self.assertEqual(
            urls,
            ["http://1.2.3.4:8080", "socks5h://u:p@5.6.7.8:1080", "http://user:pass@1.1.1.1:3128"],
        )
        self.assertEqual(result["nodes"][0]["image_concurrency_limit"], 50)
        self.assertEqual(result["nodes"][1]["image_concurrency_limit"], proxy_service.DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT)
        self.assertEqual(result["added_count"], 3)
        self.assertEqual(result["duplicate_count"], 2)
        self.assertEqual(result["invalid_count"], 4)
        self.assertEqual([item["line"] for item in result["invalid_items"]], [7, 8, 9, 10])

    def test_import_rejects_too_many_lines(self) -> None:
        with self.assertRaises(ValueError):
            proxy_service.import_proxy_nodes("\n".join(f"http://h{i}:1" for i in range(10_001)))


if __name__ == "__main__":
    unittest.main()
