import io
import json
import unittest
import urllib.error
from email.message import Message

from openusage_bar.network import (
    AuthenticationRequired,
    BoundedHTTPClient,
    MalformedResponse,
    NetworkError,
    RateLimited,
    ResponseTooLarge,
    UnsafeEndpoint,
    validate_endpoint,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Opener:
    def __init__(self, result):
        self.result = result
        self.last_request = None

    def open(self, request, timeout):
        self.last_request = request
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def http_error(code, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError("https://api.example.com", code, "error", headers, None)


class EndpointSafetyTests(unittest.TestCase):
    def test_post_json_uses_json_body_and_method(self):
        resolver = lambda _host: ["93.184.216.34"]
        opener = Opener(Response(b'{"status":1}'))
        client = BoundedHTTPClient(resolver, opener)

        payload = client.post_json(
            "https://api.example.com/quota",
            {"Oasis-Token": "test-only"},
            {"probe": True},
        )

        self.assertEqual(payload, {"status": 1})
        self.assertEqual(opener.last_request.method, "POST")
        self.assertEqual(json.loads(opener.last_request.data), {"probe": True})
    def test_rejects_non_https_endpoint(self):
        with self.assertRaises(UnsafeEndpoint):
            validate_endpoint("http://api.example.com/usage", lambda _host: ["93.184.216.34"])

    def test_rejects_loopback_and_link_local_resolution(self):
        for address in ("127.0.0.1", "::1", "169.254.169.254"):
            with self.subTest(address=address), self.assertRaises(UnsafeEndpoint):
                validate_endpoint("https://api.example.com/usage", lambda _host, value=address: [value])

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(UnsafeEndpoint):
            validate_endpoint("https://user:pass@example.com/usage", lambda _host: ["93.184.216.34"])

    def test_accepts_public_https_endpoint(self):
        self.assertEqual(
            validate_endpoint("https://api.example.com/usage", lambda _host: ["93.184.216.34"]),
            "https://api.example.com/usage",
        )

    def test_maps_authentication_and_rate_limit_statuses(self):
        resolver = lambda _host: ["93.184.216.34"]
        with self.assertRaises(AuthenticationRequired):
            BoundedHTTPClient(resolver, Opener(http_error(401))).get_json("https://api.example.com", {})
        with self.assertRaises(RateLimited):
            BoundedHTTPClient(resolver, Opener(http_error(429))).get_json("https://api.example.com", {})

    def test_rejects_oversized_and_malformed_json(self):
        resolver = lambda _host: ["93.184.216.34"]
        with self.assertRaises(ResponseTooLarge):
            BoundedHTTPClient(resolver, Opener(Response(b"12345")), max_bytes=4).get_json("https://api.example.com", {})
        with self.assertRaises(MalformedResponse):
            BoundedHTTPClient(resolver, Opener(Response(b"not-json"))).get_json("https://api.example.com", {})

    def test_revalidates_redirect_target(self):
        resolver = lambda host: [host] if host == "127.0.0.1" else ["93.184.216.34"]
        with self.assertRaises(UnsafeEndpoint):
            BoundedHTTPClient(resolver, Opener(http_error(302, "https://127.0.0.1/private"))).get_json(
                "https://api.example.com", {}
            )

    def test_reserved_proxy_ip_requires_exact_hostname_allowlist(self):
        resolver = lambda _host: ["198.18.0.79"]
        endpoint = "https://www.minimaxi.com/v1/token_plan/remains"

        with self.assertRaises(UnsafeEndpoint):
            BoundedHTTPClient(resolver).get_json(endpoint, {})

        client = BoundedHTTPClient(
            resolver,
            Opener(Response(b'{"base_resp":{"status_code":0}}')),
            allowed_reserved_hosts={"www.minimaxi.com"},
        )
        self.assertEqual(
            client.get_json(endpoint, {}),
            {"base_resp": {"status_code": 0}},
        )

    def test_redirect_host_allowlist_blocks_authorization_forwarding(self):
        resolver = lambda _host: ["93.184.216.34"]
        client = BoundedHTTPClient(
            resolver,
            Opener(http_error(302, "https://attacker.example/collect")),
            allowed_redirect_hosts={"q.us-east-1.amazonaws.com"},
        )

        with self.assertRaises(NetworkError):
            client.get_json(
                "https://q.us-east-1.amazonaws.com/getUsageLimits",
                {"Authorization": "Bearer test-only"},
            )


if __name__ == "__main__":
    unittest.main()
