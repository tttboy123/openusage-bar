"""Smoke the packaged Gateway account credential mutation against native storage."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, TextIO


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openusage_bar.gateway.config import GatewayConfigStore
from openusage_bar.keychain import default_keychain


VERSION = 1
PROVIDER_ID = "openai"
ALIAS_PREFIX = "CI Smoke"
MAX_PROTOCOL_BYTES = 16 * 1024
HELPER_TIMEOUT_SECONDS = 15
DISPLAY_ID = re.compile(r"^acct_[0-9a-f]{12}$")
SAFE_CODES = {
    "config_invalid",
    "invalid_settings_helper",
    "native_credential_mismatch",
    "settings_helper_failed",
}


class SmokeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code if code in SAFE_CODES else "settings_helper_failed")
        self.code = code if code in SAFE_CODES else "settings_helper_failed"


def resolve_settings_helper(value: object) -> Path:
    if type(value) is not str:
        raise SmokeFailure("invalid_settings_helper")
    active_platform = sys.platform
    if (
        not value
        or len(value) > 4096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise SmokeFailure("invalid_settings_helper")
    if active_platform == "win32" and (value.startswith("\\\\") or value.startswith("//")):
        raise SmokeFailure("invalid_settings_helper")
    path = Path(value)
    if not path.is_absolute() and (len(path.parts) < 2 or any(part == ".." for part in path.parts)):
        raise SmokeFailure("invalid_settings_helper")
    if any(part == ".." for part in path.parts):
        raise SmokeFailure("invalid_settings_helper")
    resolved = path if path.is_absolute() else Path.cwd() / path
    try:
        _reject_symlink_components(resolved)
        metadata = resolved.lstat()
    except OSError:
        raise SmokeFailure("invalid_settings_helper") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not _is_native_executable(resolved, active_platform)
    ):
        raise SmokeFailure("invalid_settings_helper")
    try:
        return resolved.resolve(strict=False)
    except OSError:
        raise SmokeFailure("invalid_settings_helper") from None


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise SmokeFailure("invalid_settings_helper")


def _is_native_executable(path: Path, platform: str) -> bool:
    if platform == "win32":
        return path.suffix.casefold() == ".exe"
    return os.access(path, os.X_OK)


def run_smoke(
    settings_helper: str | Path,
    *,
    store: object | None = None,
    keychain: object | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    secret1: str | None = None,
    secret2: str | None = None,
    nonce: str | None = None,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
) -> dict[str, object]:
    helper = resolve_settings_helper(str(settings_helper))
    resolved_store = store if store is not None else GatewayConfigStore()
    resolved_keychain = keychain if keychain is not None else default_keychain()
    resolved_nonce = nonce if nonce is not None else token_factory(16)
    resolved_secret1 = secret1 if secret1 is not None else token_factory(32)
    resolved_secret2 = secret2 if secret2 is not None else token_factory(32)
    if not _safe_generated_value(resolved_nonce):
        raise SmokeFailure("settings_helper_failed")
    if not _safe_generated_value(resolved_secret1) or not _safe_generated_value(resolved_secret2):
        raise SmokeFailure("settings_helper_failed")
    alias_create = f"{ALIAS_PREFIX} {resolved_nonce}"
    alias_edit = f"{ALIAS_PREFIX} {resolved_nonce} Edited"
    display_id: str | None = None
    credential_account: str | None = None
    removed = False
    try:
        created = _mutate(
            helper,
            {
                "version": VERSION,
                "action": "create_account",
                "account": {"providerId": PROVIDER_ID, "alias": alias_create},
                "credentialMaterial": {"providerKey": resolved_secret1},
            },
            command_runner=command_runner,
        )
        display_id = _public_display_id(created)
        account = _unique_smoke_account(resolved_store, display_id, alias_create)
        credential_account = account.credential_account
        _assert_secret(resolved_keychain, credential_account, resolved_secret1)

        edited = _mutate(
            helper,
            {
                "version": VERSION,
                "action": "edit_account",
                "account": {"displayId": display_id, "alias": alias_edit},
                "credentialMaterial": {"providerKey": resolved_secret2},
            },
            command_runner=command_runner,
        )
        if _public_display_id(edited) != display_id:
            raise SmokeFailure("config_invalid")
        account = _unique_smoke_account(resolved_store, display_id, alias_edit)
        if account.credential_account != credential_account:
            raise SmokeFailure("config_invalid")
        _assert_secret(resolved_keychain, credential_account, resolved_secret2)

        _mutate(
            helper,
            {
                "version": VERSION,
                "action": "remove_account",
                "account": {"displayId": display_id},
                "credentialMaterial": {},
            },
            command_runner=command_runner,
        )
        _assert_removed(resolved_store, resolved_keychain, display_id, credential_account)
        removed = True
        return {"version": VERSION, "ok": True, "code": "ok"}
    finally:
        if not removed:
            _cleanup(
                helper,
                resolved_store,
                resolved_keychain,
                display_id=display_id,
                credential_account=credential_account,
                aliases={alias_create, alias_edit},
                command_runner=command_runner,
            )


def _mutate(
    helper: Path,
    request: dict[str, object],
    *,
    command_runner: Callable[..., subprocess.CompletedProcess],
) -> dict[str, object]:
    encoded = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROTOCOL_BYTES:
        raise SmokeFailure("settings_helper_failed")
    try:
        completed = command_runner(
            [str(helper), "gateway-account-mutate"],
            input=encoded,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HELPER_TIMEOUT_SECONDS,
            shell=False,
        )
    except Exception:
        raise SmokeFailure("settings_helper_failed") from None
    if (
        completed.returncode != 0
        or completed.stderr != ""
        or type(completed.stdout) is not str
        or len(completed.stdout.encode("utf-8")) > MAX_PROTOCOL_BYTES
    ):
        raise SmokeFailure("settings_helper_failed")
    try:
        payload = _loads_strict(completed.stdout)
    except Exception:
        raise SmokeFailure("settings_helper_failed") from None
    if (
        type(payload) is not dict
        or set(payload) != {"version", "ok", "code", "account"}
        or payload.get("version") != VERSION
        or payload.get("ok") is not True
        or payload.get("code") != "ok"
        or type(payload.get("account")) is not dict
    ):
        raise SmokeFailure("settings_helper_failed")
    return payload


def _public_display_id(payload: dict[str, object]) -> str:
    account = payload.get("account")
    if type(account) is not dict or set(account) != {
        "alias",
        "displayId",
        "providerId",
        "state",
    }:
        raise SmokeFailure("settings_helper_failed")
    display_id = account.get("displayId")
    if type(display_id) is not str or DISPLAY_ID.fullmatch(display_id) is None:
        raise SmokeFailure("settings_helper_failed")
    if account.get("providerId") != PROVIDER_ID:
        raise SmokeFailure("settings_helper_failed")
    return display_id


def _unique_smoke_account(store: object, display_id: str, alias: str):
    try:
        config = store.load()
        matches = [
            account
            for account in config.accounts
            if account.display_id == display_id
            and account.provider_id == PROVIDER_ID
            and account.alias == alias
        ]
    except Exception:
        raise SmokeFailure("config_invalid") from None
    if len(matches) != 1:
        raise SmokeFailure("config_invalid")
    return matches[0]


def _assert_secret(keychain: object, credential_account: str, expected: str) -> None:
    try:
        value = keychain.get(credential_account)
    except Exception:
        raise SmokeFailure("native_credential_mismatch") from None
    if value != expected:
        raise SmokeFailure("native_credential_mismatch")


def _assert_removed(
    store: object,
    keychain: object,
    display_id: str,
    credential_account: str,
) -> None:
    try:
        config = store.load()
    except Exception:
        raise SmokeFailure("config_invalid") from None
    if any(account.display_id == display_id for account in config.accounts):
        raise SmokeFailure("config_invalid")
    try:
        value = keychain.get(credential_account)
    except Exception:
        raise SmokeFailure("native_credential_mismatch") from None
    if value is not None:
        raise SmokeFailure("native_credential_mismatch")


def _cleanup(
    helper: Path,
    store: object,
    keychain: object,
    *,
    display_id: str | None,
    credential_account: str | None,
    aliases: set[str],
    command_runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    cleanup_ids: list[str] = []
    if display_id is not None:
        cleanup_ids.append(display_id)
    try:
        config = store.load()
        for account in config.accounts:
            if (
                account.provider_id == PROVIDER_ID
                and account.alias in aliases
                and account.display_id not in cleanup_ids
            ):
                cleanup_ids.append(account.display_id)
                if credential_account is None:
                    credential_account = account.credential_account
    except Exception:
        pass
    for candidate in cleanup_ids:
        try:
            _mutate(
                helper,
                {
                    "version": VERSION,
                    "action": "remove_account",
                    "account": {"displayId": candidate},
                    "credentialMaterial": {},
                },
                command_runner=command_runner,
            )
        except Exception:
            pass
    if credential_account is not None:
        try:
            keychain.delete(credential_account)
        except Exception:
            pass


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    store: object | None = None,
    keychain: object | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    secret1: str | None = None,
    secret2: str | None = None,
    nonce: str | None = None,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
) -> int:
    del stderr
    try:
        helper = _parse_helper_arg(sys.argv[1:] if argv is None else argv)
        report = run_smoke(
            helper,
            store=store,
            keychain=keychain,
            command_runner=command_runner,
            secret1=secret1,
            secret2=secret2,
            nonce=nonce,
            token_factory=token_factory,
        )
        _write_envelope(stdout, True, str(report["code"]))
        return 0
    except SmokeFailure as error:
        _write_envelope(stdout, False, error.code)
        return 1
    except Exception:
        _write_envelope(stdout, False, "settings_helper_failed")
        return 1


def _write_envelope(output: TextIO, ok: bool, code: str) -> None:
    if not ok and code not in SAFE_CODES:
        code = "settings_helper_failed"
    json.dump(
        {"version": VERSION, "ok": ok, "code": code},
        output,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    output.write("\n")
    output.flush()


def _parse_helper_arg(argv: list[str]) -> str:
    args = list(argv)
    if len(args) == 1 and args[0] != "--settings-helper":
        return args[0]
    if len(args) == 2 and args[0] == "--settings-helper" and args[1]:
        return args[1]
    raise SmokeFailure("invalid_settings_helper")


def _safe_generated_value(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value.encode("utf-8")) <= 256
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _loads_strict(raw: str) -> object:
    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("invalid constant")

    return json.loads(
        raw,
        object_pairs_hook=no_duplicate_keys,
        parse_constant=reject_constant,
    )


if __name__ == "__main__":
    raise SystemExit(main())
