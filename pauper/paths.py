from __future__ import annotations

import os
from pathlib import Path


APP_ID = "io.github.mike_cr.Pauper"
APP_NAME = "pauper"


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


def xdg_runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")
    return Path(runtime)


def config_dir() -> Path:
    return xdg_config_home() / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    return xdg_data_home() / APP_NAME


def voices_dir() -> Path:
    return data_dir() / "voices"


def socket_dir() -> Path:
    return xdg_runtime_dir() / APP_NAME


def socket_path() -> Path:
    return socket_dir() / "socket"


def private_python_dir() -> Path:
    return Path(os.environ.get("PAUPER_PRIVATE_PYTHON", "/usr/lib/pauper/python"))
