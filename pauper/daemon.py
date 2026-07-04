from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import socket
import socketserver
import sys
import threading
from typing import Any

import onnxruntime
from .paths import private_python_dir

PRIVATE_PYTHON = private_python_dir()
if PRIVATE_PYTHON.exists():
    sys.path.insert(0, str(PRIVATE_PYTHON))

from piper import PiperVoice, SynthesisConfig
from piper.voice import ESPEAK_DATA_DIR, PiperConfig

from .audio import pcm_to_wav
from .config import AppConfig, load_config, save_config
from .paths import socket_dir, socket_path
from .protocol import ProtocolError, recv_json_line, send_json
from .providers import all_provider_rankings, best_provider, ranked_provider_info, ranked_provider_names


LOG = logging.getLogger("pauperd")
UNSET = object()


class PiperState:
    def __init__(self, config_path: Path | None = None, bound_socket: Path | None = None) -> None:
        self.config_path = config_path
        self.bound_socket = bound_socket or socket_path()
        self.config = load_config(config_path)
        self.ensure_execution_provider()
        self.voice: PiperVoice | None = None
        self.loaded_voice_id: str | None = None
        self.loaded_speaker_id: int | None = None
        self.loaded_model_path: Path | None = None
        self.loaded_config_path: Path | None = None
        self.loaded_execution_provider: str | None = None
        self.synthesis_voice_id: str | None = None
        self.synthesis_speaker_id: int | None = None
        self.synthesis_model_path: Path | None = None
        self.synthesis_config_path: Path | None = None
        self.load_lock = threading.Lock()
        self.synth_lock = threading.Lock()
        self.retention_timer: threading.Timer | None = None

    def status(self) -> dict[str, Any]:
        self.clear_missing_configured_voice()
        synthesis = self.synthesis_target()
        available_providers = available_execution_providers()
        return {
            "configured_voice": self.config.voice,
            "configured_speaker": self.config.speaker,
            "configured_model_path": self.config.model_path,
            "configured_config_path": self.config.config_path,
            "synthesis_voice": synthesis["voice"],
            "synthesis_speaker": synthesis["speaker"],
            "synthesis_model_path": synthesis["model_path"],
            "synthesis_config_path": synthesis["config_path"],
            "synthesis_in_memory": synthesis["in_memory"],
            "loaded_voice": self.loaded_voice_id,
            "loaded_speaker": self.loaded_speaker_id,
            "loaded_model_path": str(self.loaded_model_path) if self.loaded_model_path else None,
            "loaded_config_path": str(self.loaded_config_path) if self.loaded_config_path else None,
            "loaded_execution_provider": self.loaded_execution_provider,
            "socket": str(self.bound_socket),
            "ready": self.has_synthesis_target(),
            "lazy_load": self.config.lazy_load,
            "retention_seconds": self.config.retention_seconds,
            "execution_provider": self.config.execution_provider,
            "audio_output": self.config.audio_output,
            "recommended_execution_provider": recommended_execution_provider(),
            "available_execution_providers": ranked_provider_names(available_providers),
            "available_execution_provider_rankings": ranked_provider_info(available_providers),
            "execution_provider_rankings": all_provider_rankings(available_providers),
        }

    def ensure_execution_provider(self) -> None:
        if self.config.execution_provider is not None:
            return

        available = available_execution_providers()
        provider = best_provider(available)
        if provider is None:
            raise RuntimeError("ONNX Runtime reports no available execution providers")

        self.config.execution_provider = provider
        save_config(self.config, self.config_path)
        LOG.info("execution provider selected: <auto> -> %s", provider)

    def update_settings(
        self,
        lazy_load: bool | None = None,
        retention_seconds: int | None = None,
        execution_provider: str | None = None,
        audio_output: str | None | object = UNSET,
    ) -> dict[str, Any]:
        old_lazy_load = self.config.lazy_load
        old_retention_seconds = self.config.retention_seconds
        old_execution_provider = self.config.execution_provider
        old_audio_output = self.config.audio_output
        if lazy_load is not None:
            self.config.lazy_load = lazy_load
        self.config.retention_seconds = retention_seconds
        if execution_provider is not None:
            validate_execution_provider(execution_provider)
            self.config.execution_provider = execution_provider
        if audio_output is not UNSET:
            self.config.audio_output = audio_output if isinstance(audio_output, str) and audio_output else None
        save_config(self.config, self.config_path)
        if (
            old_lazy_load != self.config.lazy_load
            or old_retention_seconds != self.config.retention_seconds
            or old_execution_provider != self.config.execution_provider
            or old_audio_output != self.config.audio_output
        ):
            LOG.info(
                "settings changed: lazy_load=%s -> %s, retention_seconds=%s -> %s, execution_provider=%s -> %s, audio_output=%s -> %s",
                old_lazy_load,
                self.config.lazy_load,
                old_retention_seconds,
                self.config.retention_seconds,
                old_execution_provider,
                self.config.execution_provider,
                old_audio_output,
                self.config.audio_output,
            )

        if old_execution_provider != self.config.execution_provider and self.voice is not None:
            self.unload_voice()

        if self.config.retention_seconds is None:
            self.cancel_retention_timer()
        elif self.voice is not None:
            self.schedule_retention_unload()

        if not self.config.lazy_load and self.has_synthesis_target():
            self.load_synthesis_target()

        return self.status()

    def set_default_voice(
        self,
        voice_id: str,
        model_path: Path,
        config_path: Path,
        speaker: int | None = None,
    ) -> dict[str, Any]:
        self._validate_voice_paths(model_path, config_path)

        self.config.voice = voice_id
        self.config.model_path = str(model_path)
        self.config.config_path = str(config_path)
        self.config.speaker = speaker
        save_config(self.config, self.config_path)
        return self.status()

    def set_synthesis_voice(
        self,
        voice_id: str,
        model_path: Path,
        config_path: Path,
        speaker: int | None = None,
        apply_policy: bool = True,
    ) -> dict[str, Any]:
        self._validate_voice_paths(model_path, config_path)
        old_target = self.synthesis_target()
        old_loaded_matches = self._loaded_voice_matches(voice_id, model_path, config_path)
        self.synthesis_voice_id = voice_id
        self.synthesis_model_path = model_path
        self.synthesis_config_path = config_path
        self.synthesis_speaker_id = speaker
        if self.config.voice is None:
            self.config.voice = voice_id
            self.config.model_path = str(model_path)
            self.config.config_path = str(config_path)
            self.config.speaker = speaker
            save_config(self.config, self.config_path)
            LOG.info("default voice set from first synthesis target: %s", voice_id)
        LOG.info(
            "synthesis target changed: %s -> %s",
            old_target["voice"] or "<none>",
            voice_id,
        )

        if self.voice is not None and not old_loaded_matches:
            self.unload_voice()

        if apply_policy and not self.config.lazy_load:
            self.load_synthesis_target(force=True)
        return self.status()

    def unload_voice(self) -> dict[str, Any]:
        self.cancel_retention_timer()
        with self.load_lock:
            with self.synth_lock:
                self.clear_loaded_voice_locked("manual request")
        return self.status()

    def forget_voice(self, voice_id: str, model_path: Path, config_path: Path) -> dict[str, Any]:
        with self.load_lock:
            with self.synth_lock:
                if self._loaded_voice_matches(voice_id, model_path, config_path):
                    self.cancel_retention_timer()
                    self.clear_loaded_voice_locked("deleted voice")

            if self._configured_voice_matches(voice_id, model_path, config_path):
                self.config.voice = None
                self.config.model_path = None
                self.config.config_path = None
                self.config.speaker = None
                save_config(self.config, self.config_path)
            if self._synthesis_voice_matches(voice_id, model_path, config_path):
                self.synthesis_voice_id = None
                self.synthesis_model_path = None
                self.synthesis_config_path = None
                self.synthesis_speaker_id = None

        return self.status()

    def load_synthesis_target(self, force: bool = False) -> PiperVoice:
        target = self.synthesis_target()
        voice_id = target["voice"]
        model_path = _optional_path(target["model_path"])
        config_path = _optional_path(target["config_path"])
        if not voice_id or model_path is None or config_path is None:
            raise RuntimeError("no synthesis voice is configured")
        return self.load_voice(voice_id, model_path, config_path, target["speaker"], force=force)

    def has_synthesis_target(self) -> bool:
        target = self.synthesis_target_without_memory()
        return bool(target["voice"] and target["model_path"] and target["config_path"])

    def load_voice(
        self,
        voice_id: str | None = None,
        model_path: Path | None = None,
        config_path: Path | None = None,
        speaker: int | None = None,
        force: bool = False,
    ) -> PiperVoice:
        with self.load_lock:
            self.cancel_retention_timer()
            voice_id = voice_id or self.config.voice
            if not voice_id:
                raise RuntimeError("no default Piper voice is configured")
            model_path = model_path or _optional_path(self.config.model_path)
            config_path = config_path or _optional_path(self.config.config_path)
            if model_path is None or config_path is None:
                raise RuntimeError(f"no model path configured for voice: {voice_id}")
            if self._configured_voice_matches(voice_id, model_path, config_path):
                missing = self.missing_voice_files(model_path, config_path)
                if missing:
                    self.clear_configured_voice()
                    raise RuntimeError(f"configured voice files are missing: {', '.join(str(path) for path in missing)}")
            self._validate_voice_paths(model_path, config_path)
            if speaker is None and voice_id == self.config.voice:
                speaker = self.config.speaker

            if (
                self.voice is not None
                and self.loaded_voice_id == voice_id
                and self.loaded_model_path == model_path
                and self.loaded_config_path == config_path
                and not force
            ):
                self.loaded_speaker_id = speaker
                return self.voice

            if self.voice is not None:
                self.log_unload(f"replacing with {voice_id}")
            LOG.info("loading voice %s from %s", voice_id, model_path)
            loaded = load_piper_voice(model_path, config_path, self.config.execution_provider)
            self.voice = loaded
            self.loaded_voice_id = voice_id
            self.loaded_speaker_id = speaker
            self.loaded_model_path = model_path
            self.loaded_config_path = config_path
            self.loaded_execution_provider = self.config.execution_provider
            LOG.info("voice %s loaded", voice_id)
            return loaded

    def synthesize(self, text: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("text is empty")

        voice = self.voice if self.loaded_voice_matches_synthesis_target() else self.load_synthesis_target()
        cfg = self._synthesis_config(overrides or {})

        with self.synth_lock:
            chunks = list(voice.synthesize(text, cfg))

        if not chunks:
            raise RuntimeError("Piper returned no audio")
        self.schedule_retention_unload()

        sample_rate = chunks[0].sample_rate
        wav = pcm_to_wav(
            (chunk.audio_int16_bytes for chunk in chunks),
            sample_rate=sample_rate,
            sample_width=chunks[0].sample_width,
            channels=chunks[0].sample_channels,
        )

        return {
            "audio": wav,
            "sample_rate": sample_rate,
            "sample_width": chunks[0].sample_width,
            "sample_channels": chunks[0].sample_channels,
            "loaded_voice": self.loaded_voice_id,
        }

    def _synthesis_config(self, overrides: dict[str, Any]) -> SynthesisConfig:
        return SynthesisConfig(
            speaker_id=_optional_int(overrides.get("speaker", self.loaded_speaker_id if self.voice is not None else self.synthesis_target()["speaker"])),
            length_scale=_optional_float(overrides.get("length_scale", self.config.length_scale)),
            noise_scale=_optional_float(overrides.get("noise_scale", self.config.noise_scale)),
            noise_w_scale=_optional_float(overrides.get("noise_w_scale", self.config.noise_w_scale)),
            normalize_audio=bool(overrides.get("normalize_audio", self.config.normalize_audio)),
            volume=float(overrides.get("volume", self.config.volume)),
        )

    def schedule_retention_unload(self) -> None:
        self.cancel_retention_timer()
        retention = self.config.retention_seconds
        if retention is None:
            return
        if retention <= 0:
            with self.load_lock:
                with self.synth_lock:
                    self.clear_loaded_voice_locked("retention policy")
            return

        timer = threading.Timer(float(retention), self.unload_after_retention)
        timer.daemon = True
        self.retention_timer = timer
        timer.start()

    def cancel_retention_timer(self) -> None:
        timer = self.retention_timer
        self.retention_timer = None
        if timer is not None:
            timer.cancel()

    def unload_after_retention(self) -> None:
        with self.load_lock:
            with self.synth_lock:
                self.clear_loaded_voice_locked("retention timeout")
                self.retention_timer = None

    def clear_loaded_voice_locked(self, reason: str) -> None:
        self.log_unload(reason)
        self.voice = None
        self.loaded_voice_id = None
        self.loaded_speaker_id = None
        self.loaded_model_path = None
        self.loaded_config_path = None
        self.loaded_execution_provider = None

    def log_unload(self, reason: str) -> None:
        if self.voice is None:
            return
        LOG.info("unloading voice %s from memory: %s", self.loaded_voice_id or "<unknown>", reason)

    def synthesis_target(self) -> dict[str, Any]:
        if self.synthesis_voice_id and self.synthesis_model_path and self.synthesis_config_path:
            return {
                "voice": self.synthesis_voice_id,
                "speaker": self.synthesis_speaker_id,
                "model_path": str(self.synthesis_model_path),
                "config_path": str(self.synthesis_config_path),
                "in_memory": self.loaded_voice_matches_synthesis_target(),
            }

        return {
            "voice": self.config.voice,
            "speaker": self.config.speaker,
            "model_path": self.config.model_path,
            "config_path": self.config.config_path,
            "in_memory": self.loaded_voice_matches_configured_voice(),
        }

    def loaded_voice_matches_synthesis_target(self) -> bool:
        target = self.synthesis_target_without_memory()
        voice_id = target["voice"]
        model_path = _optional_path(target["model_path"])
        config_path = _optional_path(target["config_path"])
        if not voice_id or model_path is None or config_path is None:
            return False
        return self._loaded_voice_matches(voice_id, model_path, config_path)

    def loaded_voice_matches_configured_voice(self) -> bool:
        model_path = _optional_path(self.config.model_path)
        config_path = _optional_path(self.config.config_path)
        if not self.config.voice or model_path is None or config_path is None:
            return False
        return self._loaded_voice_matches(self.config.voice, model_path, config_path)

    def synthesis_target_without_memory(self) -> dict[str, Any]:
        if self.synthesis_voice_id and self.synthesis_model_path and self.synthesis_config_path:
            return {
                "voice": self.synthesis_voice_id,
                "speaker": self.synthesis_speaker_id,
                "model_path": str(self.synthesis_model_path),
                "config_path": str(self.synthesis_config_path),
            }
        return {
            "voice": self.config.voice,
            "speaker": self.config.speaker,
            "model_path": self.config.model_path,
            "config_path": self.config.config_path,
        }

    def _validate_voice_paths(self, model_path: Path, config_path: Path) -> None:
        missing = self.missing_voice_files(model_path, config_path)
        if missing:
            raise RuntimeError(f"voice files are missing: {', '.join(str(path) for path in missing)}")

    def clear_missing_configured_voice(self) -> None:
        model_path = _optional_path(self.config.model_path)
        config_path = _optional_path(self.config.config_path)
        if model_path is None and config_path is None:
            return

        missing = self.missing_voice_files(model_path, config_path)
        if missing:
            LOG.warning("clearing configured voice because files are missing: %s", ", ".join(str(path) for path in missing))
            self.clear_configured_voice()

    def clear_configured_voice(self) -> None:
        self.config.voice = None
        self.config.model_path = None
        self.config.config_path = None
        self.config.speaker = None
        save_config(self.config, self.config_path)

    def missing_voice_files(self, model_path: Path | None, config_path: Path | None) -> list[Path]:
        missing = []
        if model_path is None or not model_path.exists():
            missing.append(model_path or Path("<missing model path>"))
        if config_path is None or not config_path.exists():
            missing.append(config_path or Path("<missing config path>"))
        return missing

    def _configured_voice_matches(self, voice_id: str, model_path: Path, config_path: Path) -> bool:
        return self._voice_matches(
            self.config.voice,
            _optional_path(self.config.model_path),
            _optional_path(self.config.config_path),
            voice_id,
            model_path,
            config_path,
        )

    def _loaded_voice_matches(self, voice_id: str, model_path: Path, config_path: Path) -> bool:
        return self._voice_matches(
            self.loaded_voice_id,
            self.loaded_model_path,
            self.loaded_config_path,
            voice_id,
            model_path,
            config_path,
        )

    def _synthesis_voice_matches(self, voice_id: str, model_path: Path, config_path: Path) -> bool:
        return self._voice_matches(
            self.synthesis_voice_id,
            self.synthesis_model_path,
            self.synthesis_config_path,
            voice_id,
            model_path,
            config_path,
        )

    def _voice_matches(
        self,
        current_voice_id: str | None,
        current_model_path: Path | None,
        current_config_path: Path | None,
        voice_id: str,
        model_path: Path,
        config_path: Path,
    ) -> bool:
        if current_model_path is not None or current_config_path is not None:
            return current_model_path == model_path and current_config_path == config_path
        return current_voice_id == voice_id


class PiperRequestHandler(socketserver.StreamRequestHandler):
    server: "PiperServer"

    def handle(self) -> None:
        try:
            request = recv_json_line(self.rfile)
            response = self.dispatch(request)
            audio = response.pop("audio", None)
            send_json(self.request, {"ok": True, **response, "bytes": len(audio) if audio else 0})
            if audio:
                self.request.sendall(audio)
        except (BrokenPipeError, ConnectionResetError):
            LOG.debug("client disconnected before response was sent")
            return
        except Exception as exc:
            LOG.exception("request failed")
            try:
                send_json(self.request, {"ok": False, "error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                LOG.debug("client disconnected before error response was sent")
            return

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "status":
            return self.server.state.status()
        if action == "ensure_loaded":
            self.server.state.clear_missing_configured_voice()
            config = self.server.state.config
            if not config.lazy_load and self.server.state.voice is None and self.server.state.has_synthesis_target():
                self.server.state.load_synthesis_target()
            return self.server.state.status()
        if action == "set_settings":
            return self.server.state.update_settings(
                _optional_bool(request.get("lazy_load")),
                _optional_retention_seconds(request.get("retention_seconds")),
                _optional_str(request.get("execution_provider")),
                _optional_str(request.get("audio_output")) if "audio_output" in request else UNSET,
            )
        if action == "set_default":
            return self.server.state.set_default_voice(
                _required_str(request, "voice"),
                _required_path(request, "model_path"),
                _required_path(request, "config_path"),
                _optional_int(request.get("speaker")),
            )
        if action == "set_synthesis":
            return self.server.state.set_synthesis_voice(
                _required_str(request, "voice"),
                _required_path(request, "model_path"),
                _required_path(request, "config_path"),
                _optional_int(request.get("speaker")),
            )
        if action == "load_voice":
            voice_id = _required_str(request, "voice")
            speaker = _optional_int(request.get("speaker"))
            model_path = _required_path(request, "model_path")
            config_path = _required_path(request, "config_path")
            self.server.state.set_synthesis_voice(voice_id, model_path, config_path, speaker, apply_policy=False)
            self.server.state.load_voice(voice_id, model_path, config_path, speaker, force=True)
            return self.server.state.status()
        if action == "unload_voice":
            return self.server.state.unload_voice()
        if action == "forget_voice":
            return self.server.state.forget_voice(
                _required_str(request, "voice"),
                _required_path(request, "model_path"),
                _required_path(request, "config_path"),
            )
        if action == "synthesize":
            return self.server.state.synthesize(
                _required_str(request, "text"),
                request.get("synthesis") if isinstance(request.get("synthesis"), dict) else None,
            )

        raise ProtocolError(f"unknown action: {action}")


class PiperServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: str, state: PiperState) -> None:
        self.state = state
        super().__init__(server_address, PiperRequestHandler)


def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    path = args.socket
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    state = PiperState(args.config, args.socket)
    if args.preload and not state.config.lazy_load and state.has_synthesis_target():
        try:
            state.load_synthesis_target()
        except RuntimeError as exc:
            LOG.warning("starting without a loaded voice: %s", exc)

    server = PiperServer(str(path), state)
    os.chmod(path, 0o600)
    LOG.info("listening on %s", path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Pauper resident TTS daemon")
    parser.add_argument("--socket", type=Path, default=socket_path())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--no-preload",
        dest="preload",
        action="store_false",
        help="start listening before loading the configured voice",
    )
    parser.set_defaults(preload=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"pauperd: {exc}", file=sys.stderr)
        return 1
    return 0


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"missing string field: {key}")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProtocolError("expected string value")
    return value


def _required_path(payload: dict[str, Any], key: str) -> Path:
    return Path(_required_str(payload, key)).expanduser()


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_retention_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    seconds = int(value)
    if seconds < 0:
        return None
    return seconds


def available_execution_providers() -> list[str]:
    return list(onnxruntime.get_available_providers())


def recommended_execution_provider() -> str:
    provider = best_provider(available_execution_providers())
    if provider is None:
        raise RuntimeError("ONNX Runtime reports no available execution providers")
    return provider


def validate_execution_provider(provider: str) -> None:
    available = available_execution_providers()
    if provider not in available:
        raise RuntimeError(f"ONNX execution provider is not available: {provider}")


def provider_chain(provider: str) -> list[str]:
    validate_execution_provider(provider)
    return [provider]


def load_piper_voice(model_path: Path, config_path: Path, execution_provider: str) -> PiperVoice:
    providers = provider_chain(execution_provider)
    LOG.info("using ONNX execution providers: %s", ", ".join(providers))
    with config_path.open("r", encoding="utf-8") as config_file:
        config_dict = json.load(config_file)

    return PiperVoice(
        config=PiperConfig.from_dict(config_dict),
        session=onnxruntime.InferenceSession(
            str(model_path),
            sess_options=onnxruntime.SessionOptions(),
            providers=providers,
        ),
        espeak_data_dir=Path(ESPEAK_DATA_DIR),
        download_dir=Path.cwd(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
