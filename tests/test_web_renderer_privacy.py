import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = ROOT / "web/src"
ADD_PROVIDER_DIALOG = WEB_SOURCE / "components/AddProviderDialog.tsx"
PROVIDER_ACCOUNT_ACTIONS = WEB_SOURCE / "components/ProviderAccountActions.tsx"

ALLOWED_STORAGE_KEYS = {"usagehub.lang", "usagehub.providers.layout"}
FORBIDDEN_VALUE_NAME = re.compile(
    r"api\s*[_-]?\s*key|cookie|session|token|authorization",
    re.IGNORECASE,
)
STORAGE_CALL = re.compile(
    r"\b(?:localStorage|sessionStorage)\s*\.\s*setItem\s*\(",
)
STORAGE_WRITE = re.compile(
    r"\b(?P<storage>localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*"
    r"(?P<quote>['\"])(?P<key>[^'\"]+)(?P=quote)\s*,\s*"
    r"(?P<value>.*?)\)\s*;",
    re.DOTALL,
)
STATE_DECLARATION = re.compile(
    r"\bconst\s*\[\s*(?P<name>[A-Za-z_$][\w$]*)\s*,[^]]*]\s*=\s*useState\b",
)
INPUT_ELEMENT = re.compile(r"<input\b(?P<attributes>[^>]*)>", re.DOTALL)
CONSOLE_PROBE_FETCH = re.compile(
    r"\bfetch\s*\(\s*selected\.consoleUrl\s*,\s*\{(?P<options>.*?)\}\s*\)",
    re.DOTALL,
)


def renderer_sources() -> list[Path]:
    return sorted((*WEB_SOURCE.rglob("*.ts"), *WEB_SOURCE.rglob("*.tsx")))


def normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


class WebRendererPrivacyTests(unittest.TestCase):
    def test_browser_storage_writes_are_non_secret_ui_preferences(self) -> None:
        violations: list[str] = []

        for path in renderer_sources():
            source = path.read_text(encoding="utf-8")
            calls = list(STORAGE_CALL.finditer(source))
            writes = list(STORAGE_WRITE.finditer(source))
            parsed_offsets = {match.start() for match in writes}
            relative_path = path.relative_to(ROOT)

            for call in calls:
                if call.start() not in parsed_offsets:
                    violations.append(
                        f"{relative_path}: storage writes require a literal allowlisted key"
                    )

            for write in writes:
                key = write.group("key")
                value = write.group("value")
                if key not in ALLOWED_STORAGE_KEYS:
                    violations.append(
                        f"{relative_path}: storage key {key!r} is not allowlisted"
                    )
                if FORBIDDEN_VALUE_NAME.search(value):
                    violations.append(
                        f"{relative_path}: storage value for {key!r} references secret-shaped data"
                    )

        self.assertEqual(violations, [])

    def test_add_provider_dialog_credential_state_is_ephemeral_and_host_action_only(self) -> None:
        source = ADD_PROVIDER_DIALOG.read_text(encoding="utf-8")
        # The CC Switch-style flow intentionally accepts an API key in the
        # renderer, but it must stay in component state (never persisted) and
        # only ever reach the trusted host action bridge.
        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)
        self.assertIn("apiKey", source)
        self.assertIn("providerConfigSave", source)
        # The credential leaves through the injected apply adapter only; the
        # dialog itself never fetches a network route with the key.
        self.assertNotIn('fetch("/v1', source)
        self.assertNotIn('fetch("/gateway', source)
        self.assertIn("apply(", source)
        api_source = (WEB_SOURCE / "api.ts").read_text(encoding="utf-8")
        self.assertIn("export async function applyProviderConfig", api_source)

    def test_gateway_account_actions_have_no_credential_inputs_or_state(self) -> None:
        source = PROVIDER_ACCOUNT_ACTIONS.read_text(encoding="utf-8")
        credential_markers = (
            "apikey",
            "authorization",
            "cookie",
            "credential",
            "password",
            "secret",
            "session",
            "token",
        )

        credential_state = [
            match.group("name")
            for match in STATE_DECLARATION.finditer(source)
            if any(
                marker in normalized_identifier(match.group("name"))
                for marker in credential_markers
            )
        ]
        credential_inputs = []
        for index, match in enumerate(INPUT_ELEMENT.finditer(source), start=1):
            attributes = match.group("attributes")
            normalized_attributes = normalized_identifier(attributes)
            if (
                re.search(r"\bpassword\b", attributes, re.IGNORECASE)
                or any(marker in normalized_attributes for marker in credential_markers)
            ):
                credential_inputs.append(index)

        self.assertEqual(credential_state, [])
        self.assertEqual(credential_inputs, [])

    def test_provider_config_apply_sends_credentials_only_to_the_host_action(self) -> None:
        api_source = (WEB_SOURCE / "api.ts").read_text(encoding="utf-8")
        # The only network route that may carry the API key is the desktop host
        # action bridge; it must be a same-origin POST with credentials omit.
        apply_match = re.search(
            r"export\s+async\s+function\s+applyProviderConfig\b(?P<body>[\s\S]*?)\n}",
            api_source,
        )
        self.assertIsNotNone(apply_match, "applyProviderConfig export is required")
        body = apply_match.group("body") if apply_match else ""

        violations = []
        if '"/host/v1/actions"' not in body:
            violations.append("apply must target /host/v1/actions")
        if not re.search(r"\bmethod\s*:\s*['\"]POST['\"]", body):
            violations.append("apply must use POST")
        if not re.search(r"\bcredentials\s*:\s*['\"]omit['\"]", body):
            violations.append("apply must explicitly omit browser credentials")
        if not re.search(r"\breferrerPolicy\s*:\s*['\"]no-referrer['\"]", body):
            violations.append("apply must suppress the referrer")
        if re.search(r"['\"]/v1/", body.replace("/host/v1/actions", "")):
            violations.append("apply must not send the key to any /v1 route")

        self.assertEqual(violations, [])
