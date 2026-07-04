from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from . import __version__
from .config import load_config
from .paths import socket_path
from .protocol import connect_socket, recv_json_line, send_json
from .voices import Voice
from .voices import download_voice as download_voice_file
from .voices import find_installed_voice, list_installed, load_catalog


def request(payload: dict[str, Any], socket: Path, timeout: float = 5.0) -> tuple[dict[str, Any], bytes]:
    with connect_socket(socket, timeout=timeout) as sock:
        send_json(sock, payload)
        file_obj = sock.makefile("rb")
        header = recv_json_line(file_obj)
        if not header.get("ok"):
            raise RuntimeError(str(header.get("error", "request failed")))

        size = int(header.get("bytes") or 0)
        body = file_obj.read(size) if size else b""
        if len(body) != size:
            raise RuntimeError("short audio response from pauperd")
        return header, body


def cmd_status(args: argparse.Namespace) -> int:
    header, _ = request({"action": "status"}, args.socket)
    if args.json:
        print(json.dumps(header, indent=2, sort_keys=True))
        return 0

    print(format_status(header))
    return 0


def cmd_models_available(args: argparse.Namespace) -> int:
    downloaded = {voice.id for voice in list_installed()}
    voices = [voice for voice in load_catalog() if voice.id not in downloaded]
    if args.json:
        print(json.dumps([voice.to_dict() for voice in voices], indent=2, sort_keys=True))
        return 0

    print_voice_table(voices)
    return 0


def cmd_models_downloaded(args: argparse.Namespace) -> int:
    voices = list_installed()
    if args.json:
        print(json.dumps([voice.to_dict() for voice in voices], indent=2, sort_keys=True))
        return 0

    print_voice_table(voices, show_paths=args.paths)
    return 0


def print_voice_table(voices: list[Voice], show_paths: bool = False) -> None:
    if not voices:
        print("No models found.")
        return

    rows = []
    for voice in voices:
        size = format_bytes(voice.model_size_bytes)
        speakers = str(voice.num_speakers or len(voice.speaker_id_map or {}) or 1)
        row = [
            voice.id,
            voice.language_name or voice.language,
            voice.quality,
            speakers,
            size,
        ]
        if show_paths:
            row.append(str(voice.model_path) if voice.model_path else "")
        rows.append(row)

    headers = ["Model", "Language", "Quality", "Speakers", "Size"]
    if show_paths:
        headers.append("Path")
    print_table(headers, rows)


def cmd_set_default(args: argparse.Namespace) -> int:
    header, _ = request(voice_payload("set_default", args), args.socket)
    print(json.dumps(header, indent=2, sort_keys=True))
    return 0


