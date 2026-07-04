from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
from pathlib import Path
import shutil
import urllib.request

from .paths import voices_dir


CATALOG_PATHS = (
    Path("/usr/share/piper-voices/voices.json"),
    Path("/usr/share/piper-voices.json"),
    Path("/usr/share/calibre/piper-voices.json"),
)
SYSTEM_VOICES_DIR = Path("/usr/share/piper-voices")
PIPER_VOICES_REVISION = "main"
PIPER_VOICES_BASE_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/{PIPER_VOICES_REVISION}"


@dataclass(slots=True)
class Voice:
    id: str
    language: str
    name: str
    quality: str
    language_name: str | None = None
    model_url: str | None = None
    config_url: str | None = None
    model_file: str | None = None
    config_file: str | None = None
    model_size_bytes: int | None = None
    config_size_bytes: int | None = None
    num_speakers: int | None = None
    speaker_id_map: dict | None = None
    sample_path: str | None = None
    sample_url: str | None = None
    samples: list[dict] | None = None
    model_path: Path | None = None
    config_path: Path | None = None

    @property
    def installed(self) -> bool:
        return bool(
            self.model_path
            and self.config_path
            and self.model_path.exists()
            and self.config_path.exists()
        )

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "id": self.id,
            "language": self.language,
            "name": self.name,
            "quality": self.quality,
            "language_name": self.language_name,
            "model_url": self.model_url,
            "config_url": self.config_url,
            "model_file": self.model_file,
            "config_file": self.config_file,
            "model_size_bytes": self.model_size_bytes,
            "config_size_bytes": self.config_size_bytes,
            "num_speakers": self.num_speakers,
            "speaker_id_map": self.speaker_id_map or {},
            "sample_path": self.sample_path,
            "sample_url": self.sample_url,
            "samples": self.samples or [],
            "model_path": str(self.model_path) if self.model_path else None,
            "config_path": str(self.config_path) if self.config_path else None,
            "installed": self.installed,
        }


def voice_paths(voice_id: str, base_dir: Path | None = None) -> tuple[Path, Path]:
    directory = base_dir or voices_dir()
    return directory / f"{voice_id}.onnx", directory / f"{voice_id}.onnx.json"


def find_installed_voice(voice_id: str, base_dir: Path | None = None) -> Voice | None:
    for directory in _voice_dirs(base_dir):
        model_path, config_path = voice_paths(voice_id, directory)
        if model_path.exists() and config_path.exists():
            language, name, quality = split_voice_id(voice_id)
            return Voice(
                id=voice_id,
                language=language,
                name=name,
                quality=quality,
                model_path=model_path,
                config_path=config_path,
            )

    return None


def split_voice_id(voice_id: str) -> tuple[str, str, str]:
    parts = voice_id.rsplit("-", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]

    return "", voice_id, ""


def list_installed(base_dir: Path | None = None) -> list[Voice]:
    voices: list[Voice] = []
    seen: set[str] = set()
    for directory in _voice_dirs(base_dir):
        for model_path in sorted(directory.glob("*.onnx")):
            config_path = model_path.with_suffix(model_path.suffix + ".json")
            if not config_path.exists() or model_path.stem in seen:
                continue

            language, name, quality = split_voice_id(model_path.stem)
            seen.add(model_path.stem)
            voices.append(
                Voice(
                    id=model_path.stem,
                    language=language,
                    name=name,
                    quality=quality,
                    model_path=model_path,
                    config_path=config_path,
                )
            )

    return voices


def load_catalog(path: Path | None = None) -> list[Voice]:
    bundled = _load_bundled_catalog()
    if path is None and bundled:
        return bundled

    catalog_path = path or next((p for p in CATALOG_PATHS if p.exists()), None)
    if not catalog_path:
        return []

    with catalog_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if "lang_map" not in raw:
        return _load_flat_catalog(raw)

    lang_map = raw.get("lang_map", {})
    voices: list[Voice] = []
    for language, speakers in lang_map.items():
        for speaker_name, qualities in speakers.items():
            for quality, links in qualities.items():
                voice_id = f"{language}-{speaker_name}-{quality}"
                model_path, config_path = voice_paths(voice_id)
                voices.append(
                Voice(
                    id=voice_id,
                    language=language,
                    name=speaker_name,
                    quality=quality,
                    language_name=language,
                    model_url=links.get("model"),
                    config_url=links.get("config"),
                    model_file=_repo_path_from_url(links.get("model")),
                    config_file=_repo_path_from_url(links.get("config")),
                    sample_url=_sample_url_from_model_url(links.get("model")),
                    model_path=model_path,
                    config_path=config_path,
                )
                )

    return sorted(voices, key=lambda voice: (voice.language, voice.name, voice.quality))


def _load_flat_catalog(raw: dict) -> list[Voice]:
    voices: list[Voice] = []
    for voice_id, metadata in raw.items():
        if not isinstance(metadata, dict):
            continue

        files = metadata.get("files", {})
        model_file = next((name for name in files if name.endswith(".onnx")), None)
        config_file = next((name for name in files if name.endswith(".onnx.json")), None)
        language_info = metadata.get("language", {})
        language = language_info.get("code", "")
        language_name = language_info.get("name_english") or language
        name = metadata.get("name", "")
        quality = metadata.get("quality", "")
        model_path, config_path = voice_paths(voice_id)
        voices.append(
            Voice(
                id=voice_id,
                language=language,
                name=name,
                quality=quality,
                language_name=language_name,
                model_url=_catalog_url(model_file),
                config_url=_catalog_url(config_file),
                model_file=model_file,
                config_file=config_file,
                sample_url=_catalog_url(_sample_path_from_model_path(model_file)),
                model_path=model_path,
                config_path=config_path,
            )
        )

    return sorted(voices, key=lambda voice: (voice.language, voice.name, voice.quality))


