from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import tomllib
from typing import Any

from .paths import config_path, voices_dir


@dataclass(slots=True)
class AppConfig:
    voice: str | None = None
    voices_dir: str = str(voices_dir())
    model_path: str | None = None
    config_path: str | None = None
    speaker: int | None = None
    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w_scale: float | None = None
    volume: float = 1.0
    normalize_audio: bool = True
    execution_provider: str | None = None
    lazy_load: bool = False
    retention_seconds: int | None = None
    audio_output: str | None = None


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or config_path()
    if not cfg_path.exists():
        return AppConfig()

    raw = load_toml_config(cfg_path)
    return config_from_mapping(raw)


def config_from_mapping(raw: dict[str, Any]) -> AppConfig:
    valid = {field.name for field in AppConfig.__dataclass_fields__.values()}
    return AppConfig(**{key: value for key, value in raw.items() if key in valid})


def load_toml_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return raw if isinstance(raw, dict) else {}


def save_config(config: AppConfig, path: Path | None = None) -> None:
    cfg_path = path or config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg_path.open("w", encoding="utf-8") as f:
        f.write(toml_dumps(asdict(config)))


def toml_dumps(values: dict[str, Any]) -> str:
    lines = []
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"unsupported TOML config value for {value!r}")


def update_config(values: dict[str, Any], path: Path | None = None) -> AppConfig:
    config = load_config(path)
    for key, value in values.items():
        if hasattr(config, key):
            setattr(config, key, value)
    save_config(config, path)
    return config