def cmd_load_voice(args: argparse.Namespace) -> int:
    header, _ = request(voice_payload("load_voice", args), args.socket)
    print(json.dumps(header, indent=2, sort_keys=True))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    voice = download_voice_file(args.voice)
    print(json.dumps(voice.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_unload(args: argparse.Namespace) -> int:
    header, _ = request({"action": "unload_voice"}, args.socket)
    print(json.dumps(header, indent=2, sort_keys=True))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_speak(args: argparse.Namespace) -> int:
    text = text_from_args(args)
    synthesis = {
        "speaker": args.speaker,
        "length_scale": args.length_scale,
        "noise_scale": args.noise_scale,
        "noise_w_scale": args.noise_w_scale,
        "volume": args.volume,
    }
    header, audio = request(
        {"action": "synthesize", "text": text, "synthesis": synthesis},
        args.socket,
    )

    if args.output:
        args.output.write_bytes(audio)
    elif args.play:
        play_wav(audio)
    else:
        sys.stdout.buffer.write(audio)

    if args.verbose:
        print(json.dumps(header, indent=2, sort_keys=True), file=sys.stderr)
    return 0


def format_status(status: dict[str, Any]) -> str:
    lines = [
        "Pauper daemon",
        f"  Socket: {value_or_none(status.get('socket'))}",
        f"  Ready to synthesize: {yes_no(status.get('ready'))}",
        "",
        "Voices",
        f"  Default: {format_voice(status.get('configured_voice'), status.get('configured_speaker'))}",
        f"  Synthesis: {format_voice(status.get('synthesis_voice'), status.get('synthesis_speaker'))}",
        f"  In memory: {format_voice(status.get('loaded_voice'), status.get('loaded_speaker'))}",
        "",
        "Settings",
        f"  Load on demand: {yes_no(status.get('lazy_load'))}",
        f"  Retain models: {format_retention(status.get('retention_seconds'))}",
        f"  ONNX provider: {value_or_none(status.get('execution_provider'))}",
        f"  Audio output: {audio_output_label(status.get('audio_output'))}",
    ]

    loaded_provider = status.get("loaded_execution_provider")
    if loaded_provider:
        lines.append(f"  Loaded provider: {loaded_provider}")

    recommended = status.get("recommended_execution_provider")
    if recommended and recommended != status.get("execution_provider"):
        lines.append(f"  Recommended provider: {recommended}")

    return "\n".join(lines)


def format_voice(voice: Any, speaker: Any = None) -> str:
    if not voice:
        return "none"
    if speaker is None:
        return str(voice)
    return f"{voice} (speaker {speaker})"


def format_retention(value: Any) -> str:
    if value is None:
        return "keep loaded"
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return value_or_none(value)
    if seconds <= 0:
        return "unload immediately"
    if seconds == 60:
        return "1 minute"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minutes"
    return f"{seconds} seconds"


def yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def value_or_none(value: Any) -> str:
    if value is None or value == "":
        return "none"
    return str(value)


def audio_output_label(value: Any) -> str:
    if value is None or value == "":
        return "default"
    return str(value)


def format_bytes(value: Any) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""

    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024

    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    print("  ".join("-" * width for width in widths).rstrip())
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())


def cmd_install_speechd_user(args: argparse.Namespace) -> int:
    config_dir = Path.home() / ".config" / "speech-dispatcher"
    modules_dir = config_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    destination = modules_dir / "pauper-generic.conf"
    source = Path(__file__).resolve().parent.parent / "data" / "speech-dispatcher" / "modules" / "pauper-generic.conf"
    if not source.exists():
        source = Path("/etc/speech-dispatcher/modules/pauper-generic.conf")
    if not source.exists():
        raise RuntimeError("cannot find pauper-generic.conf")

    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Installed {destination}")
    print(f"Copy or merge this into {config_dir / 'speechd.conf'} to make Speech Dispatcher use Pauper:")
    print('AddModule "pauper" "sd_generic" "pauper-generic.conf"')
    print("DefaultModule pauper")
    return 0


def text_from_args(args: argparse.Namespace) -> str:
    if args.text:
        return " ".join(args.text)

    data = os.environ.get("DATA")
    if data:
        return data

    return sys.stdin.read()


def voice_payload(action: str, args: argparse.Namespace) -> dict[str, Any]:
    model_path = args.model_path
    config_path = args.config_path
    if (model_path is None) != (config_path is None):
        raise RuntimeError("--model-path and --config-path must be used together")
    if model_path is None or config_path is None:
        voice = find_installed_voice(args.voice)
        if voice is None or not voice.model_path or not voice.config_path:
            raise RuntimeError(f"voice is not installed: {args.voice}")
        model_path = voice.model_path
        config_path = voice.config_path

    return {
        "action": action,
        "voice": args.voice,
        "model_path": str(model_path),
        "config_path": str(config_path),
        "speaker": args.speaker,
    }


def play_wav(audio: bytes, output: str | None = None) -> None:
    output = output if output is not None else load_config().audio_output
    if output:
        player = shutil.which("pw-play")
        if not player:
            raise RuntimeError("selected audio output requires pw-play from pipewire-bin")
        subprocess.run([player, "--target", output, "-"], input=audio, check=True)
        return

    player = shutil.which("pw-play") or shutil.which("paplay") or shutil.which("aplay")
    if not player:
        raise RuntimeError("no audio player found; install pipewire-bin, pulseaudio-utils, or alsa-utils")

    command = [player, "-"]
    if Path(player).name == "aplay":
        command = [player, "-q", "-"]

    subprocess.run(command, input=audio, check=True)


def play_audio_file(path: Path) -> None:
    players = [
        ("gst-play-1.0", ["gst-play-1.0", "--no-interactive", str(path)]),
        ("mpv", ["mpv", "--really-quiet", str(path)]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)]),
        ("pw-play", ["pw-play", str(path)]),
        ("paplay", ["paplay", str(path)]),
    ]
    for executable, command in players:
        if shutil.which(executable):
            subprocess.run(command, check=True)
            return

    raise RuntimeError("no sample-capable audio player found; install gstreamer, mpv, ffmpeg, or pipewire")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client for pauperd")
    parser.add_argument("--socket", type=Path, default=socket_path())
    subparsers = parser.add_subparsers(required=True)

    status = subparsers.add_parser("status", help="show daemon status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    models = subparsers.add_parser("models", help="list voice models")
    model_subparsers = models.add_subparsers(required=True)

    downloaded = model_subparsers.add_parser("downloaded", help="list downloaded models")
    downloaded.add_argument("--json", action="store_true")
    downloaded.add_argument("--paths", action="store_true", help="show local model paths")
    downloaded.set_defaults(func=cmd_models_downloaded)

    available = model_subparsers.add_parser("available", help="list downloadable models not installed locally")
    available.add_argument("--json", action="store_true")
    available.set_defaults(func=cmd_models_available)

    set_default = subparsers.add_parser("set-default", help="set the startup default model")
    set_default.add_argument("voice")
    set_default.add_argument("--speaker", type=int)
    set_default.add_argument("--model-path", type=Path)
    set_default.add_argument("--config-path", type=Path)
    set_default.set_defaults(func=cmd_set_default)

    load_voice = subparsers.add_parser("load", help="set the current synthesis model")
    load_voice.add_argument("voice")
    load_voice.add_argument("--speaker", type=int)
    load_voice.add_argument("--model-path", type=Path)
    load_voice.add_argument("--config-path", type=Path)
    load_voice.set_defaults(func=cmd_load_voice)

    download = subparsers.add_parser("download", help="download a model")
    download.add_argument("voice")
    download.set_defaults(func=cmd_download)

    unload = subparsers.add_parser("unload", help="unload the model from memory")
    unload.set_defaults(func=cmd_unload)

    version = subparsers.add_parser("version", help="show Pauper version")
    version.set_defaults(func=cmd_version)

    speak = subparsers.add_parser("speak", help="synthesize text")
    speak.add_argument("text", nargs="*")
    speak.add_argument("--play", action="store_true")
    speak.add_argument("--output", type=Path)
    speak.add_argument("--speaker", type=int)
    speak.add_argument("--length-scale", type=float)
    speak.add_argument("--noise-scale", type=float)
    speak.add_argument("--noise-w-scale", type=float)
    speak.add_argument("--volume", type=float, default=1.0)
    speak.add_argument("--verbose", action="store_true")
    speak.set_defaults(func=cmd_speak)

    install = subparsers.add_parser("install-speechd-user", help="install user Speech Dispatcher module config")
    install.set_defaults(func=cmd_install_speechd_user)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"pauper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
