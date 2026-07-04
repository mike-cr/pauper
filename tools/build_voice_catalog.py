#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
import urllib.request


BASE_URL = "https://huggingface.co/rhasspy/piper-voices"
REVISION = "main"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac"}
socket.setdefaulttimeout(20)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate bundled Piper voice metadata and download preview samples."
    )
    parser.add_argument(
        "--source",
        default=f"{BASE_URL}/resolve/{REVISION}/voices.json",
        help="flat Piper voices.json catalog path or URL",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("pauper/resources/voices.json"),
        help="generated bundled catalog",
    )
    parser.add_argument(
        "--samples-dir",
        type=Path,
        default=Path("pauper/resources/samples"),
        help="directory for downloaded sample audio",
    )
    parser.add_argument("--limit", type=int, help="only process the first N voices")
    parser.add_argument(
        "--max-samples-per-voice",
        type=int,
        default=10,
        help="maximum preview samples to bundle for each voice",
    )
    args = parser.parse_args()

    raw = read_json(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.samples_dir.mkdir(parents=True, exist_ok=True)

    voices = []
    items = sorted(raw.items())
    if args.limit:
        items = items[: args.limit]

    for index, (voice_id, metadata) in enumerate(items, start=1):
        print(f"[{index}/{len(items)}] {voice_id}", file=sys.stderr)
        entry = build_entry(voice_id, metadata)
        if not url_exists(entry["model_url"]) or not url_exists(entry["config_url"]):
            print(f"  skipping missing model/config: {voice_id}", file=sys.stderr)
            continue

        samples = find_samples(entry["sample_dir"])[: args.max_samples_per_voice]
        if samples:
            bundled_samples = []
            for sample in samples:
                sample_url = resolve_url(sample)
                sample_ext = Path(sample).suffix.lower() or ".mp3"
                sample_stem = Path(sample).stem
                sample_file = args.samples_dir / f"{voice_id}-{sample_stem}{sample_ext}"
                print(f"  sample {sample_stem}", file=sys.stderr)
                if not download(sample_url, sample_file):
                    print(f"  skipping sample after retries: {sample}", file=sys.stderr)
                    continue
                bundled_samples.append(
                    {
                        "label": sample_label(sample, entry["speaker_id_map"]),
                        "speaker_id": sample_speaker_id(sample),
                        "source_path": sample,
                        "url": sample_url,
                        "path": f"samples/{sample_file.name}",
                    }
                )

            entry["samples"] = bundled_samples
            entry["sample_url"] = bundled_samples[0]["url"]
            entry["sample_path"] = bundled_samples[0]["path"]
        else:
            entry["samples"] = []
            entry["sample_url"] = None
            entry["sample_path"] = None

        voices.append(entry)
        time.sleep(0.05)

    catalog = {
        "version": 1,
        "source": f"{BASE_URL}/tree/{REVISION}",
        "voices": voices,
    }
    args.out.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prune_unreferenced_samples(args.samples_dir, voices)
    return 0


def build_entry(voice_id: str, metadata: dict) -> dict:
    files = metadata.get("files", {})
    model_file = next(name for name in files if name.endswith(".onnx"))
    config_file = next(name for name in files if name.endswith(".onnx.json"))
    language = metadata.get("language", {})

    return {
        "id": voice_id,
        "language": language.get("code", ""),
        "language_name": language.get("name_english") or language.get("code", ""),
        "language_native": language.get("name_native"),
        "language_family": language.get("family"),
        "language_region": language.get("region"),
        "name": metadata.get("name", ""),
        "quality": metadata.get("quality", ""),
        "num_speakers": metadata.get("num_speakers"),
        "speaker_id_map": metadata.get("speaker_id_map", {}),
        "aliases": metadata.get("aliases", []),
        "model_file": model_file,
        "config_file": config_file,
        "model_size_bytes": files.get(model_file, {}).get("size_bytes"),
        "config_size_bytes": files.get(config_file, {}).get("size_bytes"),
        "model_url": resolve_url(model_file),
        "config_url": resolve_url(config_file),
        "sample_dir": str(Path(model_file).parent / "samples"),
    }


def read_json(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    return json.loads(Path(source).read_text(encoding="utf-8"))


def find_samples(sample_dir: str) -> list[str]:
    api_url = (
        "https://huggingface.co/api/models/rhasspy/piper-voices/tree/"
        f"{REVISION}/{quote(sample_dir, safe='/')}?recursive=false"
    )
    try:
        with urllib.request.urlopen(api_url, timeout=30) as response:
            entries = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    except URLError:
        raise

    samples = (
        entry["path"]
        for entry in entries
        if entry.get("type") == "file" and Path(entry.get("path", "")).suffix.lower() in AUDIO_SUFFIXES
    )
    return sorted(samples, key=sample_sort_key)


def sample_sort_key(path: str) -> tuple[int, int | str, str]:
    speaker_id = sample_speaker_id(path)
    if speaker_id is not None:
        return (0, speaker_id, path)
    return (1, Path(path).stem, path)


def sample_speaker_id(path: str) -> int | None:
    stem = Path(path).stem
    if stem.startswith("speaker_"):
        suffix = stem.removeprefix("speaker_")
        if suffix.isdigit():
            return int(suffix)
    return None


def sample_label(path: str, speaker_id_map: dict) -> str:
    speaker_id = sample_speaker_id(path)
    if speaker_id is not None:
        for name, mapped_id in sorted(speaker_id_map.items()):
            if mapped_id == speaker_id:
                return f"{name} ({speaker_id})"
        return f"Speaker {speaker_id}"

    return Path(path).stem.replace("_", " ")


def resolve_url(path: str) -> str:
    return f"{BASE_URL}/resolve/{REVISION}/{quote(path, safe='/-_.')}?download=true"


def download(url: str, destination: Path) -> bool:
    if destination.exists() and destination.stat().st_size > 0:
        return True

    tmp_path = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=20) as response, tmp_path.open("wb") as out:
                shutil.copyfileobj(response, out)
            tmp_path.replace(destination)
            return True
        except (TimeoutError, HTTPError, URLError, OSError) as exc:
            tmp_path.unlink(missing_ok=True)
            print(f"    attempt {attempt}/3 failed: {exc}", file=sys.stderr)
            time.sleep(attempt)

    return False


def url_exists(url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=30):
            return True
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def prune_unreferenced_samples(samples_dir: Path, voices: list[dict]) -> None:
    referenced = {
        Path(sample["path"]).name
        for voice in voices
        for sample in voice.get("samples", [])
        if sample.get("path")
    }
    for path in samples_dir.iterdir():
        if path.is_file() and path.name not in referenced:
            path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
