"""Pure platform mapping for private local runtime endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TypeAlias


_MAX_STATE_DIR_LENGTH = 4096
_RuntimePath: TypeAlias = PurePosixPath | PureWindowsPath


@dataclass(frozen=True)
class RuntimeDescriptor:
    local_api_transport: str
    local_api_host: str | None
    local_api_port: int | None
    local_api_socket_path: _RuntimePath | None
    local_api_token_path: _RuntimePath | None
    gateway_host: str
    gateway_port: int
    gateway_token_path: _RuntimePath
    plugin_host: str
    plugin_port: int
    plugin_state_dir: _RuntimePath
    plugin_database_path: _RuntimePath

    @classmethod
    def for_platform(
        cls,
        platform: str,
        *,
        state_dir: os.PathLike[str],
    ) -> RuntimeDescriptor:
        if not isinstance(platform, str):
            raise RuntimeError("unsupported platform")
        if platform == "win32":
            path_type = PureWindowsPath
        elif platform == "darwin" or platform.startswith("linux"):
            path_type = PurePosixPath
        else:
            raise RuntimeError("unsupported platform")

        directory = _validated_state_dir(state_dir, path_type=path_type)
        gateway_token_path = directory / "gateway.token"
        plugin_state_dir = directory / "plugin"

        if platform == "win32":
            return cls(
                local_api_transport="tcp",
                local_api_host="127.0.0.1",
                local_api_port=17821,
                local_api_socket_path=None,
                local_api_token_path=directory / "api.token",
                gateway_host="127.0.0.1",
                gateway_port=17823,
                gateway_token_path=gateway_token_path,
                plugin_host="127.0.0.1",
                plugin_port=17824,
                plugin_state_dir=plugin_state_dir,
                plugin_database_path=plugin_state_dir / "plugin.sqlite3",
            )

        return cls(
            local_api_transport="unix",
            local_api_host=None,
            local_api_port=None,
            local_api_socket_path=directory / "openusage.sock",
            local_api_token_path=None,
            gateway_host="127.0.0.1",
            gateway_port=17823,
            gateway_token_path=gateway_token_path,
            plugin_host="127.0.0.1",
            plugin_port=17824,
            plugin_state_dir=plugin_state_dir,
            plugin_database_path=plugin_state_dir / "plugin.sqlite3",
        )


def _validated_state_dir(
    value: object,
    *,
    path_type: type[PurePosixPath] | type[PureWindowsPath],
) -> _RuntimePath:
    if isinstance(value, (str, bytes)) or not isinstance(value, os.PathLike):
        raise ValueError("invalid state directory")

    try:
        raw = os.fspath(value)
    except Exception:
        raise ValueError("invalid state directory") from None

    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > _MAX_STATE_DIR_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("invalid state directory")

    try:
        directory = path_type(raw)
    except Exception:
        raise ValueError("invalid state directory") from None
    if (
        not directory.is_absolute()
        or ".." in directory.parts
        or len(directory.parts) <= 1
        or (
            isinstance(directory, PureWindowsPath)
            and directory.drive.startswith("\\\\")
        )
    ):
        raise ValueError("invalid state directory")
    return directory
