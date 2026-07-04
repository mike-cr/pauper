# Pauper

Pauper is a resident Piper text-to-speech service for Linux phones and other
small Linux systems.

Piper normally has to load an ONNX voice model before it can synthesize speech.
That startup cost is noticeable when Speech Dispatcher asks for lots of short
utterances. Pauper keeps a selected voice ready behind a local user service, so
Speech Dispatcher and command-line clients can send text to the already-running
daemon.

The project includes:

- `pauperd`: the user daemon that owns the Piper model and Unix socket.
- `pauper-cli`: a CLI client and Speech Dispatcher bridge.
- `pauper-gtk`: a GTK/libadwaita manager designed for phone-sized screens.
- Debian packaging metadata for installing the service, desktop launcher,
  AppStream metadata, and Speech Dispatcher module config.

## What It Does

Pauper can:

- Download known Piper voices.
- Preview bundled voice samples before downloading a model.
- Select a default startup voice and speaker.
- Select the current synthesis voice separately from the startup default.
- Keep the current model loaded permanently, unload it immediately, or retain it
  for a configured idle time.
- Choose an ONNX Runtime execution provider from the providers available on the
  system.
- Choose a PulseAudio/PipeWire audio output device through `pactl`.
- Expose synthesis to Speech Dispatcher through its generic module interface.

The manager UI works even when the daemon is not running: it can still show the
voice catalog, play bundled samples, and download/delete local voice files.
Daemon-only actions become available once `pauperd` is reachable.

## Runtime Requirements

On Debian-style systems, Pauper expects these system packages:

- `ca-certificates`
- `gir1.2-adw-1`
- `gir1.2-gtk-4.0`
- `pipewire-bin`
- `pulseaudio-utils`
- `python3`
- `python3-gi`
- `python3-numpy`
- `python3-onnxruntime`
- `python3-pip`
- `speech-dispatcher`

The Debian package installs Piper's Python package privately under
`/usr/lib/pauper/python` if it is not already present there. ONNX Runtime is kept
as the system package so the daemon uses the execution providers supplied by the
distribution.

## Installed Commands

After installation, the main commands are:

```sh
pauper-gtk
pauperd
pauper-cli --help
```

Use `pauper-gtk` for normal phone use. Use `pauper-cli` when you want to test
or script the service.

Useful CLI commands:

```sh
pauper-cli status
pauper-cli version
pauper-cli models downloaded
pauper-cli models available
pauper-cli speak --play "Hello from Pauper."
pauper-cli unload
```

To set voices from the CLI:

```sh
pauper-cli set-default en_GB-cori-high --speaker 0
pauper-cli load en_GB-cori-high --speaker 0
```

`set-default` changes the voice loaded at daemon startup. `load` changes the
voice selected for synthesis now; whether it is immediately loaded into memory
depends on the daemon's lazy-loading and retention settings.

## Starting The Daemon

Pauper is intended to run as a user systemd service so it has access to the
user's audio session and `XDG_RUNTIME_DIR`.

The Debian package installs:

```text
/usr/lib/systemd/user/pauper.service
```

The package currently enables the user unit globally for future user sessions.
For the current session, start it with:

```sh
systemctl --user start pauper.service
```

Common service commands:

```sh
systemctl --user status pauper.service
systemctl --user enable --now pauper.service
journalctl --user -u pauper.service -f
```

From a source checkout, you can run the daemon directly:

```sh
python3 -m pauper.daemon --debug
```

Then run the GUI or CLI from another terminal:

```sh
python3 -m pauper.gui
python3 -m pauper.client status
```

## Speech Dispatcher

Pauper includes a Speech Dispatcher generic module config:

```text
data/speech-dispatcher/modules/pauper-generic.conf
```

The Debian package installs it to:

```text
/etc/speech-dispatcher/modules/pauper-generic.conf
```

It also installs an example Speech Dispatcher config snippet to:

```text
/usr/share/doc/pauper/examples/speechd-pauper.conf
```

It runs synthesis through:

```text
/usr/bin/pauper-cli speak --play
```

The package does not edit per-user Speech Dispatcher configuration. To enable
Pauper for your user, copy or merge the example into:

```text
~/.config/speech-dispatcher/speechd.conf
```

The relevant lines are:

```text
AddModule "pauper" "sd_generic" "pauper-generic.conf"
DefaultModule pauper
```

Restart Speech Dispatcher after changing its configuration.

The CLI helper can copy the module config into the current user's Speech
Dispatcher module directory, but it still does not edit `speechd.conf`.

From an installed package:

```sh
pauper-cli install-speechd-user
```

From a source checkout:

```sh
python3 -m pauper.client install-speechd-user
```

## Files And State

Pauper follows the XDG base directories.

User configuration:

```text
~/.config/pauper/config.toml
```

This stores settings such as:

- startup default voice, model path, config path, and speaker
- current voice directory
- ONNX execution provider
- lazy-loading mode
- retention time
- selected audio output
- synthesis tuning values such as volume and scale settings

Downloaded voices:

```text
~/.local/share/pauper/voices/
```

Runtime socket:

```text
$XDG_RUNTIME_DIR/pauper/socket
```

`XDG_RUNTIME_DIR` must be set. Pauper is expected to run in a normal user
session or user systemd service where that runtime directory exists.

Sample cache:

```text
~/.cache/pauper/
```

Most voice samples are bundled with the application so they can be played before
installing the full model.

## Debian Package Layout

The package installs these main files:

```text
/usr/bin/pauper-cli
/usr/bin/pauperd
/usr/bin/pauper-gtk
/usr/lib/pauper/python/
/usr/lib/systemd/user/pauper.service
/usr/lib/speech-dispatcher-modules/pauper
/etc/speech-dispatcher/modules/pauper-generic.conf
/usr/share/applications/io.github.mike_cr.Pauper.desktop
/usr/share/icons/hicolor/scalable/apps/io.github.mike_cr.Pauper.svg
/usr/share/metainfo/io.github.mike_cr.Pauper.metainfo.xml
```

`/usr/lib/pauper/python/` is the private Python target used for PyPI packages
that are not expected to come from apt, currently including Piper. It is removed
when the Debian package is removed or purged.

## Development From The Tree

The package entry points map to these modules:

```sh
python3 -m pauper.daemon
python3 -m pauper.client
python3 -m pauper.gui
```

The voice catalog is stored at:

```text
pauper/resources/voices.json
```

Bundled screenshots and AppStream metadata live under:

```text
data/screenshots/
data/metainfo/io.github.mike_cr.Pauper.metainfo.xml
```

The application ID is:

```text
io.github.mike_cr.Pauper
```
