from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import openusage_bar.gateway.cache as gateway_cache
from openusage_bar.gateway.cache import (
    CacheCandidate,
    CacheKeySet,
    ExactHit,
    Miss,
    PrefixHint,
    SQLiteGatewayCache,
)
from openusage_bar.gateway.pii import Redactor
from openusage_bar.gateway.streaming import StreamAggregator, StreamResult


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **changes: int) -> None:
        self.current += timedelta(**changes)


class _FakeRedaction:
    cacheable = True
    redacted_text = "caller-claimed-safe"


def _openai_stream(
    text: str,
    *,
    tool_use: bool = False,
    interrupted: bool = False,
) -> StreamResult:
    aggregator = StreamAggregator("openai", "text/event-stream")
    if tool_use:
        aggregator.feed(
            b"event: response.output_item.added\n"
            b'data: {"type":"response.output_item.added",'
            b'"item":{"type":"function_call","name":"lookup"}}\n\n'
        )
    if text:
        payload = json.dumps(
            {"type": "response.output_text.delta", "delta": text},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        aggregator.feed(
            b"event: response.output_text.delta\ndata: " + payload + b"\n\n"
        )
    aggregator.feed(
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":'
        b'{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    )
    return aggregator.finish(interrupted=interrupted)


def _keys(
    request_text: str,
    *,
    provider_id: str = "openai",
    model: str = "gpt-4.1-mini",
    prefixes: tuple[object, ...] | None = None,
    parameters: dict[str, object] | None = None,
):
    request = Redactor().redact(request_text)
    prefix_results = prefixes if prefixes is not None else (request,)
    keys = CacheKeySet.from_request(
        provider_id=provider_id,
        model=model,
        parameters=parameters or {"temperature": 0, "top_p": 1},
        request=request,
        prefixes=prefix_results,
        stream=True,
    )
    return request, keys


def _candidate(
    request_text: str,
    response_text: str,
    *,
    provider_id: str = "openai",
    model: str = "gpt-4.1-mini",
    prefixes: tuple[object, ...] | None = None,
    parameters: dict[str, object] | None = None,
    provider_metadata: dict[str, str] | None = None,
) -> tuple[object, CacheKeySet, CacheCandidate]:
    request, keys = _keys(
        request_text,
        provider_id=provider_id,
        model=model,
        prefixes=prefixes,
        parameters=parameters,
    )
    response = request.redact_response(response_text)
    completion = _openai_stream(response_text)
    candidate = CacheCandidate.create(
        keys=keys,
        request=request,
        response=response,
        completion=completion,
        provider_metadata=provider_metadata or {"cache_control": "ephemeral"},
    )
    if candidate is None:
        raise AssertionError("safe test candidate was rejected")
    return request, keys, candidate


class SQLiteGatewayCacheSecurityTests(unittest.TestCase):
    def test_windows_parent_acl_failure_prevents_sqlite_open(self):
        class RejectingSecurity:
            def harden_directory(self, _path):
                raise OSError("synthetic ACL failure")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            gateway_cache,
            "_WINDOWS_FILE_SECURITY",
            RejectingSecurity(),
        ), patch.object(gateway_cache.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(
                RuntimeError,
                "^cache database initialization failed$",
            ):
                SQLiteGatewayCache(
                    Path(directory) / "gateway-cache.sqlite3"
                )
        connect.assert_not_called()

    def test_constructor_requires_exact_cache_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in (
                "activity.sqlite3",
                "activity.sqlite3-wal",
                "activity.sqlite3-shm",
                "cache.sqlite3",
                "gateway-telemetry.sqlite3",
            ):
                with self.subTest(filename=filename), self.assertRaisesRegex(
                    ValueError, "^invalid cache database path$"
                ):
                    SQLiteGatewayCache(root / filename)

    def test_constructor_rejects_any_hardlinked_database_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "collector"
            gateway_dir = root / "gateway"
            source_dir.mkdir()
            gateway_dir.mkdir()
            activity = source_dir / "activity.sqlite3"
            activity.write_bytes(b"")
            gateway_path = gateway_dir / "gateway-cache.sqlite3"
            os.link(activity, gateway_path)

            with self.assertRaisesRegex(ValueError, "^invalid cache database path$"):
                SQLiteGatewayCache(gateway_path)

            self.assertEqual(activity.read_bytes(), b"")

    def test_constructor_rejects_unknown_or_malformed_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            with closing(sqlite3.connect(path)) as database:
                database.execute("CREATE TABLE unrelated (raw_prompt TEXT)")
                database.commit()

            with self.assertRaisesRegex(
                ValueError, "^cache database schema is incompatible$"
            ):
                SQLiteGatewayCache(path)

    def test_disabled_cache_never_creates_database_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            _, keys = _keys("public request")

            cache = SQLiteGatewayCache(path, enabled=False)
            try:
                self.assertIsInstance(cache.lookup(keys), Miss)
                self.assertEqual(cache.stats().entries, 0)
                cache.clear()
            finally:
                cache.close()

            self.assertFalse(path.exists())
            self.assertFalse(Path(f"{path}-wal").exists())
            self.assertFalse(Path(f"{path}-shm").exists())

    def test_cache_has_no_boolean_redaction_or_completeness_bypass(self) -> None:
        request, keys = _keys("public request")
        response = request.redact_response("public response")
        completion = _openai_stream("public response")

        with self.assertRaises((TypeError, ValueError)):
            CacheKeySet.from_request(
                provider_id="openai",
                model="gpt-4.1-mini",
                parameters={},
                request=_FakeRedaction(),
                prefixes=(),
                stream=True,
            )
        with self.assertRaises(TypeError):
            CacheCandidate.create(
                keys=keys,
                request=request,
                response=response,
                completion=completion,
                redacted=True,
                complete=True,
                has_tool_use=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3"
            )
            try:
                self.assertFalse(hasattr(cache, "put"))
                self.assertFalse(hasattr(cache, "get"))
            finally:
                cache.close()

    def test_candidate_requires_matching_sealed_request_response_and_completion(self) -> None:
        request, keys = _keys("email jane@example.com")
        completion = _openai_stream("sent jane@example.com")
        matching = request.redact_response("sent jane@example.com")
        unseen = request.redact_response("sent other@example.net")
        foreign_request = Redactor().redact("email other@example.net")
        foreign_response = foreign_request.redact_response(
            "sent other@example.net"
        )

        accepted = CacheCandidate.create(
            keys=keys,
            request=request,
            response=matching,
            completion=completion,
            provider_metadata={},
        )
        unseen_candidate = CacheCandidate.create(
            keys=keys,
            request=request,
            response=unseen,
            completion=completion,
            provider_metadata={},
        )
        foreign_candidate = CacheCandidate.create(
            keys=keys,
            request=request,
            response=foreign_response,
            completion=completion,
            provider_metadata={},
        )

        self.assertIsInstance(accepted, CacheCandidate)
        self.assertIsNone(unseen_candidate)
        self.assertIsNone(foreign_candidate)

    def test_tool_use_interruption_and_secret_requests_cannot_form_candidates(self) -> None:
        request, keys = _keys("public request")
        response = request.redact_response("public response")
        tool_result = _openai_stream("public response", tool_use=True)
        interrupted_result = _openai_stream("public response", interrupted=True)
        secret_request, secret_keys = _keys(
            "use sk-proj-0123456789abcdefghijklmnopqrstuvwxyz"
        )
        secret_response = secret_request.redact_response("not used")

        for label, key_set, request_result, response_result, completion in (
            ("tool", keys, request, response, tool_result),
            ("interrupted", keys, request, response, interrupted_result),
            (
                "secret",
                secret_keys,
                secret_request,
                secret_response,
                _openai_stream("not used"),
            ),
        ):
            with self.subTest(case=label):
                self.assertIsNone(
                    CacheCandidate.create(
                        keys=key_set,
                        request=request_result,
                        response=response_result,
                        completion=completion,
                        provider_metadata={},
                    )
                )


class SQLiteGatewayCacheBehaviorTests(unittest.TestCase):
    def test_windows_security_seam_hardens_parent_then_database_handle(self):
        calls = []

        class Security:
            def harden_directory(self, path):
                calls.append(("directory", path))

            def harden_file(self, descriptor):
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise AssertionError("expected regular cache database")
                calls.append(("file", descriptor))

        with tempfile.TemporaryDirectory() as directory, patch.object(
            gateway_cache,
            "_WINDOWS_FILE_SECURITY",
            Security(),
        ):
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3"
            )
            cache.close()

        self.assertEqual(
            [kind for kind, _value in calls[:2]],
            ["directory", "file"],
        )

    @unittest.skipUnless(os.name == "nt", "Windows-native diagnostic contract")
    def test_windows_private_wal_cache_initializes_without_hidden_platform_error(self):
        failures: list[tuple[str, BaseException]] = []
        original_prepare = gateway_cache._prepare_private_database

        def recording_prepare(path):
            try:
                return original_prepare(path)
            except BaseException as error:
                failures.append(("prepare", error))
                raise

        class RecordingCache(SQLiteGatewayCache):
            def _initialize_database(self):
                try:
                    return super()._initialize_database()
                except BaseException as error:
                    failures.append(("initialize", error))
                    raise

        with tempfile.TemporaryDirectory() as directory, patch.object(
            gateway_cache,
            "_prepare_private_database",
            recording_prepare,
        ):
            try:
                cache = RecordingCache(
                    Path(directory) / "gateway-cache.sqlite3"
                )
            except RuntimeError:
                self.assertTrue(failures)
                stage, error = failures[0]
                code = getattr(error, "winerror", None)
                if code is None:
                    code = getattr(error, "sqlite_errorcode", None)
                self.fail(
                    "Windows cache initialization failed "
                    f"stage={stage} type={type(error).__name__} code={code}"
                )
            else:
                cache.close()

    def test_hot_exact_hit_reuses_the_already_validated_safe_response(self) -> None:
        _, keys, candidate = _candidate(
            "hot validation request",
            "safe response " + "o" * 4096,
        )

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=16 * 1024,
            )
            try:
                self.assertTrue(cache.store(candidate))
                self.assertIsInstance(cache.lookup(keys), ExactHit)
                with patch(
                    "openusage_bar.gateway.cache._safe_stored_response",
                    wraps=gateway_cache._safe_stored_response,
                ) as validate_response:
                    repeated = cache.lookup(keys)

                self.assertIsInstance(repeated, ExactHit)
                validate_response.assert_not_called()
            finally:
                cache.close()

    @unittest.skipIf(os.name == "nt", "POSIX mode drift contract")
    def test_hot_exact_hit_fails_closed_on_database_permission_drift(self) -> None:
        _, keys, candidate = _candidate("hot permission request", "safe response")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            cache = SQLiteGatewayCache(path, size_cap_bytes=4096)
            try:
                self.assertTrue(cache.store(candidate))
                self.assertIsInstance(cache.lookup(keys), ExactHit)
                path.chmod(0o644)

                with self.assertRaisesRegex(
                    ValueError, "^invalid cache database path$"
                ):
                    cache.lookup(keys)
            finally:
                path.chmod(0o600)
                cache.close()

    @unittest.skipIf(os.name == "nt", "open SQLite files cannot be replaced on Windows")
    def test_persistent_connection_rejects_database_path_replacement(self) -> None:
        _, keys, candidate = _candidate("hot request", "safe response")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            replacement = Path(directory) / "replacement.sqlite3"
            cache = SQLiteGatewayCache(path, size_cap_bytes=4096)
            self.assertTrue(cache.store(candidate))
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o600)
            os.replace(replacement, path)

            with self.assertRaisesRegex(
                ValueError, "^invalid cache database path$"
            ):
                cache.lookup(keys)
            with self.assertRaisesRegex(
                ValueError, "^invalid cache database path$"
            ):
                cache.close()

    def test_hot_lookups_reuse_the_hardened_cache_connection(self) -> None:
        _, keys, candidate = _candidate("hot request", "safe response")

        with tempfile.TemporaryDirectory() as directory, patch(
            "openusage_bar.gateway.cache.sqlite3.connect",
            wraps=sqlite3.connect,
        ) as connect:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=4096,
            )
            try:
                self.assertTrue(cache.store(candidate))
                connect.reset_mock()

                self.assertIsInstance(cache.lookup(keys), ExactHit)
                self.assertIsInstance(cache.lookup(keys), ExactHit)

                self.assertEqual(connect.call_count, 0)
            finally:
                cache.close()

    def test_cache_keys_are_deterministic_and_isolated_by_pii_context(self) -> None:
        first_request = Redactor().redact("email jane@example.com")
        repeated_request = Redactor().redact("email jane@example.com")
        second_request = Redactor().redact("email john@example.net")

        first = CacheKeySet.from_request(
            provider_id="openai",
            model="gpt-4.1-mini",
            parameters={"top_p": 1, "temperature": 0},
            request=first_request,
            prefixes=(first_request,),
            stream=True,
        )
        repeated = CacheKeySet.from_request(
            provider_id="openai",
            model="gpt-4.1-mini",
            parameters={"temperature": 0, "top_p": 1},
            request=repeated_request,
            prefixes=(repeated_request,),
            stream=True,
        )
        second = CacheKeySet.from_request(
            provider_id="openai",
            model="gpt-4.1-mini",
            parameters={"temperature": 0, "top_p": 1},
            request=second_request,
            prefixes=(second_request,),
            stream=True,
        )
        legacy_raw_hash = hashlib.sha256(
            first_request.redacted_text.encode("utf-8")
        ).hexdigest()

        self.assertEqual(first.exact_sha256, repeated.exact_sha256)
        self.assertEqual(first.prefix_sha256, repeated.prefix_sha256)
        self.assertNotEqual(first.exact_sha256, second.exact_sha256)
        self.assertNotEqual(first.prefix_sha256, second.prefix_sha256)
        self.assertRegex(first.exact_sha256, "^[0-9a-f]{64}$")
        self.assertNotEqual(first.exact_sha256, legacy_raw_hash)
        self.assertNotEqual(
            first.exact_sha256,
            CacheKeySet.from_request(
                provider_id="anthropic",
                model="gpt-4.1-mini",
                parameters={"temperature": 0, "top_p": 1},
                request=first_request,
                prefixes=(first_request,),
                stream=True,
            ).exact_sha256,
        )

    def test_exact_hit_is_reusable_only_for_the_same_pii_context(self) -> None:
        first_request, keys, candidate = _candidate(
            "email jane@example.com",
            "sent jane@example.com",
        )
        repeated_request, repeated_keys = _keys("email jane@example.com")
        _, different_keys = _keys("email john@example.net")

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=4096,
            )
            try:
                self.assertTrue(cache.store(candidate))
                repeated_hit = cache.lookup(repeated_keys)
                different_hit = cache.lookup(different_keys)
            finally:
                cache.close()

        self.assertEqual(keys.exact_sha256, repeated_keys.exact_sha256)
        self.assertNotEqual(keys.exact_sha256, different_keys.exact_sha256)
        self.assertIsInstance(repeated_hit, ExactHit)
        self.assertIsInstance(different_hit, Miss)
        self.assertNotIn("jane@example.com", repeated_hit.redacted_response)
        self.assertEqual(
            first_request.rehydrate(repeated_hit.redacted_response),
            "sent jane@example.com",
        )
        self.assertEqual(
            repeated_request.rehydrate(repeated_hit.redacted_response),
            "sent jane@example.com",
        )

    def test_request_derived_response_never_hits_a_different_pii_context(self) -> None:
        _, stored_keys, candidate = _candidate(
            "Email jane@example.com",
            "Hello jane",
        )
        _, repeated_keys = _keys("Email jane@example.com")
        _, different_keys = _keys("Email john@example.net")

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=4096,
            )
            try:
                self.assertTrue(cache.store(candidate))
                repeated_hit = cache.lookup(repeated_keys)
                different_hit = cache.lookup(different_keys)
            finally:
                cache.close()

        self.assertEqual(stored_keys.exact_sha256, repeated_keys.exact_sha256)
        self.assertNotEqual(stored_keys.exact_sha256, different_keys.exact_sha256)
        self.assertIsInstance(repeated_hit, ExactHit)
        self.assertEqual(repeated_hit.redacted_response, "Hello jane")
        self.assertIsInstance(different_hit, Miss)

    def test_l2_match_is_a_hint_without_any_previous_response(self) -> None:
        shared_prefix = Redactor().redact("system: public\nuser: hello")
        _, stored_keys, candidate = _candidate(
            "system: public\nuser: hello\nassistant: welcome",
            "old response must not be replayed",
            prefixes=(shared_prefix,),
            provider_metadata={"cache_control": "ephemeral"},
        )
        _, extended_keys = _keys(
            "system: public\nuser: hello\nassistant: welcome\nuser: next",
            prefixes=(shared_prefix, Redactor().redact("second boundary")),
        )
        self.assertNotEqual(stored_keys.exact_sha256, extended_keys.exact_sha256)

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=4096,
            )
            try:
                self.assertTrue(cache.store(candidate))
                hint = cache.lookup(extended_keys)
            finally:
                cache.close()

        self.assertIsInstance(hint, PrefixHint)
        self.assertRegex(hint.prefix_sha256, "^[0-9a-f]{64}$")
        self.assertEqual(hint.depth, 1)
        self.assertEqual(dict(hint.provider_metadata), {"cache_control": "ephemeral"})
        self.assertFalse(hasattr(hint, "value"))
        self.assertFalse(hasattr(hint, "response"))
        self.assertFalse(hasattr(hint, "redacted_response"))
        self.assertNotIn("old response", repr(hint))

    def test_default_ttl_is_one_hour_and_expired_entry_is_removed(self) -> None:
        clock = _Clock(datetime(2026, 8, 8, 12, tzinfo=timezone.utc))
        _, keys, candidate = _candidate("ttl request", "safe")

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=4096,
                clock=clock,
            )
            try:
                self.assertTrue(cache.store(candidate))
                clock.advance(seconds=3599)
                self.assertIsInstance(cache.lookup(keys), ExactHit)
                clock.advance(seconds=2)
                self.assertIsInstance(cache.lookup(keys), Miss)
                self.assertEqual(cache.stats().entries, 0)
            finally:
                cache.close()

    def test_size_cap_evicts_least_recently_used_entry(self) -> None:
        clock = _Clock(datetime(2026, 8, 8, 12, tzinfo=timezone.utc))
        entries = [
            _candidate(f"request {index}", value)
            for index, value in enumerate(
                ("a" * 4096, "b" * 4096, "c" * 4096), start=1
            )
        ]

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3",
                size_cap_bytes=9000,
                clock=clock,
            )
            try:
                self.assertTrue(cache.store(entries[0][2]))
                clock.advance(seconds=1)
                self.assertTrue(cache.store(entries[1][2]))
                clock.advance(seconds=1)
                self.assertIsInstance(cache.lookup(entries[0][1]), ExactHit)
                clock.advance(seconds=1)
                self.assertTrue(cache.store(entries[2][2]))

                self.assertIsInstance(cache.lookup(entries[0][1]), ExactHit)
                self.assertIsInstance(cache.lookup(entries[1][1]), Miss)
                self.assertIsInstance(cache.lookup(entries[2][1]), ExactHit)
                self.assertEqual(cache.stats().entries, 2)
            finally:
                cache.close()

    def test_clear_is_independent_and_does_not_touch_activity_ledger(self) -> None:
        _, _, candidate = _candidate("clear request", "safe")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            activity = root / "activity.sqlite3"
            activity.write_bytes(b"collector-owned-ledger")
            path = root / "gateway-cache.sqlite3"
            cache = SQLiteGatewayCache(path, size_cap_bytes=4096)
            try:
                self.assertTrue(cache.store(candidate))
                cache.clear()
                self.assertEqual(cache.stats().entries, 0)
            finally:
                cache.close()

            self.assertEqual(path.name, "gateway-cache.sqlite3")
            self.assertEqual(activity.read_bytes(), b"collector-owned-ledger")

    def test_file_is_private_wal_database_with_documented_default_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            cache = SQLiteGatewayCache(path)
            try:
                self.assertEqual(cache.journal_mode, "wal")
                self.assertEqual(cache.size_cap_bytes, 100 * 1024 * 1024)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                cache.close()

    def test_concurrent_writers_do_not_lose_cache_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            initializer = SQLiteGatewayCache(path, size_cap_bytes=1024 * 1024)
            initializer.close()
            barrier = threading.Barrier(4)
            caches = [
                SQLiteGatewayCache(path, size_cap_bytes=1024 * 1024)
                for _ in range(4)
            ]

            def write(index: int) -> None:
                _, _, candidate = _candidate(
                    f"concurrent request {index}", f"safe-{index}"
                )
                barrier.wait(timeout=10)
                if not caches[index].store(candidate):
                    raise AssertionError("safe cache entry was rejected")

            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(write, index) for index in range(4)]
                    for future in futures:
                        future.result(timeout=20)
            finally:
                for cache in caches:
                    cache.close()

            cache = SQLiteGatewayCache(path, size_cap_bytes=1024 * 1024)
            try:
                self.assertEqual(cache.stats().entries, 4)
            finally:
                cache.close()

    def test_database_and_sidecars_never_contain_request_or_response_pii(self) -> None:
        request_pii = "jane@example.com"
        response_pii = "private response for jane@example.com"
        prompt_prefix = "private prompt prefix jane@example.com"
        prefix = Redactor().redact(prompt_prefix)
        _, _, candidate = _candidate(
            f"email {request_pii}",
            response_pii,
            prefixes=(prefix,),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            cache = SQLiteGatewayCache(path, size_cap_bytes=4096)
            try:
                self.assertTrue(cache.store(candidate))
            finally:
                cache.close()

            raw = b"".join(
                candidate_path.read_bytes()
                for candidate_path in (
                    path,
                    Path(f"{path}-wal"),
                    Path(f"{path}-shm"),
                )
                if candidate_path.exists()
            )
            self.assertNotIn(request_pii.encode("utf-8"), raw)
            self.assertNotIn(response_pii.encode("utf-8"), raw)
            self.assertNotIn(prompt_prefix.encode("utf-8"), raw)

    def test_sentence_final_email_never_reaches_cache_database_or_sidecars(self) -> None:
        email = "jane@example.com"
        request_text = f"Email {email}."
        response_text = f"The receipt was sent to {email}."
        _, keys, candidate = _candidate(request_text, response_text)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-cache.sqlite3"
            cache = SQLiteGatewayCache(path, size_cap_bytes=4096)
            try:
                self.assertTrue(cache.store(candidate))
                hit = cache.lookup(keys)
            finally:
                cache.close()

            self.assertIsInstance(hit, ExactHit)
            self.assertNotIn(email, hit.redacted_response)
            raw = b"".join(
                candidate_path.read_bytes()
                for candidate_path in (
                    path,
                    Path(f"{path}-wal"),
                    Path(f"{path}-shm"),
                )
                if candidate_path.exists()
            )
            self.assertNotIn(email.encode("utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
