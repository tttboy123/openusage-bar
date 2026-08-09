import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = ROOT / "web/src"
ADD_PROVIDER_DIALOG = WEB_SOURCE / "components/AddProviderDialog.tsx"

ALLOWED_STORAGE_KEYS = {"usagehub.lang"}
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

    def test_add_provider_dialog_has_no_provider_credential_inputs_or_state(self) -> None:
        source = ADD_PROVIDER_DIALOG.read_text(encoding="utf-8")
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
                or any(
                    marker in normalized_attributes
                    for marker in credential_markers
                )
            ):
                credential_inputs.append(index)

        violations = []
        if credential_state:
            violations.append(
                f"Provider credential state is forbidden: {credential_state!r}"
            )
        if credential_inputs:
            violations.append(
                f"Provider credential inputs are forbidden: {credential_inputs!r}"
            )

        self.assertEqual(violations, [])

    def test_add_provider_dialog_endpoint_probe_omits_browser_credentials(self) -> None:
        source = ADD_PROVIDER_DIALOG.read_text(encoding="utf-8")
        match = CONSOLE_PROBE_FETCH.search(source)
        self.assertIsNotNone(
            match,
            "AddProviderDialog must probe provider consoles through selected.consoleUrl",
        )
        options = match.group("options") if match else ""
        compact_options = re.sub(r"\s+", "", options)

        violations = []
        if not re.search(r"\bmethod\s*:\s*['\"]HEAD['\"]", options):
            violations.append("console probe must use HEAD")
        if not re.search(r"\bmode\s*:\s*['\"]no-cors['\"]", options):
            violations.append("console probe must use no-cors")
        if not re.search(r"\bcredentials\s*:\s*['\"]omit['\"]", options):
            violations.append("console probe must explicitly omit browser credentials")
        if not re.search(r"\breferrerPolicy\s*:\s*['\"]no-referrer['\"]", options):
            violations.append("console probe must suppress the referrer")
        if re.search(r"\b(?:headers|body)\s*:", options):
            violations.append("console probe must not attach headers or a body")
        if re.search(
            r"authorization|api\s*[_-]?\s*key|cookie|session|token",
            compact_options,
            re.IGNORECASE,
        ):
            violations.append("console probe must not reference credential payload")

        self.assertEqual(violations, [])
