from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import unittest

from openusage_bar.gateway.pii import RedactionResult, Redactor


class GatewayPIIRedactionTests(unittest.TestCase):
    def test_confident_identity_patterns_are_reversible_and_cacheable(self) -> None:
        cases = (
            ("email", "contact jane@example.com for details", "jane@example.com"),
            (
                "mac_path",
                "read /Users/lune/private/notes.txt",
                "/Users/lune/private/notes.txt",
            ),
            (
                "linux_path",
                "read /home/alice/private/notes.txt",
                "/home/alice/private/notes.txt",
            ),
            (
                "windows_path",
                r"read C:\Users\Alice\private\notes.txt",
                r"C:\Users\Alice\private\notes.txt",
            ),
            (
                "escaped_windows_path",
                r"read C:\\Users\\Alice\\private\\notes.txt",
                r"C:\\Users\\Alice\\private\\notes.txt",
            ),
            ("phone", "call +1 (415) 555-2671 tomorrow", "+1 (415) 555-2671"),
        )

        for label, original, private_value in cases:
            with self.subTest(pattern=label):
                result = Redactor().redact(original)

                self.assertIsInstance(result, RedactionResult)
                self.assertTrue(result.cacheable)
                self.assertIsNone(result.bypass_reason)
                self.assertEqual(result.placeholder_count, 1)
                self.assertNotIn(private_value, result.redacted_text)
                self.assertEqual(result.rehydrate(result.redacted_text), original)

    def test_credentials_are_redacted_but_always_bypass_cache(self) -> None:
        credentials = (
            "Bearer bearer-value-0123456789abcdef",
            "sk-proj-0123456789abcdefghijklmnopqrstuvwxyz",
            "sk-ant-api03-0123456789abcdefghijklmnopqrstuvwxyz",
            "sk-or-v1-0123456789abcdefghijklmnopqrstuvwxyz",
            "AIzaSy0123456789abcdefghijklmnopqrstuvwxyz",
            "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        )

        for credential in credentials:
            with self.subTest(prefix=credential.split("-", 1)[0]):
                original = f"credential={credential}"
                result = Redactor().redact(original)

                self.assertFalse(result.cacheable)
                self.assertIsNotNone(result.bypass_reason)
                self.assertNotIn(credential, result.redacted_text)
                self.assertEqual(result.rehydrate(result.redacted_text), original)

    def test_configured_literal_is_redacted_but_always_bypasses_cache(self) -> None:
        secret = "workspace-private-literal-42"
        result = Redactor(literal_secrets=(secret,)).redact(
            f"do not persist {secret} anywhere"
        )

        self.assertFalse(result.cacheable)
        self.assertNotIn(secret, result.redacted_text)
        self.assertEqual(
            result.rehydrate(result.redacted_text),
            f"do not persist {secret} anywhere",
        )

    def test_unclassified_high_entropy_value_is_redacted_and_fails_closed(self) -> None:
        unknown = "Q7vz9Wm2Kx4Rp8Nc6Ty3Hs5Jd1Lf0BaUe9Gi2ZoV"
        original = f"opaque={unknown}"

        result = Redactor().redact(original)

        self.assertFalse(result.cacheable)
        self.assertIsNotNone(result.bypass_reason)
        self.assertNotIn(unknown, result.redacted_text)
        self.assertEqual(result.rehydrate(result.redacted_text), original)

    def test_reserved_placeholder_collision_fails_closed(self) -> None:
        original = "literal __OPENUSAGE_EMAIL_1__ supplied by a caller"

        result = Redactor().redact(original)

        self.assertFalse(result.cacheable)
        self.assertIsNotNone(result.bypass_reason)
        self.assertEqual(result.rehydrate(result.redacted_text), original)

    def test_repeated_value_uses_one_stable_placeholder(self) -> None:
        original = "jane@example.com then jane@example.com"

        result = Redactor().redact(original)

        self.assertEqual(result.placeholder_count, 1)
        self.assertEqual(len(set(result.redacted_text.split(" then "))), 1)
        self.assertEqual(result.rehydrate(result.redacted_text), original)

    def test_sentence_final_email_is_redacted_in_request_and_response(self) -> None:
        request_text = "Email jane@example.com."
        response_text = "The receipt was sent to jane@example.com."

        request = Redactor().redact(request_text)
        response = request.redact_response(response_text)

        self.assertTrue(request.cacheable)
        self.assertNotIn("jane@example.com", request.redacted_text)
        self.assertEqual(request.rehydrate(request.redacted_text), request_text)
        self.assertTrue(response.cacheable)
        self.assertNotIn("jane@example.com", response.redacted_text)
        self.assertEqual(response.rehydrate(response.redacted_text), response_text)

    def test_response_reuses_request_placeholder_and_rejects_new_identity(self) -> None:
        request = Redactor().redact("Send the receipt to jane@example.com")
        request_placeholder = request.redacted_text.removeprefix(
            "Send the receipt to "
        )

        matching = request.redact_response(
            "The receipt was sent to jane@example.com"
        )
        unseen = request.redact_response(
            "The receipt was forwarded to other@example.net"
        )
        injected_placeholder = request.redact_response(
            "The receipt was sent to __OPENUSAGE_EMAIL_999__"
        )

        self.assertTrue(matching.cacheable)
        self.assertIn(request_placeholder, matching.redacted_text)
        self.assertEqual(
            matching.rehydrate(matching.redacted_text),
            "The receipt was sent to jane@example.com",
        )
        self.assertFalse(unseen.cacheable)
        self.assertNotIn("other@example.net", unseen.redacted_text)
        self.assertFalse(injected_placeholder.cacheable)

    def test_repr_and_public_serialization_never_expose_originals_or_map(self) -> None:
        secret = "workspace-private-literal-42"
        result = Redactor(literal_secrets=(secret,)).redact(
            f"jane@example.com has {secret}"
        )

        rendered = repr(result)
        public_json = json.dumps(result.to_public_dict(), sort_keys=True)
        for forbidden in ("jane@example.com", secret, "rehydration", "original"):
            self.assertNotIn(forbidden, rendered)
            self.assertNotIn(forbidden, public_json)

    def test_tampered_result_fails_closed_at_every_public_boundary(self) -> None:
        marker = "sk-review-secret-material"
        original = "contact jane@example.com"
        mutations = {
            "_bypass_reason": lambda: marker,
            "_cacheable": lambda: marker,
            "_integrity": lambda: marker,
            "_lineage": lambda: marker,
            "_mapping": lambda: {"__OPENUSAGE_EMAIL_1__": marker},
            "_redacted_text": lambda: marker,
            "_role": lambda: marker,
            "_seal": lambda: marker,
            "_source_sha256": lambda: marker,
            "_used_placeholders": lambda: frozenset({marker}),
        }
        public_boundaries = {
            "redacted_text": lambda value, _: value.redacted_text,
            "cacheable": lambda value, _: value.cacheable,
            "bypass_reason": lambda value, _: value.bypass_reason,
            "placeholder_count": lambda value, _: value.placeholder_count,
            "rehydrate": lambda value, safe_text: value.rehydrate(safe_text),
            "redact_response": lambda value, _: value.redact_response(
                "sent to jane@example.com"
            ),
            "to_public_dict": lambda value, _: value.to_public_dict(),
            "repr": lambda value, _: repr(value),
        }

        self.assertEqual(set(mutations), set(RedactionResult.__slots__))
        for slot, replacement in mutations.items():
            for boundary, observe in public_boundaries.items():
                with self.subTest(slot=slot, boundary=boundary):
                    result = Redactor().redact(original)
                    safe_text = result.redacted_text
                    object.__setattr__(result, slot, replacement())

                    if boundary == "repr":
                        rendered = observe(result, safe_text)
                        self.assertNotIn(marker, rendered)
                        self.assertNotIn("jane@example.com", rendered)
                        self.assertIn(
                            rendered,
                            ("<invalid>", "RedactionResult(<invalid>)"),
                        )
                        continue

                    try:
                        observe(result, safe_text)
                    except ValueError as error:
                        self.assertNotIn(marker, str(error))
                        self.assertNotIn(marker, repr(error))
                    except Exception as error:
                        self.assertNotIn(marker, str(error))
                        self.assertNotIn(marker, repr(error))
                        self.fail(
                            "tampered public boundary raised "
                            f"unexpected {type(error).__name__}"
                        )
                    else:
                        self.fail("tampered public boundary did not fail closed")

    def test_request_local_rehydration_maps_are_isolated_under_concurrency(self) -> None:
        barrier_workers = 8

        def redact(index: int) -> tuple[str, str]:
            original = f"contact person{index}@example.com"
            result = Redactor().redact(original)
            return result.redacted_text, result.rehydrate(result.redacted_text)

        with ThreadPoolExecutor(max_workers=barrier_workers) as pool:
            results = tuple(pool.map(redact, range(barrier_workers)))

        placeholders = {redacted for redacted, _ in results}
        self.assertEqual(len(placeholders), 1)
        self.assertEqual(
            {rehydrated for _, rehydrated in results},
            {f"contact person{index}@example.com" for index in range(barrier_workers)},
        )

    def test_plain_text_is_unchanged_and_has_no_rehydration_state(self) -> None:
        original = "Summarize the public release notes."

        result = Redactor().redact(original)

        self.assertTrue(result.cacheable)
        self.assertEqual(result.redacted_text, original)
        self.assertEqual(result.placeholder_count, 0)
        self.assertIsNone(result.bypass_reason)
        self.assertEqual(result.rehydrate(original), original)


if __name__ == "__main__":
    unittest.main()
