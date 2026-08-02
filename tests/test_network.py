import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import Mock, patch

from openusage_bar.network import (
    AuthenticationRequired,
    BoundedHTTPClient,
    HTTPStatusError,
    MalformedResponse,
    NetworkError,
    PinnedHTTPSOpener,
    RateLimited,
    ResponseTooLarge,
    UnsafeEndpoint,
    _PinnedHTTPSConnection,
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


class PinningOpener(Opener):
    def __init__(self, result):
        super().__init__(result)
        self.addresses = None

    def open_pinned(self, request, timeout, addresses):
        self.last_request = request
        self.addresses = addresses
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PinnedConnection:
    def __init__(self, response, connect_error=None):
        self.response = response
        self.connect_error = connect_error
        self.request_call = None
        self.closed = False

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        return None

    def request(self, method, target, body=None, headers=None):
        self.request_call = (method, target, body, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def http_error(code, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError("https://api.example.com", code, "error", headers, None)


class EndpointSafetyTests(unittest.TestCase):
    def test_pinned_connection_uses_numeric_tcp_peer_and_original_tls_hostname(self):
        plain = Mock()
        context = Mock()
        tls = Mock()
        context.wrap_socket.return_value = tls

        with patch("openusage_bar.network.socket.socket", return_value=plain) as create, patch(
            "openusage_bar.network.ssl.create_default_context", return_value=context
        ):
            connection = _PinnedHTTPSConnection(
                "api.example.com", 443, "93.184.216.34", 7.5
            )
            connection.connect()

        create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        plain.settimeout.assert_called_once_with(7.5)
        plain.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(
            plain, server_hostname="api.example.com"
        )
        self.assertIs(connection.sock, tls)

    def test_pinned_client_resolves_once_and_passes_only_validated_addresses(self):
        calls = []

        def rebinding_resolver(host):
            calls.append(host)
            return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

        opener = PinningOpener(Response(b'{"status":1}'))
        payload = BoundedHTTPClient(
            rebinding_resolver,
            opener,
            pin_resolved_address=True,
        ).post_json("https://api.example.com/v1/chat/completions", {}, {"probe": True})

        self.assertEqual(payload, {"status": 1})
        self.assertEqual(calls, ["api.example.com"])
        self.assertEqual(opener.addresses, ("93.184.216.34",))

    def test_pinned_opener_connects_to_numeric_address_but_keeps_hostname_and_path(self):
        response = Response(b'{"status":1}')
        response.status = 200
        response.reason = "OK"
        response.headers = Message()
        observed = []

        def factory(host, port, address, timeout):
            observed.append((host, port, address, timeout))
            return PinnedConnection(response)

        request = urllib.request.Request(
            "https://api.example.com:8443/v1/chat/completions?mode=test",
            data=b"{}",
            headers={
                "Authorization": "Bearer test-only",
                "Host": "attacker.example",
            },
            method="POST",
        )
        wrapped = PinnedHTTPSOpener(connection_factory=factory).open_pinned(
            request, 7.5, ("93.184.216.34",)
        )

        self.assertEqual(
            observed,
            [("api.example.com", 8443, "93.184.216.34", 7.5)],
        )
        self.assertEqual(wrapped.read(), b'{"status":1}')
        connection = wrapped.connection
        self.assertEqual(connection.request_call[0:2], (
            "POST", "/v1/chat/completions?mode=test"
        ))
        self.assertEqual(connection.request_call[2], b"{}")
        self.assertEqual(
            connection.request_call[3]["Authorization"], "Bearer test-only"
        )
        self.assertNotIn("Host", connection.request_call[3])
        wrapped.close()
        self.assertTrue(connection.closed)

    def test_pinned_opener_falls_back_only_before_sending_the_request(self):
        response = Response(b"{}")
        response.status = 200
        response.reason = "OK"
        response.headers = Message()
        connections = []

        def factory(_host, _port, address, _timeout):
            connection = PinnedConnection(
                response,
                connect_error=(OSError("unreachable") if address.endswith(".1") else None),
            )
            connections.append(connection)
            return connection

        request = urllib.request.Request(
            "https://api.example.com/v1/chat/completions",
            data=b"{}",
            method="POST",
        )
        wrapped = PinnedHTTPSOpener(connection_factory=factory).open_pinned(
            request, 5.0, ("93.184.216.1", "93.184.216.34")
        )

        self.assertIsNone(connections[0].request_call)
        self.assertIsNotNone(connections[1].request_call)
        wrapped.close()

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

    def test_post_stream_is_bounded_and_keeps_the_response_out_of_json_parsing(self):
        resolver = lambda _host: ["93.184.216.34"]
        wire = b'data: {"id":"one"}\n\ndata: [DONE]\n\n'
        opener = Opener(Response(wire))
        client = BoundedHTTPClient(resolver, opener, max_bytes=len(wire))

        stream = client.post_stream(
            "https://api.example.com/v1/chat/completions",
            {"Authorization": "Bearer test-only"},
            {"model": "gpt-5", "stream": True},
        )

        self.assertEqual(b"".join(stream), wire)
        self.assertEqual(opener.last_request.method, "POST")
        self.assertEqual(json.loads(opener.last_request.data)["model"], "gpt-5")

        oversized = BoundedHTTPClient(
            resolver, Opener(Response(b"12345")), max_bytes=4
        ).post_stream(
            "https://api.example.com/v1/chat/completions", {}, {"stream": True}
        )
        with self.assertRaises(ResponseTooLarge):
            b"".join(oversized)

    def test_preserves_non_auth_http_status_for_execution_retry_policy(self):
        resolver = lambda _host: ["93.184.216.34"]
        for code in (400, 408, 500, 503):
            with self.subTest(code=code), self.assertRaises(HTTPStatusError) as raised:
                BoundedHTTPClient(resolver, Opener(http_error(code))).post_json(
                    "https://api.example.com", {}, {"probe": True}
                )
            self.assertEqual(raised.exception.status, code)
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
