from __future__ import annotations

from collections import defaultdict
import hashlib
from importlib import resources
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
from typing import Any
from urllib.parse import urlparse
import urllib.request

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import __version__
from .client import play_wav, request
from .config import load_config
from .config import update_config
from .paths import APP_ID, socket_path, xdg_cache_home
from .providers import best_provider, ranked_provider_info, ranked_provider_names
from .voices import delete_voice as delete_voice_file
from .voices import download_voice as download_voice_file
from .voices import is_downloaded_voice, list_installed, load_catalog, voice_paths
from .voices import speaker_count_for_voice_id


class SpeakerOption(GObject.GObject):
    def __init__(self, label: str, speaker_id: int | None = None, sample_index: int = -1) -> None:
        super().__init__()
        self.label = label
        self.speaker_id = speaker_id
        self.sample_index = sample_index

    @property
    def available(self) -> bool:
        return self.sample_index >= 0


class ManagerWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Pauper")
        self.set_default_size(300, 760)
        self.set_size_request(240, 360)
        self.voices: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self.sample_process: subprocess.Popen | None = None
        self.sample_voice_id: str | None = None
        self.sample_buttons: dict[str, Gtk.Button] = {}
        self.sample_dropdowns: dict[str, Gtk.DropDown] = {}
        self.speaker_selection: dict[str, int | None] = {}
        self.sample_selection: dict[str, int] = {}
        self.drag_scroll_start: float | None = None
        self.drag_scroll_active = False
        self.daemon_available = False
        self.language_keys: list[str] = []
        self.quality_keys: list[str] = []
        self.install_state_keys: list[str] = ["all", "installed", "available"]
        self.updating_filters = False
        self.updating_settings = False
        self.refresh_generation = 0
        self.status_poll_source_id: int | None = None
        self.preserve_voice_scroll_updates = 0
        self.ensure_loaded_on_connect = True
        self.retention_values: list[int | None] = [0, 60, 300, 900, 3600, None]
        self.retention_labels: list[str] = [
            "Unload immediately",
            "1 minute",
            "5 minutes",
            "15 minutes",
            "1 hour",
            "Keep loaded",
        ]
        self.provider_names: list[str] = ["CPUExecutionProvider"]
        self.provider_labels: list[str] = ["CPUExecutionProvider"]
        self.audio_output_values: list[str | None] = [None]
        self.audio_output_labels: list[str] = ["Default output"]

        self.set_decorated(False)

        self.stack = Gtk.Stack()
        self.set_content(self.stack)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(8)
        root.set_margin_end(8)
        self.stack.add_named(root, "voices")

        page_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_box.set_hexpand(True)
        title_box.set_halign(Gtk.Align.START)
        title_logo = self.app_logo_image(24)
        title_logo.set_valign(Gtk.Align.CENTER)
        title_box.append(title_logo)
        title_label = Gtk.Label(label="Pauper")
        title_label.add_css_class("title-2")
        title_box.append(title_label)
        header_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        about_button = Gtk.Button.new_from_icon_name("help-about-symbolic")
        about_button.set_tooltip_text("About Pauper")
        about_button.connect("clicked", lambda _button: self.show_about_page())
        settings_button = Gtk.Button.new_from_icon_name("preferences-system-symbolic")
        settings_button.set_tooltip_text("Settings")
        settings_button.connect("clicked", lambda _button: self.show_settings_page())
        header_buttons.append(about_button)
        header_buttons.append(settings_button)
        page_actions.append(title_box)
        page_actions.append(header_buttons)
        root.append(page_actions)

        state_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        state_panel.add_css_class("boxed-list")
        state_panel.set_margin_bottom(2)
        root.append(state_panel)

        default_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        default_name = Gtk.Label(label="Default", xalign=0)
        default_name.add_css_class("dim-label")
        default_name.set_width_chars(8)
        self.default_label = Gtk.Label(label="Unknown", xalign=0)
        self.default_label.set_hexpand(True)
        self.default_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        default_row.append(default_name)
        default_row.append(self.default_label)
        state_panel.append(default_row)

        synthesis_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        synthesis_name = Gtk.Label(label="Synthesis", xalign=0)
        synthesis_name.add_css_class("dim-label")
        synthesis_name.set_width_chars(8)
        self.synthesis_label = Gtk.Label(label="Unknown", xalign=0)
        self.synthesis_label.set_hexpand(True)
        self.synthesis_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        synthesis_row.append(synthesis_name)
        synthesis_row.append(self.synthesis_label)
        state_panel.append(synthesis_row)

        memory_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        memory_name = Gtk.Label(label="In memory", xalign=0)
        memory_name.add_css_class("dim-label")
        memory_name.set_width_chars(8)
        self.memory_label = Gtk.Label(label="Unknown", xalign=0)
        self.memory_label.set_hexpand(True)
        self.memory_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        memory_row.append(memory_name)
        memory_row.append(self.memory_label)
        state_panel.append(memory_row)

        memory_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        memory_actions.set_halign(Gtk.Align.START)
        self.unload_button = icon_label_button("media-eject-symbolic", "Unload")
        self.unload_button.set_tooltip_text("Unload voice")
        self.unload_button.connect("clicked", lambda _button: self.unload_voice())
        self.unload_button.set_sensitive(False)
        memory_actions.append(self.unload_button)
        state_panel.append(memory_actions)

        test_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.test_entry = Gtk.Entry()
        self.test_entry.set_hexpand(True)
        self.test_entry.set_width_chars(1)
        self.test_entry.set_placeholder_text("Test speech")
        self.test_entry.set_text("Hello from Pauper.")
        self.test_entry.connect("activate", lambda _entry: self.speak_test())
        test_box.append(self.test_entry)

        test_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        test_button.set_tooltip_text("Speak test phrase")
        test_button.add_css_class("suggested-action")
        test_button.connect("clicked", lambda _button: self.speak_test())
        test_box.append(test_button)
        root.append(test_box)

        filters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.language_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.language_dropdown)
        self.language_dropdown.connect("notify::selected", lambda _dropdown, _param: self.populate_list())
        filters.append(self.language_dropdown)

        self.quality_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.quality_dropdown)
        self.quality_dropdown.connect("notify::selected", lambda _dropdown, _param: self.populate_list())
        filters.append(self.quality_dropdown)

        self.install_state_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.install_state_dropdown)
        self.install_state_dropdown.set_model(
            Gtk.StringList.new(["Any download state", "Downloaded only", "Not downloaded"])
        )
        self.install_state_dropdown.connect("notify::selected", lambda _dropdown, _param: self.populate_list())
        filters.append(self.install_state_dropdown)

        root.append(filters)

        self.voices_scroller = Gtk.ScrolledWindow()
        self.voices_scroller.set_vexpand(True)
        self.voices_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.voices_scroller.set_kinetic_scrolling(True)
        self.voices_scroller.set_overlay_scrolling(True)
        self.voices_scroller.add_css_class("hidden-scrollbar")
        self.add_drag_scroll(self.voices_scroller)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.add_css_class("voice-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.voices_scroller.set_child(self.listbox)
        root.append(self.voices_scroller)

        top_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_actions.set_visible(False)
        self.top_actions = top_actions
        self.message_label = Gtk.Label(xalign=0)
        self.message_label.set_hexpand(True)
        self.message_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.message_label.set_max_width_chars(24)
        self.message_label.add_css_class("dim-label")
        top_actions.append(self.message_label)
        self.retry_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.retry_button.set_tooltip_text("Retry daemon connection")
        self.retry_button.connect("clicked", lambda _button: self.refresh())
        self.set_retry_visible(False)
        top_actions.append(self.retry_button)
        root.append(top_actions)

        self.stack.add_named(self.settings_page(), "settings")
        self.stack.add_named(self.about_page(), "about")
        self.refresh()

    def show_settings_page(self) -> None:
        self.stack.set_visible_child_name("settings")

    def show_about_page(self) -> None:
        self.stack.set_visible_child_name("about")

    def show_voices_page(self) -> None:
        self.stack.set_visible_child_name("voices")

    def app_logo_image(self, pixel_size: int = 112) -> Gtk.Image:
        icon_path = (
            Path(__file__).resolve().parent.parent
            / "data"
            / "icons"
            / "hicolor"
            / "scalable"
            / "apps"
            / f"{APP_ID}.svg"
        )
        if icon_path.exists():
            image = Gtk.Image.new_from_file(str(icon_path))
        else:
            image = Gtk.Image.new_from_icon_name(APP_ID)
        image.set_pixel_size(pixel_size)
        return image

    def add_drag_scroll(self, scroller: Gtk.ScrolledWindow) -> None:
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self.drag_scroll_begin)
        drag.connect("drag-update", self.drag_scroll_update)
        drag.connect("drag-end", self.drag_scroll_end)
        scroller.add_controller(drag)

    def drag_scroll_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        adjustment = self.voices_scroller.get_vadjustment()
        self.drag_scroll_start = adjustment.get_value()
        self.drag_scroll_active = False

    def drag_scroll_update(self, gesture: Gtk.GestureDrag, _offset_x: float, offset_y: float) -> None:
        if self.drag_scroll_start is None:
            return

        if abs(offset_y) > 6:
            self.drag_scroll_active = True
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        adjustment = self.voices_scroller.get_vadjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper() - adjustment.get_page_size()
        value = min(max(self.drag_scroll_start - offset_y, lower), max(lower, upper))
        adjustment.set_value(value)

    def drag_scroll_end(self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float) -> None:
        self.drag_scroll_start = None
        self.drag_scroll_active = False

    def settings_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(10)
        page.set_margin_bottom(10)
        page.set_margin_start(8)
        page.set_margin_end(8)

        settings_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_button.set_tooltip_text("Back to voices")
        back_button.connect("clicked", lambda _button: self.show_voices_page())
        settings_title = Gtk.Label(label="Settings")
        settings_title.add_css_class("title-2")
        settings_title.set_hexpand(True)
        settings_title.set_halign(Gtk.Align.CENTER)
        settings_spacer = Gtk.Box()
        settings_spacer.set_hexpand(True)
        settings_top.append(back_button)
        settings_top.append(settings_title)
        settings_top.append(settings_spacer)
        page.append(settings_top)

        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        group.add_css_class("boxed-list")
        group.set_halign(Gtk.Align.CENTER)
        group.set_size_request(180, -1)
        page.append(group)

        lazy_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lazy_row.set_halign(Gtk.Align.FILL)
        lazy_label = Gtk.Label(label="Load on demand", xalign=0)
        lazy_label.add_css_class("dim-label")
        self.lazy_switch = Gtk.Switch()
        self.lazy_switch.set_valign(Gtk.Align.CENTER)
        self.lazy_switch.connect("notify::active", lambda _switch, _param: self.settings_changed())
        self.lazy_value_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.lazy_value_box.set_halign(Gtk.Align.START)
        self.lazy_switch.set_halign(Gtk.Align.END)
        self.lazy_value_box.append(self.lazy_switch)
        lazy_row.append(lazy_label)
        lazy_row.append(self.lazy_value_box)
        group.append(lazy_row)

        retention_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        retention_row.set_halign(Gtk.Align.FILL)
        retention_label = Gtk.Label(label="Retain models", xalign=0)
        retention_label.add_css_class("dim-label")
        self.retention_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.retention_dropdown)
        self.retention_dropdown.set_model(Gtk.StringList.new(self.retention_labels))
        self.retention_dropdown.connect("notify::selected", lambda _dropdown, _param: self.settings_changed())
        retention_row.append(retention_label)
        retention_row.append(self.retention_dropdown)
        group.append(retention_row)

        output_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        output_row.set_halign(Gtk.Align.FILL)
        output_label = Gtk.Label(label="Audio output", xalign=0)
        output_label.add_css_class("dim-label")
        self.output_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.output_dropdown)
        self.output_dropdown.set_model(Gtk.StringList.new(self.audio_output_labels))
        self.output_dropdown.connect("notify::selected", lambda _dropdown, _param: self.settings_changed())
        output_row.append(output_label)
        output_row.append(self.output_dropdown)
        group.append(output_row)

        provider_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        provider_row.set_halign(Gtk.Align.FILL)
        provider_label = Gtk.Label(label="ONNX provider", xalign=0)
        provider_label.add_css_class("dim-label")
        self.provider_dropdown = Gtk.DropDown()
        configure_string_dropdown(self.provider_dropdown)
        self.provider_dropdown.set_model(Gtk.StringList.new(self.provider_labels))
        self.provider_dropdown.connect("notify::selected", lambda _dropdown, _param: self.provider_selection_changed())
        provider_row.append(provider_label)
        provider_row.append(self.provider_dropdown)
        group.append(provider_row)

        provider_info_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        provider_info_row.set_halign(Gtk.Align.FILL)
        self.provider_info_label = Gtk.Label(xalign=0)
        self.provider_info_label.add_css_class("dim-label")
        self.provider_info_label.set_wrap(True)
        self.provider_info_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.provider_info_label.set_width_chars(16)
        self.provider_info_label.set_max_width_chars(16)
        provider_info_row.append(self.provider_info_label)
        group.append(provider_info_row)

        return page

    def about_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(10)
        page.set_margin_bottom(10)
        page.set_margin_start(8)
        page.set_margin_end(8)

        about_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_button.set_tooltip_text("Back to voices")
        back_button.connect("clicked", lambda _button: self.show_voices_page())
        about_top.append(back_button)
        page.append(about_top)

        brand_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        brand_box.set_halign(Gtk.Align.CENTER)
        logo = self.app_logo_image()
        logo.set_halign(Gtk.Align.CENTER)
        brand_box.append(logo)
        app_name = Gtk.Label(label="Pauper")
        app_name.add_css_class("title-1")
        app_name.set_halign(Gtk.Align.CENTER)
        brand_box.append(app_name)
        page.append(brand_box)

        about_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        about_group.add_css_class("boxed-list")
        about_group.set_halign(Gtk.Align.CENTER)
        page.append(about_group)

        about_description = Gtk.Label(
            label="A resident Piper text-to-speech service with a phone-friendly manager for downloading voices, choosing speakers, and controlling daemon memory behavior.",
            xalign=0.5,
        )
        about_description.set_wrap(True)
        about_description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        about_description.set_justify(Gtk.Justification.CENTER)
        about_description.set_max_width_chars(34)
        about_group.append(about_description)

        about_details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        about_details.set_halign(Gtk.Align.CENTER)
        about_group.append(about_details)

        version_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        version_label = Gtk.Label(label="Version", xalign=0)
        version_label.add_css_class("dim-label")
        version_label.set_width_chars(8)
        version_value = Gtk.Label(label=__version__, xalign=0)
        version_value.set_width_chars(14)
        version_row.append(version_label)
        version_row.append(version_value)
        about_details.append(version_row)

        github_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        github_label = Gtk.Label(label="GitHub", xalign=0)
        github_label.add_css_class("dim-label")
        github_label.set_width_chars(8)
        github_link = Gtk.Label(xalign=0)
        github_link.set_markup('<a href="https://github.com/mike-cr/pauper">mike-cr/pauper</a>')
        github_link.set_halign(Gtk.Align.START)
        github_link.set_width_chars(14)
        github_link.connect("activate-link", self.open_link)
        github_row.append(github_label)
        github_row.append(github_link)
        about_details.append(github_row)

        return page

    def open_link(self, _label: Gtk.Label, uri: str) -> bool:
        Gtk.show_uri(self, uri, 0)
        return True

    def refresh(self) -> None:
        self.refresh_generation += 1
        generation = self.refresh_generation
        self.set_message("Connecting to pauperd...")
        self.set_retry_visible(False)
        self.run_background(lambda: self.load_state(generation))

    def load_state(self, generation: int) -> None:
        voices = local_voices()
        try:
            action = "ensure_loaded" if self.ensure_loaded_on_connect else "status"
            status, _ = gui_request({"action": action})
        except Exception as exc:
            status, voices = offline_state(voices)
            GLib.idle_add(self.apply_offline_state, generation, status, voices, friendly_error(str(exc)))
            return

        GLib.idle_add(self.apply_state, generation, status, voices)

    def apply_state(self, generation: int, status: dict[str, Any], voices: list[dict[str, Any]]) -> bool:
        if generation != self.refresh_generation:
            return GLib.SOURCE_REMOVE

        self.daemon_available = True
        self.status = dict(status)
        self.ensure_loaded_on_connect = False
        self.voices = voices
        self.update_state_labels()
        self.update_settings_controls()
        self.set_message("")
        self.set_retry_visible(False)
        self.schedule_status_poll()
        self.populate_filters()
        self.populate_list()
        return GLib.SOURCE_REMOVE

    def apply_offline_state(self, generation: int, status: dict[str, Any], voices: list[dict[str, Any]], error: str) -> bool:
        if generation != self.refresh_generation:
            return GLib.SOURCE_REMOVE

        self.daemon_available = False
        self.status = dict(status)
        self.voices = voices
        self.update_state_labels()
        self.update_settings_controls()
        self.set_message(f"Error: {error}")
        self.set_retry_visible(is_daemon_connection_error(error))
        self.cancel_status_poll()
        self.populate_filters()
        self.populate_list()
        return GLib.SOURCE_REMOVE

    def schedule_status_poll(self) -> None:
        if self.status_poll_source_id is not None:
            return
        self.status_poll_source_id = GLib.timeout_add_seconds(2, self.poll_daemon_status)

    def cancel_status_poll(self) -> None:
        if self.status_poll_source_id is None:
            return
        GLib.source_remove(self.status_poll_source_id)
        self.status_poll_source_id = None

    def poll_daemon_status(self) -> bool:
        if not self.daemon_available:
            self.status_poll_source_id = None
            return GLib.SOURCE_REMOVE

        self.run_background(self.load_daemon_status)
        return GLib.SOURCE_CONTINUE

    def load_daemon_status(self) -> None:
        try:
            status, _ = gui_request({"action": "status"})
        except Exception as exc:
            GLib.idle_add(self.apply_status_poll_error, friendly_error(str(exc)))
            return

        GLib.idle_add(self.apply_status_update, status)

    def apply_status_update(self, status: dict[str, Any]) -> bool:
        self.daemon_available = True
        if status == self.status:
            return GLib.SOURCE_REMOVE

        self.status = dict(status)
        self.update_state_labels()
        self.update_settings_controls()
        self.populate_list()
        return GLib.SOURCE_REMOVE

    def apply_status_poll_error(self, error: str) -> bool:
        self.daemon_available = False
        self.cancel_status_poll()
        self.status, _voices = offline_state(self.voices)
        self.set_message(f"Error: {error}")
        self.set_retry_visible(is_daemon_connection_error(error))
        self.update_state_labels()
        self.update_settings_controls()
        self.populate_list()
        return GLib.SOURCE_REMOVE

    def update_state_labels(self) -> None:
        synthesis = daemon_synthesis_label(self.status)
        loaded = daemon_loaded_label(self.status)
        configured = self.status.get("configured_voice") or "None"
        self.default_label.set_text(format_voice_with_speaker(configured, self.status.get("configured_speaker")))
        self.synthesis_label.set_text(format_voice_with_speaker(synthesis, self.status.get("synthesis_speaker")))
        self.memory_label.set_text(format_voice_with_speaker(loaded, self.status.get("loaded_speaker")))
        self.unload_button.set_sensitive(self.daemon_available and bool(self.status.get("loaded_voice") or self.status.get("loaded_model_path")))

    def update_settings_controls(self) -> None:
        self.updating_settings = True
        self.lazy_switch.set_active(bool(self.status.get("lazy_load")))
        retention = self.status.get("retention_seconds")
        try:
            selected = self.retention_values.index(retention if isinstance(retention, int) else None)
        except ValueError:
            selected = self.retention_values.index(None)
        self.retention_dropdown.set_selected(selected)

        configured_provider = self.status.get("execution_provider")
        provider_names = available_provider_names(self.status)
        if isinstance(configured_provider, str) and configured_provider and configured_provider not in provider_names:
            provider_names.insert(0, configured_provider)
        elif not isinstance(configured_provider, str) or not configured_provider:
            configured_provider = best_provider(provider_names)
        self.provider_names = provider_names
        self.provider_labels = provider_labels(self.status, provider_names)
        self.provider_dropdown.set_model(Gtk.StringList.new(self.provider_labels))
        output_items = audio_output_items()
        configured_output = self.status.get("audio_output")
        if isinstance(configured_output, str) and configured_output and configured_output not in [value for _label, value in output_items]:
            output_items.append((configured_output, configured_output))
        self.audio_output_labels = [label for label, _value in output_items]
        self.audio_output_values = [value for _label, value in output_items]
        self.output_dropdown.set_model(Gtk.StringList.new(self.audio_output_labels))
        self.update_settings_value_widths()
        self.provider_dropdown.set_selected(
            self.index_or_zero(
                provider_names,
                configured_provider if isinstance(configured_provider, str) else "",
            )
        )
        self.output_dropdown.set_selected(self.index_or_zero(self.audio_output_values, configured_output if isinstance(configured_output, str) else None))
        self.update_provider_info()
        self.updating_settings = False

    def update_settings_value_widths(self) -> None:
        width = value_width_pixels([*self.retention_labels, *self.provider_labels, *self.audio_output_labels, "Off"])
        for widget in (self.lazy_value_box, self.retention_dropdown, self.provider_dropdown, self.output_dropdown):
            widget.set_size_request(min(width, 118), -1)
        self.provider_info_label.set_size_request(min(width, 118), -1)

    def provider_selection_changed(self) -> None:
        if self.updating_settings:
            self.update_provider_info()
            return

        self.update_provider_info()
        self.settings_changed()

    def update_provider_info(self) -> None:
        provider = self.selected_provider()
        if not provider:
            self.provider_info_label.set_text("")
            return

        self.provider_info_label.set_text(provider_description(self.status, provider))

    def settings_changed(self) -> None:
        if self.updating_settings:
            return

        selected = self.retention_dropdown.get_selected()
        retention = None
        if selected != Gtk.INVALID_LIST_POSITION and selected < len(self.retention_values):
            retention = self.retention_values[selected]

        payload = {
            "action": "set_settings",
            "lazy_load": self.lazy_switch.get_active(),
            "retention_seconds": retention,
            "execution_provider": self.selected_provider(),
            "audio_output": self.selected_audio_output(),
        }
        self.set_message("Saving settings...")
        self.run_background(lambda: self.save_settings(payload))

    def save_settings(self, payload: dict[str, Any]) -> None:
        if self.daemon_available:
            response, _ = gui_request(payload)
            GLib.idle_add(self.apply_daemon_action_response, response)
        else:
            config = update_config({
                "lazy_load": payload["lazy_load"],
                "retention_seconds": payload["retention_seconds"],
                "execution_provider": payload["execution_provider"],
                "audio_output": payload["audio_output"],
            })
            self.status["lazy_load"] = config.lazy_load
            self.status["retention_seconds"] = config.retention_seconds
            self.status["execution_provider"] = config.execution_provider
            self.status["audio_output"] = config.audio_output
            local_providers = local_available_execution_providers()
            self.status["recommended_execution_provider"] = best_provider(local_providers)
            self.status["available_execution_providers"] = ranked_provider_names(local_providers)
            self.status["available_execution_provider_rankings"] = ranked_provider_info(local_providers)
            GLib.idle_add(self.clear_message)

    def clear_message(self) -> bool:
        self.set_message("")
        return GLib.SOURCE_REMOVE

    def populate_filters(self) -> None:
        self.updating_filters = True
        selected_language = self.selected_language()
        selected_quality = self.selected_quality()

        language_items = [("All languages", "")]
        seen_languages: set[str] = set()
        for voice in sorted(self.voices, key=self.voice_sort_key):
            key = str(voice.get("language") or "")
            if not key or key in seen_languages:
                continue
            seen_languages.add(key)
            language_items.append((self.language_label(voice), key))

        quality_items = [("All qualities", "")]
        for quality in sorted({str(voice.get("quality") or "") for voice in self.voices if voice.get("quality")}):
            quality_items.append((quality.replace("_", " "), quality))

        self.language_keys = [key for _label, key in language_items]
        self.quality_keys = [key for _label, key in quality_items]

        self.language_dropdown.set_model(Gtk.StringList.new([label for label, _key in language_items]))
        self.quality_dropdown.set_model(Gtk.StringList.new([label for label, _key in quality_items]))
        self.language_dropdown.set_selected(self.index_or_zero(self.language_keys, selected_language))
        self.quality_dropdown.set_selected(self.index_or_zero(self.quality_keys, selected_quality))
        self.updating_filters = False

    def populate_list(self) -> None:
        if self.updating_filters:
            return

        restore_scroll = self.preserve_voice_scroll_updates > 0
        scroll_value = self.voices_scroller.get_vadjustment().get_value() if restore_scroll else 0.0
        if restore_scroll:
            self.preserve_voice_scroll_updates -= 1

        self.sample_buttons.clear()
        self.sample_dropdowns.clear()
        while row := self.listbox.get_row_at_index(0):
            self.listbox.remove(row)

        selected_language = self.selected_language()
        selected_quality = self.selected_quality()
        selected_install_state = self.selected_install_state()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for voice in sorted(self.voices, key=self.voice_sort_key):
            if selected_language and voice.get("language") != selected_language:
                continue
            if selected_quality and voice.get("quality") != selected_quality:
                continue
            if selected_install_state == "installed" and not voice.get("installed"):
                continue
            if selected_install_state == "available" and voice.get("installed"):
                continue

            grouped[self.language_label(voice)].append(voice)

        if not grouped:
            self.listbox.append(self.empty_row())
            if restore_scroll:
                GLib.idle_add(self.restore_voice_scroll, scroll_value)
            return

        for language in sorted(grouped):
            self.listbox.append(self.heading_row(language, len(grouped[language])))
            for voice in grouped[language]:
                self.listbox.append(self.voice_row(voice))

        if restore_scroll:
            GLib.idle_add(self.restore_voice_scroll, scroll_value)

    def preserve_voice_scroll_for_next_updates(self, count: int = 1) -> None:
        self.preserve_voice_scroll_updates = max(self.preserve_voice_scroll_updates, count)

    def restore_voice_scroll(self, value: float) -> bool:
        adjustment = self.voices_scroller.get_vadjustment()
        lower = adjustment.get_lower()
        upper = max(lower, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(min(max(value, lower), upper))
        return GLib.SOURCE_REMOVE

    def selected_language(self) -> str:
        selected = self.language_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.language_keys):
            return ""
        return self.language_keys[selected]

    def selected_quality(self) -> str:
        selected = self.quality_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.quality_keys):
            return ""
        return self.quality_keys[selected]

    def selected_install_state(self) -> str:
        selected = self.install_state_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.install_state_keys):
            return "all"
        return self.install_state_keys[selected]

    def selected_provider(self) -> str:
        selected = self.provider_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.provider_names):
            return "CPUExecutionProvider"
        return self.provider_names[selected]

    def selected_audio_output(self) -> str | None:
        selected = self.output_dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.audio_output_values):
            return None
        return self.audio_output_values[selected]

    def index_or_zero(self, values: list[Any], value: Any) -> int:
        try:
            return values.index(value)
        except ValueError:
            return 0

    def empty_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        label = Gtk.Label(label="No voices match these filters", xalign=0)
        label.add_css_class("dim-label")
        label.set_margin_top(16)
        label.set_margin_bottom(16)
        label.set_margin_start(12)
        label.set_margin_end(12)
        row.set_child(label)
        return row

    def heading_row(self, language: str, count: int) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        row.add_css_class("header")
        row.add_css_class("voice-header")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(14)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        row.set_child(box)

        title = Gtk.Label(label=language, xalign=0)
        title.set_hexpand(True)
        title.add_css_class("heading")
        make_shrinking_label(title, 14)
        box.append(title)

        badge = Gtk.Label(label=str(count))
        badge.add_css_class("dim-label")
        box.append(badge)
        return row

    def voice_row(self, voice: dict[str, Any]) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("voice-card")
        row.set_selectable(False)
        row.set_activatable(False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        row.set_child(outer)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        title = Gtk.Label(label=self.voice_title(voice), xalign=0)
        make_shrinking_label(title, 18)
        title.add_css_class("heading")
        voice_id_label = Gtk.Label(label=str(voice.get("id", "")), xalign=0)
        make_shrinking_label(voice_id_label, 20)
        voice_id_label.add_css_class("dim-label")
        status = Gtk.Label(label=self.voice_status_text(voice), xalign=0)
        make_shrinking_label(status, 18)
        status.add_css_class("dim-label")
        details = Gtk.Label(label=self.voice_detail_text(voice), xalign=0)
        make_shrinking_label(details, 18)
        details.add_css_class("dim-label")
        text_box.append(title)
        text_box.append(voice_id_label)
        text_box.append(status)
        text_box.append(details)
        outer.append(text_box)

        samples = self.voice_samples(voice)
        speaker_options = self.speaker_options(voice)
        voice_id = voice.get("id")
        if len(speaker_options) > 1 and isinstance(voice_id, str):
            speaker_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            speaker_label = Gtk.Label(label="Speaker", xalign=0)
            speaker_label.add_css_class("dim-label")
            speaker_row.append(speaker_label)
            dropdown = Gtk.DropDown()
            dropdown.set_hexpand(True)
            dropdown.set_model(speaker_option_model(speaker_options))
            dropdown.set_factory(speaker_option_factory())
            dropdown.set_list_factory(speaker_option_factory())
            dropdown.set_selected(self.option_index_for_sample(voice_id, speaker_options))
            dropdown.connect("notify::selected", lambda widget, _param, item=voice: self.sample_selected(item, widget))
            self.sample_dropdowns[voice_id] = dropdown
            speaker_row.append(dropdown)
            outer.append(speaker_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.append(controls)

        if samples or speaker_options:
            icon = (
                "media-playback-stop-symbolic"
                if voice.get("id") == self.sample_voice_id
                else "media-playback-start-symbolic"
            )
            sample_button = icon_label_button(icon, "Play")
            sample_button.connect("clicked", lambda _button, item=voice: self.play_sample(item))
            if isinstance(voice.get("id"), str):
                self.sample_buttons[voice["id"]] = sample_button
            self.update_sample_button_for_voice(voice)
            controls.append(sample_button)

        if voice.get("installed"):
            if self.voice_matches_status(voice, "configured"):
                default_button = icon_label_button("object-select-symbolic", "Default")
                default_button.set_sensitive(False)
                default_button.set_tooltip_text("Default voice")
            else:
                default_button = icon_label_button("starred-symbolic", "Set default")
                default_button.set_tooltip_text("Set as default")
                default_button.connect("clicked", lambda _button, item=voice: self.set_default_voice(item))
            controls.append(default_button)

            if self.voice_matches_status(voice, "synthesis"):
                button = icon_label_button("object-select-symbolic", "Selected")
                button.set_sensitive(False)
                button.set_tooltip_text("Selected for synthesis")
            else:
                button = icon_label_button("send-to-symbolic", "Use")
                button.set_tooltip_text("Use for synthesis")
                button.connect("clicked", lambda _button, item=voice: self.load_voice(item))
            default_button.set_sensitive(default_button.get_sensitive() and self.daemon_available)
            button.set_sensitive(button.get_sensitive() and self.daemon_available)
            controls.append(button)
            if voice.get("deletable"):
                delete_button = icon_label_button("user-trash-symbolic", "Delete")
                delete_button.set_tooltip_text("Delete downloaded voice")
                delete_button.add_css_class("destructive-action")
                delete_button.connect("clicked", lambda _button, item=voice: self.delete_voice(item))
                controls.append(delete_button)
        else:
            button = icon_label_button("folder-download-symbolic", "Download")
            button.set_tooltip_text("Download voice")
            button.connect("clicked", lambda _button, voice_id=voice["id"]: self.download_voice(voice_id))
            controls.append(button)
        return row

    def voice_samples(self, voice: dict[str, Any]) -> list[dict[str, Any]]:
        samples = voice.get("samples")
        if isinstance(samples, list) and samples:
            return [sample for sample in samples if isinstance(sample, dict)]

        sample_path = voice.get("sample_path")
        sample_url = voice.get("sample_url")
        if sample_path or sample_url:
            return [{"label": "Sample", "path": sample_path, "url": sample_url, "speaker_id": None}]

        return []

    def speaker_options(self, voice: dict[str, Any]) -> list[SpeakerOption]:
        samples = self.voice_samples(voice)
        sample_by_speaker = {
            sample.get("speaker_id"): index
            for index, sample in enumerate(samples)
            if isinstance(sample.get("speaker_id"), int)
        }

        speaker_map = voice.get("speaker_id_map")
        if isinstance(speaker_map, dict) and speaker_map:
            return [
                SpeakerOption(f"{name} ({speaker_id})", speaker_id, sample_by_speaker.get(speaker_id, -1))
                for name, speaker_id in sorted(speaker_map.items(), key=lambda item: item[1])
                if isinstance(speaker_id, int)
            ]

        count = voice.get("num_speakers")
        if isinstance(count, int) and count > 1:
            return [
                SpeakerOption(f"Speaker {speaker_id}", speaker_id, sample_by_speaker.get(speaker_id, -1))
                for speaker_id in range(count)
            ]

        return [
            SpeakerOption(str(sample.get("label") or f"Sample {index + 1}"), sample.get("speaker_id") if isinstance(sample.get("speaker_id"), int) else None, index)
            for index, sample in enumerate(samples)
        ]

    def option_index_for_sample(self, voice_id: str, options: list[SpeakerOption]) -> int:
        status_speaker = None
        if voice_id == self.status.get("loaded_voice"):
            status_speaker = self.status.get("loaded_speaker")
        elif voice_id == self.status.get("configured_voice"):
            status_speaker = self.status.get("configured_speaker")
        if isinstance(status_speaker, int) and voice_id not in self.speaker_selection:
            for index, option in enumerate(options):
                if option.speaker_id == status_speaker:
                    return index

        if voice_id in self.speaker_selection:
            selected_speaker = self.speaker_selection[voice_id]
            for index, option in enumerate(options):
                if option.speaker_id == selected_speaker:
                    return index

        selected_sample = self.sample_selection.get(voice_id, 0)
        for index, option in enumerate(options):
            if option.sample_index == selected_sample:
                return index
        return 0

    def sample_selected(self, voice: dict[str, Any], dropdown: Gtk.DropDown) -> None:
        voice_id = voice.get("id")
        option = dropdown.get_selected_item()
        if not isinstance(voice_id, str) or not isinstance(option, SpeakerOption):
            return

        self.speaker_selection[voice_id] = option.speaker_id
        if option.available:
            self.sample_selection[voice_id] = option.sample_index
        else:
            self.sample_selection.pop(voice_id, None)
        self.update_sample_button_for_voice(voice)

    def language_label(self, voice: dict[str, Any]) -> str:
        name = voice.get("language_name") or voice.get("language") or "Unknown"
        code = voice.get("language")
        if code and code not in str(name):
            return f"{name} ({code})"
        return str(name)

    def voice_title(self, voice: dict[str, Any]) -> str:
        speaker = str(voice.get("name") or voice.get("id") or "Voice")
        quality = str(voice.get("quality") or "").replace("_", " ")
        return f"{speaker} - {quality}" if quality else speaker

    def voice_status_text(self, voice: dict[str, Any]) -> str:
        states = []
        if self.voice_matches_status(voice, "synthesis"):
            states.append("Will synthesize")
        if self.voice_matches_status(voice, "configured"):
            states.append("Default")
        if self.voice_matches_status(voice, "loaded"):
            states.append("In memory")
        states.append("Downloaded" if voice.get("installed") else "Not downloaded")
        return " / ".join(states)

    def voice_matches_status(self, voice: dict[str, Any], prefix: str) -> bool:
        voice_id = voice.get("id")
        status_voice = self.status.get(f"{prefix}_voice")
        if isinstance(voice_id, str) and voice_id and voice_id == status_voice:
            return True

        voice_model_path = voice.get("model_path")
        status_model_path = self.status.get(f"{prefix}_model_path")
        if isinstance(voice_model_path, str) and isinstance(status_model_path, str):
            return Path(voice_model_path).expanduser() == Path(status_model_path).expanduser()

        return False

    def voice_detail_text(self, voice: dict[str, Any]) -> str:
        details = []
        speakers = speaker_count(voice)
        details.append(f"{speakers} speaker" if speakers == 1 else f"{speakers} speakers")
        size = format_size(voice.get("model_size_bytes"))
        if size:
            details.append(f"Model: {size}")
        return " / ".join(details)

    def voice_sort_key(self, voice: dict[str, Any]) -> tuple[str, str, str]:
        return (
            self.language_label(voice).lower(),
            str(voice.get("name") or "").lower(),
            str(voice.get("quality") or "").lower(),
        )

    def set_default_voice(self, voice: dict[str, Any]) -> None:
        voice_id = str(voice.get("id"))
        speaker = self.selected_speaker_id(voice)
        self.set_message(f"Setting default to {voice_id}...")
        self.run_background(lambda: self.call_then_refresh(self.voice_daemon_payload("set_default", voice, speaker)))

    def load_voice(self, voice: dict[str, Any]) -> None:
        voice_id = str(voice.get("id"))
        speaker = self.selected_speaker_id(voice)
        self.set_message(f"Selecting {voice_id} for synthesis...")
        self.run_background(lambda: self.call_then_refresh(self.voice_daemon_payload("set_synthesis", voice, speaker)))

    def voice_daemon_payload(self, action: str, voice: dict[str, Any], speaker: int | None) -> dict[str, Any]:
        voice_id = voice.get("id")
        model_path = voice.get("model_path")
        config_path = voice.get("config_path")
        if not isinstance(voice_id, str) or not voice_id:
            raise RuntimeError("voice is missing an id")
        if not isinstance(model_path, str) or not model_path:
            raise RuntimeError(f"voice is missing a model path: {voice_id}")
        if not isinstance(config_path, str) or not config_path:
            raise RuntimeError(f"voice is missing a config path: {voice_id}")

        return {
            "action": action,
            "voice": voice_id,
            "model_path": model_path,
            "config_path": config_path,
            "speaker": speaker,
        }

    def download_voice(self, voice_id: str) -> None:
        self.set_message(f"Downloading {voice_id}...")
        self.run_background(lambda: self.download_voice_locally(voice_id))

    def download_voice_locally(self, voice_id: str) -> None:
        voice = download_voice_file(voice_id).to_dict()
        voice["installed"] = True
        voice["deletable"] = True
        GLib.idle_add(self.mark_voice_downloaded, voice)
        GLib.idle_add(self.refresh)

    def delete_voice(self, voice: dict[str, Any]) -> None:
        voice_id = voice.get("id")
        if not isinstance(voice_id, str):
            return

        dialog = Adw.MessageDialog.new(
            self,
            "Delete voice?",
            f"Delete {self.voice_title(voice)} and remove its downloaded model files?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response, item=voice: self.delete_voice_response(response, item))
        dialog.present()

    def delete_voice_response(self, response: str, voice: dict[str, Any]) -> None:
        if response != "delete":
            return

        voice_id = voice.get("id")
        if not isinstance(voice_id, str):
            return

        self.set_message(f"Deleting {voice_id}...")
        self.run_background(lambda: self.delete_voice_locally(voice))

    def delete_voice_locally(self, voice: dict[str, Any]) -> None:
        voice_id = str(voice.get("id"))
        if self.daemon_available and (
            self.voice_matches_status(voice, "configured")
            or self.voice_matches_status(voice, "synthesis")
            or self.voice_matches_status(voice, "loaded")
        ):
            gui_request(self.voice_daemon_payload("forget_voice", voice, None))
        else:
            forget_local_default_if_matches(voice)

        deleted = delete_voice_file(voice_id).to_dict()
        GLib.idle_add(self.mark_voice_deleted, deleted)
        GLib.idle_add(self.refresh)

    def unload_voice(self) -> None:
        self.set_message("Unloading voice...")
        self.run_background(lambda: self.call_then_refresh({"action": "unload_voice"}))

    def speak_test(self) -> None:
        text = self.test_entry.get_text()
        self.run_background(lambda: self.synthesize_and_play(text))

    def play_sample(self, voice: dict[str, Any]) -> None:
        voice_id = voice.get("id")
        if not isinstance(voice_id, str):
            return

        if self.sample_voice_id == voice_id and self.sample_process is not None:
            self.stop_sample()
            return

        self.stop_sample()
        self.set_message(f"Playing sample for {voice_id}")
        self.run_background(lambda: self.start_sample_playback(voice))

    def synthesize_and_play(self, text: str) -> None:
        _header, audio = gui_request({"action": "synthesize", "text": text})
        self.load_daemon_status()
        play_wav(audio, self.selected_audio_output())

    def start_sample_playback(self, voice: dict[str, Any]) -> None:
        sample_info = self.selected_sample(voice)
        if sample_info is None:
            GLib.idle_add(self.show_error, "No bundled sample for the selected speaker")
            return
        sample = bundled_sample_for_voice(voice, sample_info)
        if sample is None:
            sample = self.download_sample(voice, sample_info)

        command = audio_player_command(sample, self.selected_audio_output())
        process = subprocess.Popen(command)
        voice_id = str(voice["id"])
        GLib.idle_add(self.sample_started, voice_id, process)
        return_code = process.wait()
        GLib.idle_add(self.sample_finished, voice_id, process, return_code)

    def selected_sample(self, voice: dict[str, Any]) -> dict[str, Any] | None:
        voice_id = voice.get("id")
        samples = self.voice_samples(voice)
        if not samples:
            return None

        if isinstance(voice_id, str) and voice_id in self.speaker_selection:
            selected_speaker = self.speaker_selection[voice_id]
            for sample in samples:
                if sample.get("speaker_id") == selected_speaker:
                    return sample
            return None

        selected = self.sample_selection.get(str(voice_id), 0)
        return samples[min(selected, len(samples) - 1)]

    def selected_speaker_id(self, voice: dict[str, Any]) -> int | None:
        voice_id = voice.get("id")
        if isinstance(voice_id, str) and voice_id in self.speaker_selection:
            return self.speaker_selection[voice_id]

        sample = self.selected_sample(voice)
        if sample and isinstance(sample.get("speaker_id"), int):
            return sample["speaker_id"]

        options = self.speaker_options(voice)
        if options:
            return options[0].speaker_id

        return None

    def download_sample(self, voice: dict[str, Any], sample: dict[str, Any] | None) -> Path:
        url = resolve_sample_url(voice, sample)

        sample_path = sample_cache_path(url)
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        if not sample_path.exists():
            tmp_path = sample_path.with_suffix(sample_path.suffix + ".part")
            download_url(url, tmp_path)
            tmp_path.replace(sample_path)

        return sample_path

    def sample_started(self, voice_id: str, process: subprocess.Popen) -> bool:
        self.sample_process = process
        self.sample_voice_id = voice_id
        self.update_sample_buttons()
        return GLib.SOURCE_REMOVE

    def sample_finished(self, voice_id: str, process: subprocess.Popen, return_code: int) -> bool:
        if self.sample_process is process:
            self.sample_process = None
            self.sample_voice_id = None
            self.update_sample_buttons()
            if return_code == 0:
                self.set_message("")
        return GLib.SOURCE_REMOVE

    def stop_sample(self) -> None:
        process = self.sample_process
        if process is not None and process.poll() is None:
            process.terminate()
        self.sample_process = None
        self.sample_voice_id = None
        self.update_sample_buttons()
        self.set_message("")

    def update_sample_buttons(self) -> None:
        for voice_id, button in self.sample_buttons.items():
            voice = self.voice_by_id(voice_id)
            if voice is None:
                continue
            self.update_sample_button_for_voice(voice)

    def update_sample_button_for_voice(self, voice: dict[str, Any]) -> None:
        voice_id = voice.get("id")
        if not isinstance(voice_id, str):
            return

        button = self.sample_buttons.get(voice_id)
        if button is None:
            return

        if voice_id == self.sample_voice_id:
            set_button_content(button, "media-playback-stop-symbolic", "Stop")
            button.set_sensitive(True)
            button.set_tooltip_text("Stop sample")
            return

        has_sample = self.selected_sample(voice) is not None
        set_button_content(button, "media-playback-start-symbolic", "Play")
        button.set_sensitive(has_sample)
        button.set_tooltip_text("Play sample" if has_sample else "No sample for selected speaker")

    def voice_by_id(self, voice_id: str) -> dict[str, Any] | None:
        for voice in self.voices:
            if voice.get("id") == voice_id:
                return voice
        return None

    def call_then_refresh(self, payload: dict[str, Any]) -> None:
        response, _ = gui_request(payload)
        if "configured_voice" in response:
            GLib.idle_add(self.apply_daemon_action_response, response)
        else:
            GLib.idle_add(self.refresh)

    def apply_daemon_action_response(self, status: dict[str, Any]) -> bool:
        self.daemon_available = True
        self.status = dict(status)
        self.update_state_labels()
        self.update_settings_controls()
        self.set_message("")
        self.set_retry_visible(False)
        self.schedule_status_poll()
        self.populate_list()
        return GLib.SOURCE_REMOVE

    def mark_voice_downloaded(self, downloaded: dict[str, Any]) -> bool:
        voice_id = downloaded.get("id")
        if isinstance(voice_id, str):
            self.preserve_voice_scroll_for_next_updates(2)
            for voice in self.voices:
                if voice.get("id") == voice_id:
                    voice.update(downloaded)
                    voice["installed"] = True
                    break
            self.set_message("")
            self.populate_list()
        return GLib.SOURCE_REMOVE

    def mark_voice_deleted(self, deleted: dict[str, Any]) -> bool:
        voice_id = deleted.get("id")
        if isinstance(voice_id, str):
            for voice in self.voices:
                if voice.get("id") == voice_id:
                    voice["installed"] = False
                    voice["deletable"] = False
                    voice["model_path"] = deleted.get("model_path")
                    voice["config_path"] = deleted.get("config_path")
                    break
            self.set_message("")
            self.populate_list()
        return GLib.SOURCE_REMOVE

    def run_background(self, target) -> None:
        def wrapper() -> None:
            try:
                target()
            except Exception as exc:
                GLib.idle_add(self.show_error, str(exc))

        threading.Thread(target=wrapper, daemon=True).start()

    def show_error(self, message: str) -> bool:
        friendly = friendly_error(message)
        self.set_message(f"Error: {friendly}")
        self.set_retry_visible(is_daemon_connection_error(friendly))
        return GLib.SOURCE_REMOVE

    def set_message(self, message: str) -> None:
        self.message_label.set_text(message)
        self.top_actions.set_visible(bool(message) or self.retry_button.get_visible())

    def set_retry_visible(self, visible: bool) -> None:
        self.retry_button.set_visible(visible)
        self.top_actions.set_visible(bool(self.message_label.get_text()) or visible)


class ManagerApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self.on_activate)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            scrolledwindow.hidden-scrollbar scrollbar {
              min-width: 0;
              min-height: 0;
              opacity: 0;
            }
            list.voice-list row.voice-card:hover,
            list.voice-list row.voice-card:selected,
            list.voice-list row.voice-card:selected:hover,
            list.voice-list row.voice-header:hover,
            list.voice-list row.voice-header:selected,
            list.voice-list row.voice-header:selected:hover {
              background: none;
              box-shadow: none;
            }
            dropdown label,
            button label,
            entry,
            flowbox,
            row,
            list {
              min-width: 0;
            }
            button {
              min-width: 0;
            }
            """
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def on_activate(self, app: Adw.Application) -> None:
        window = self.props.active_window
        if window is None:
            window = ManagerWindow(app)
        window.present()


def main() -> int:
    app = ManagerApp()
    return app.run(None)


def gui_request(payload: dict[str, Any]):
    try:
        return request(payload, socket_path(), timeout=gui_request_timeout(payload))
    except FileNotFoundError as exc:
        raise RuntimeError("pauperd is not running") from exc
    except ConnectionRefusedError as exc:
        raise RuntimeError("pauperd is not accepting connections") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError("pauperd did not respond") from exc
    except OSError as exc:
        if "Operation not permitted" in str(exc):
            raise RuntimeError("cannot connect to pauperd from this environment") from exc
        raise RuntimeError(str(exc)) from exc

def gui_request_timeout(payload: dict[str, Any]) -> float:
    action = payload.get("action")
    if action in {"ensure_loaded", "forget_voice", "load_voice", "set_settings", "set_synthesis"}:
        return 120.0
    if action == "synthesize":
        return 120.0
    return 10.0


def friendly_error(message: str) -> str:
    if "No such file or directory" in message or "Errno 2" in message:
        return "pauperd is not running"
    if "Connection refused" in message:
        return "pauperd is not accepting connections"
    if "Operation not permitted" in message:
        return "cannot connect to pauperd from this environment"
    return message


def local_voices() -> list[dict[str, Any]]:
    installed = {voice.id: voice for voice in list_installed()}
    voices = []
    known_ids: set[str] = set()
    for voice in load_catalog():
        data = voice.to_dict()
        local = installed.get(voice.id)
        downloaded = is_downloaded_voice(voice.id)
        if downloaded:
            model_path, config_path = voice_paths(voice.id)
            data["model_path"] = str(model_path)
            data["config_path"] = str(config_path)
            data["installed"] = True
            data["deletable"] = True
        elif local and local.model_path and local.config_path:
            data["model_path"] = str(local.model_path)
            data["config_path"] = str(local.config_path)
            data["installed"] = True
        else:
            data["installed"] = False
        data.setdefault("deletable", False)
        voices.append(data)
        known_ids.add(voice.id)

    for voice_id, voice in installed.items():
        if voice_id not in known_ids:
            data = voice.to_dict()
            data["deletable"] = is_downloaded_voice(voice_id)
            voices.append(data)

    return enrich_voices_with_bundled_catalog(voices)


def offline_state(voices: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = load_config()
    local_providers = local_available_execution_providers()
    recommended_provider = best_provider(local_providers)
    execution_provider = config.execution_provider or recommended_provider

    return (
        {
            "configured_voice": config.voice,
            "configured_speaker": config.speaker,
            "configured_model_path": config.model_path,
            "configured_config_path": config.config_path,
            "synthesis_voice": config.voice,
            "synthesis_speaker": config.speaker,
            "synthesis_model_path": config.model_path,
            "synthesis_config_path": config.config_path,
            "synthesis_in_memory": False,
            "loaded_voice": None,
            "loaded_speaker": None,
            "loaded_model_path": None,
            "loaded_config_path": None,
            "loaded_execution_provider": None,
            "ready": False,
            "lazy_load": config.lazy_load,
            "retention_seconds": config.retention_seconds,
            "execution_provider": execution_provider,
            "audio_output": config.audio_output,
            "recommended_execution_provider": recommended_provider,
            "available_execution_providers": ranked_provider_names(local_providers),
            "available_execution_provider_rankings": ranked_provider_info(local_providers),
        },
        voices if voices is not None else local_voices(),
    )


def daemon_loaded_label(status: dict[str, Any]) -> str:
    voice_id = status.get("loaded_voice")
    if isinstance(voice_id, str) and voice_id:
        return voice_id

    model_path = status.get("loaded_model_path")
    if isinstance(model_path, str) and model_path:
        return Path(model_path).name

    return "None"


def daemon_synthesis_label(status: dict[str, Any]) -> str:
    voice_id = status.get("synthesis_voice")
    if isinstance(voice_id, str) and voice_id:
        return voice_id

    model_path = status.get("synthesis_model_path")
    if isinstance(model_path, str) and model_path:
        return Path(model_path).name

    return "None"


def available_provider_names(status: dict[str, Any]) -> list[str]:
    providers = status.get("available_execution_providers")
    if isinstance(providers, list):
        names = [provider for provider in providers if isinstance(provider, str) and provider]
        if names:
            return ranked_provider_names(names)
    return ranked_provider_names(local_available_execution_providers())


def provider_labels(status: dict[str, Any], names: list[str]) -> list[str]:
    ranking_by_name = {
        str(entry.get("name")): entry
        for entry in status.get("available_execution_provider_rankings", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    if not ranking_by_name:
        ranking_by_name = {str(entry["name"]): entry for entry in ranked_provider_info(names)}

    labels = []
    for index, name in enumerate(names, start=1):
        entry = ranking_by_name.get(name, {"label": name, "score": 0, "tier": "Unknown"})
        labels.append(f"{index}. {entry['label']}")
    return labels


def provider_description(status: dict[str, Any], provider: str) -> str:
    rankings = provider_rankings_for_status(status)
    ranking_by_name = {
        str(entry.get("name")): (index, entry)
        for index, entry in enumerate(rankings, start=1)
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    rank, entry = ranking_by_name.get(provider, (0, {"label": provider, "score": 0, "tier": "Unknown", "notes": ""}))
    rank_text = f"Rank {rank} of {len(rankings)}" if rank else "Rank unknown"
    return (
        f"{entry.get('label', provider)}\n"
        f"{rank_text} · score {entry.get('score', 0)} · {entry.get('tier', 'Unknown')}\n"
        f"{entry.get('notes', '')}"
    )


def provider_rankings_for_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    rankings = status.get("available_execution_provider_rankings")
    if isinstance(rankings, list) and rankings:
        return [entry for entry in rankings if isinstance(entry, dict)]

    names = available_provider_names(status)
    return ranked_provider_info(names)


def local_available_execution_providers() -> list[str]:
    try:
        import onnxruntime
    except Exception:
        return []

    try:
        return [provider for provider in onnxruntime.get_available_providers() if isinstance(provider, str)]
    except Exception:
        return []


def forget_local_default_if_matches(voice: dict[str, Any]) -> None:
    config = load_config()
    if not status_like_voice_matches(
        {
            "configured_voice": config.voice,
            "configured_model_path": config.model_path,
            "configured_config_path": config.config_path,
        },
        voice,
        "configured",
    ):
        return

    update_config({"voice": None, "model_path": None, "config_path": None, "speaker": None})


def status_like_voice_matches(status: dict[str, Any], voice: dict[str, Any], prefix: str) -> bool:
    voice_id = voice.get("id")
    status_voice = status.get(f"{prefix}_voice")
    if isinstance(voice_id, str) and voice_id and voice_id == status_voice:
        return True

    voice_model_path = voice.get("model_path")
    status_model_path = status.get(f"{prefix}_model_path")
    if isinstance(voice_model_path, str) and isinstance(status_model_path, str):
        return Path(voice_model_path).expanduser() == Path(status_model_path).expanduser()

    return False


def format_voice_with_speaker(voice_id: str, speaker_id: Any) -> str:
    if not voice_id or voice_id == "None":
        return "None"
    if isinstance(speaker_id, int) and speaker_count_for_voice_id(voice_id) != 1:
        return f"{voice_id} / Speaker {speaker_id}"
    return voice_id


def bounded_chars(values: list[str], minimum: int, maximum: int) -> int:
    widest = max((len(value) for value in values if value), default=minimum)
    return max(minimum, min(widest, maximum))


def value_width_pixels(values: list[str]) -> int:
    chars = bounded_chars(values, minimum=8, maximum=16)
    return (chars * 7) + 42


def is_daemon_connection_error(message: str) -> bool:
    return message in {
        "pauperd is not running",
        "pauperd is not accepting connections",
        "pauperd did not respond",
        "cannot connect to pauperd from this environment",
    }


def icon_label_button(icon_name: str, label: str) -> Gtk.Button:
    button = Gtk.Button()
    set_button_content(button, icon_name, label)
    return button


def set_button_content(button: Gtk.Button, icon_name: str, label: str) -> None:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.append(Gtk.Image.new_from_icon_name(icon_name))
    text = Gtk.Label(label=label)
    make_shrinking_label(text, 8)
    box.append(text)
    button.set_child(box)


def make_shrinking_label(label: Gtk.Label, max_chars: int) -> None:
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_width_chars(1)
    label.set_max_width_chars(max_chars)


def configure_string_dropdown(dropdown: Gtk.DropDown) -> None:
    factory = string_option_factory()
    dropdown.set_factory(factory)
    dropdown.set_list_factory(string_option_factory())
    dropdown.set_hexpand(True)
    dropdown.set_size_request(1, -1)


def string_option_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item) -> None:
        label = Gtk.Label(xalign=0)
        make_shrinking_label(label, 18)
        list_item.set_child(label)

    def bind(_factory, list_item) -> None:
        label = list_item.get_child()
        item = list_item.get_item()
        if isinstance(label, Gtk.Label) and isinstance(item, Gtk.StringObject):
            label.set_text(item.get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def speaker_option_model(options: list[SpeakerOption]) -> Gio.ListStore:
    model = Gio.ListStore.new(SpeakerOption)
    for option in options:
        model.append(option)
    return model


def speaker_option_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item) -> None:
        label = Gtk.Label(xalign=0)
        make_shrinking_label(label, 18)
        list_item.set_child(label)

    def bind(_factory, list_item) -> None:
        option = list_item.get_item()
        label = list_item.get_child()
        if not isinstance(option, SpeakerOption) or not isinstance(label, Gtk.Label):
            return

        label.set_text(option.label if option.available else f"{option.label} - no sample")
        label.set_sensitive(True)
        if option.available:
            label.remove_css_class("dim-label")
            label.set_tooltip_text(None)
        else:
            label.add_css_class("dim-label")
            label.set_tooltip_text("No bundled sample")

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def sample_cache_path(url: str):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    suffix = Path(urlparse(url).path).suffix or ".mp3"
    return xdg_cache_home() / "pauper" / "samples" / f"{digest}{suffix}"


def speaker_count(voice: dict[str, Any]) -> int:
    count = voice.get("num_speakers")
    if isinstance(count, int) and count > 0:
        return count

    speaker_map = voice.get("speaker_id_map")
    if isinstance(speaker_map, dict) and speaker_map:
        return len(speaker_map)

    samples = voice.get("samples")
    if isinstance(samples, list) and samples:
        return len(samples)

    return 1


def format_size(value: Any) -> str:
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


def enrich_voices_with_bundled_catalog(voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bundled = {voice.id: voice.to_dict() for voice in load_catalog()}
    enriched = []
    for voice in voices:
        voice_id = voice.get("id")
        if not isinstance(voice_id, str) or voice_id not in bundled:
            enriched.append(voice)
            continue

        merged = {**bundled[voice_id], **voice}
        for key in (
            "samples",
            "sample_path",
            "sample_url",
            "model_file",
            "config_file",
            "num_speakers",
            "speaker_id_map",
            "model_size_bytes",
            "config_size_bytes",
        ):
            if not merged.get(key):
                merged[key] = bundled[voice_id].get(key)
        enriched.append(merged)

    known_ids = {voice.get("id") for voice in enriched}
    for voice_id, voice in bundled.items():
        if voice_id not in known_ids:
            enriched.append(voice)

    return enriched


def bundled_sample_for_voice(voice: dict[str, Any], sample: dict[str, Any] | None = None) -> Path | None:
    candidates: list[str] = []
    if sample:
        path = sample.get("path")
        if isinstance(path, str) and path:
            candidates.append(path)

    sample_path = voice.get("sample_path")
    if isinstance(sample_path, str) and sample_path:
        candidates.append(sample_path)

    voice_id = voice.get("id")
    if isinstance(voice_id, str) and voice_id:
        candidates.extend(
            [
                f"samples/{voice_id}.mp3",
                f"samples/{voice_id}.wav",
                f"samples/{voice_id}.ogg",
                f"samples/{voice_id}.flac",
            ]
        )

    root = resources.files("pauper.resources")
    for candidate in candidates:
        resource = root / candidate
        if resource.is_file():
            if isinstance(resource, Path):
                return resource
            cached = xdg_cache_home() / "pauper" / "bundled-samples" / Path(candidate).name
            cached.parent.mkdir(parents=True, exist_ok=True)
            if not cached.exists():
                with resources.as_file(resource) as local_sample:
                    cached.write_bytes(Path(local_sample).read_bytes())
            return cached

    return None


def audio_output_items() -> list[tuple[str, str | None]]:
    items: list[tuple[str, str | None]] = [("Default output", None)]
    pactl_items = pactl_audio_output_items()
    return [*items, *pactl_items]


def pactl_audio_output_items() -> list[tuple[str, str]]:
    if not shutil.which("pactl"):
        return []

    try:
        short_result = subprocess.run(["pactl", "list", "short", "sinks"], check=True, capture_output=True, text=True)
    except Exception:
        return []

    descriptions = pactl_sink_descriptions()
    items = []
    for line in short_result.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        sink_name = columns[1].strip()
        if not sink_name:
            continue
        label = descriptions.get(sink_name, sink_name)
        items.append((label, sink_name))
    return items


def pactl_sink_descriptions() -> dict[str, str]:
    try:
        result = subprocess.run(["pactl", "list", "sinks"], check=True, capture_output=True, text=True)
    except Exception:
        return {}

    descriptions: dict[str, str] = {}
    current_name: str | None = None
    current_description: str | None = None
    for line in [*result.stdout.splitlines(), "Sink #end"]:
        stripped = line.strip()
        if stripped.startswith("Sink #"):
            if current_name and current_description:
                descriptions[current_name] = current_description
            current_name = None
            current_description = None
            continue
        if stripped.startswith("Name:"):
            current_name = stripped.removeprefix("Name:").strip()
        elif stripped.startswith("Description:"):
            current_description = stripped.removeprefix("Description:").strip()

    return descriptions


def audio_player_command(path: Path, output: str | None = None) -> list[str]:
    if output:
        if shutil.which("paplay"):
            return ["paplay", "--device", output, str(path)]
        if shutil.which("pw-play"):
            return ["pw-play", "--target", output, str(path)]
        raise RuntimeError("selected audio output requires paplay or pw-play")

    players = [
        ("gst-play-1.0", ["gst-play-1.0", "--no-interactive", str(path)]),
        ("mpv", ["mpv", "--really-quiet", str(path)]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)]),
        ("paplay", ["paplay", str(path)]),
        ("pw-play", ["pw-play", str(path)]),
    ]
    for executable, command in players:
        if shutil.which(executable):
            return command

    raise RuntimeError("no sample-capable audio player found; install gstreamer, mpv, ffmpeg, or pipewire")


def resolve_sample_url(voice: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    if sample:
        url = sample.get("url")
        if isinstance(url, str) and url:
            return url

    url = voice.get("sample_url")
    if isinstance(url, str) and url:
        return url
    raise RuntimeError("this voice does not advertise a sample")


def download_url(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url) as response, destination.open("wb") as out:
        out.write(response.read())


if __name__ == "__main__":
    raise SystemExit(main())