def _load_bundled_catalog() -> list[Voice]:
    try:
        catalog = resources.files("pauper.resources") / "voices.json"
        if not catalog.is_file():
            return []
        raw = json.loads(catalog.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError):
        return []

    voices = []
    for entry in raw.get("voices", []):
        voice_id = entry["id"]
        model_path, config_path = voice_paths(voice_id)
        voices.append(
            Voice(
                id=voice_id,
                language=entry.get("language", ""),
                name=entry.get("name", ""),
                quality=entry.get("quality", ""),
                language_name=entry.get("language_name"),
                model_url=entry.get("model_url"),
                config_url=entry.get("config_url"),
                model_file=entry.get("model_file"),
                config_file=entry.get("config_file"),
                model_size_bytes=entry.get("model_size_bytes"),
                config_size_bytes=entry.get("config_size_bytes"),
                num_speakers=entry.get("num_speakers"),
                speaker_id_map=entry.get("speaker_id_map", {}),
                sample_path=entry.get("sample_path"),
                sample_url=entry.get("sample_url"),
                samples=entry.get("samples", []),
                model_path=model_path,
                config_path=config_path,
            )
        )

    return sorted(voices, key=lambda voice: (voice.language, voice.name, voice.quality))


def merged_voices() -> list[Voice]:
    catalog = {voice.id: voice for voice in load_catalog()}
    for installed in list_installed():
        voice = catalog.get(installed.id)
        if voice is None:
            catalog[installed.id] = installed
            continue

        voice.model_path = installed.model_path
        voice.config_path = installed.config_path

    return sorted(catalog.values(), key=lambda voice: (voice.language, voice.name, voice.quality))


def download_voice(voice_id: str, destination: Path | None = None) -> Voice:
    destination_dir = destination or voices_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)

    catalog = {voice.id: voice for voice in load_catalog()}
    voice = catalog.get(voice_id)
    if not voice or not voice.model_url or not voice.config_url:
        raise ValueError(f"voice is not in the local catalog: {voice_id}")

    model_path, config_path = voice_paths(voice_id, destination_dir)
    _download(voice.model_url, model_path)
    _download(voice.config_url, config_path)

    return Voice(
        id=voice.id,
        language=voice.language,
        name=voice.name,
        quality=voice.quality,
        model_url=voice.model_url,
        config_url=voice.config_url,
        model_file=voice.model_file,
        config_file=voice.config_file,
        model_size_bytes=voice.model_size_bytes,
        config_size_bytes=voice.config_size_bytes,
        num_speakers=voice.num_speakers,
        speaker_id_map=voice.speaker_id_map,
        samples=voice.samples,
        sample_path=voice.sample_path,
        sample_url=voice.sample_url,
        model_path=model_path,
        config_path=config_path,
    )


def delete_voice(voice_id: str, destination: Path | None = None) -> Voice:
    destination_dir = destination or voices_dir()
    model_path, config_path = voice_paths(voice_id, destination_dir)
    if not model_path.exists() and not config_path.exists():
        raise ValueError(f"downloaded voice is not installed: {voice_id}")

    model_path.unlink(missing_ok=True)
    config_path.unlink(missing_ok=True)
    model_path.with_suffix(model_path.suffix + ".part").unlink(missing_ok=True)
    config_path.with_suffix(config_path.suffix + ".part").unlink(missing_ok=True)

    language, name, quality = split_voice_id(voice_id)
    return Voice(
        id=voice_id,
        language=language,
        name=name,
        quality=quality,
        model_path=model_path,
        config_path=config_path,
    )


def is_downloaded_voice(voice_id: str, destination: Path | None = None) -> bool:
    model_path, config_path = voice_paths(voice_id, destination)
    return model_path.exists() and config_path.exists()


def _download(url: str, destination: Path) -> None:
    tmp_path = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(url) as response, tmp_path.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp_path.replace(destination)


def _voice_dirs(base_dir: Path | None = None) -> list[Path]:
    dirs = [base_dir or voices_dir()]
    if SYSTEM_VOICES_DIR not in dirs:
        dirs.append(SYSTEM_VOICES_DIR)
    return dirs


def _catalog_url(path: str | None) -> str | None:
    if not path:
        return None
    return f"{PIPER_VOICES_BASE_URL}/{path}?download=true"


def _sample_path_from_model_path(path: str | None) -> str | None:
    if not path:
        return None
    model_path = Path(path)
    return str(model_path.parent / "samples" / "speaker_0.mp3")


def _sample_url_from_model_url(url: str | None) -> str | None:
    if not url:
        return None

    base = url.split("?", 1)[0]
    filename = base.rsplit("/", 1)[-1]
    return base.removesuffix(filename) + "samples/speaker_0.mp3?download=true"


def _repo_path_from_url(url: str | None) -> str | None:
    if not url:
        return None

    marker = "/resolve/"
    if marker not in url:
        return None

    parts = url.split(marker, 1)[1].split("?", 1)[0].split("/", 1)
    return parts[1] if len(parts) == 2 else None
