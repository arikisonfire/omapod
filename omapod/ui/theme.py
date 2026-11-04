"""Live Omarchy theming.

Reads the active theme (colors.toml, shell.toml), the user's shell.toml and
Hyprland's rounding, turns them into a stylesheet and re-applies it when the
theme changes on disk. Every colour in the app comes from here.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..events import bus

HOME = Path.home()
STATE_CURRENT = HOME / ".local" / "state" / "omarchy" / "current"
USER_SHELL = HOME / ".config" / "omarchy" / "shell.toml"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "style.css"

# Fallback palette: Omarchy's Tokyo Night, used when no theme can be read.
FALLBACK = {
    "mode": "dark", "accent": "#7aa2f7", "selection": "#292e42", "muted": "#414868",
    "background": "#1a1b26", "dark_background": "#13141c", "darker_background": "#0e0e14",
    "lighter_background": "#24283b", "foreground": "#a9b1d6", "dark_foreground": "#565f89",
    "light_foreground": "#b4bee6", "bright_foreground": "#c0caf5", "red": "#f7768e", "yellow": "#e0af68",
    "orange": "#eb927b", "green": "#9ece6a", "cyan": "#449dab", "blue": "#7aa2f7", "magenta": "#ad8ee6",
}


# ---------------------------------------------------------------- colour maths

def parse_hex(value):
    value = (value or "").strip().strip("\"'")
    match = re.match(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$", value)
    if not match:
        match3 = re.match(r"^#?([0-9a-fA-F]{3})$", value)
        if not match3:
            return None
        value = "".join(c * 2 for c in match3.group(1))
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hexa = match.group(1)
    return tuple(int(hexa[i:i + 2], 16) / 255 for i in (0, 2, 4))


def to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def mix(a, b, t):
    """t=0 -> a, t=1 -> b."""
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def luminance(rgb):
    def channel(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(color, background, minimum, toward):
    """Move `color` toward `toward` until it reaches `minimum` contrast on `background`."""
    if contrast(color, background) >= minimum:
        return color
    for step in range(1, 21):
        candidate = mix(color, toward, step / 20)
        if contrast(candidate, background) >= minimum:
            return candidate
    return toward


def rgba(rgb, alpha):
    r, g, b = (round(c * 255) for c in rgb)
    return f"rgba({r},{g},{b},{alpha:.3f})"


# ---------------------------------------------------------------- reading the theme

def theme_dir():
    override = os.environ.get("OMAPOD_THEME_DIR")
    if override:
        return Path(override)
    return STATE_CURRENT / "theme"


def _load_toml(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    if tomllib is not None:
        try:
            return tomllib.loads(raw)
        except (tomllib.TOMLDecodeError, ValueError):
            pass
    data_root = {}
    current = data_root
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip() if not line.strip().startswith(("\"", "'")) else line.strip()
        if not line:
            continue
        sec = re.match(r"^\[([A-Za-z0-9_.-]+)\]$", line)
        if sec:
            current = data_root.setdefault(sec.group(1), {})
            continue
        kv = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", line)
        if kv:
            value = kv.group(2).strip().strip("\"'")
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                pass
            current[kv.group(1)] = value
    return data_root


@dataclass
class Theme:
    name: str = ""
    dark: bool = True
    colors: dict = field(default_factory=dict)
    base_size: int = 12
    radius: int = 0
    font: str = "monospace"
    controls: dict = field(default_factory=dict)

    def color(self, key, fallback_key=None):
        value = parse_hex(self.colors.get(key))
        if value is None and fallback_key:
            value = parse_hex(self.colors.get(fallback_key))
        return value


def read_theme():
    folder = theme_dir()
    colors = dict(FALLBACK)
    loaded = _load_toml(folder / "colors.toml")
    raw_colors = {k: v for k, v in loaded.items() if isinstance(v, str)}
    # Older themes only have color0..color15.
    legacy = {"background": "color0", "foreground": "color7", "accent": "color4", "red": "color1",
              "green": "color2", "yellow": "color3", "blue": "color4", "magenta": "color5", "cyan": "color6",
              "muted": "color8"}
    for key, old in legacy.items():
        if key not in raw_colors and old in raw_colors:
            raw_colors[key] = raw_colors[old]
    if raw_colors.get("background") and raw_colors.get("foreground"):
        colors = {"mode": ""}
        colors.update(raw_colors)
    name = ""
    try:
        name = (STATE_CURRENT / "theme.name").read_text().strip()
    except OSError:
        name = folder.name
    bg = parse_hex(colors.get("background")) or parse_hex(FALLBACK["background"])
    mode = str(colors.get("mode") or "").lower()
    dark = mode != "light" if mode in ("light", "dark") else luminance(bg) < 0.4
    shell = _load_toml(folder / "shell.toml")
    user_shell = _load_toml(USER_SHELL)
    base = 12
    controls = {}
    for source in (shell, user_shell):
        font = source.get("font") or {}
        if isinstance(font, dict) and font.get("base-size"):
            try:
                base = int(font["base-size"])
            except (TypeError, ValueError):
                pass
        ctrl = source.get("controls") or source.get("style") or {}
        if isinstance(ctrl, dict):
            controls.update(ctrl)
    return Theme(name=name, dark=dark, colors=colors, base_size=max(9, min(base, 24)), controls=controls)


def hypr_rounding():
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return None
    try:
        out = subprocess.run(["hyprctl", "-j", "getoption", "decoration:rounding"], capture_output=True,
                             text=True, timeout=2).stdout
        return max(0, int(json.loads(out).get("int", 0)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- stylesheet

def _alpha_token(controls, key, default):
    try:
        return max(0.0, min(1.0, float(controls.get(key, default))))
    except (TypeError, ValueError):
        return default


def palette(theme):
    """Resolve every colour the stylesheet needs, with contrast fixes."""
    c = theme.color
    bg = c("background") or parse_hex(FALLBACK["background"])
    fg = c("foreground") or parse_hex(FALLBACK["foreground"])
    toward_text = (1.0, 1.0, 1.0) if theme.dark else (0.0, 0.0, 0.0)
    fg = ensure_contrast(fg, bg, 7.0, toward_text)
    bg_dark = c("dark_background") or mix(bg, fg, 0.035 if theme.dark else 0.045)
    bg_darker = c("darker_background") or mix(bg, fg, 0.06)
    bg_light = c("lighter_background") or mix(bg, fg, 0.08)
    # On light themes "darker" backgrounds are darker greys; keep the rail and dock subtle.
    if not theme.dark:
        bg_dark = mix(bg, bg_dark, 0.85)
        bg_darker = mix(bg, bg_darker, 0.85)
    accent = c("accent") or c("blue") or fg
    accent_text = ensure_contrast(accent, bg, 4.5, toward_text)
    accent_on = bg if contrast(bg, accent) >= contrast(fg, accent) else fg
    if contrast(accent_on, accent) < 4.5:
        accent_on = (0.0, 0.0, 0.0) if luminance(accent) > 0.35 else (1.0, 1.0, 1.0)
    selection = c("selection") or mix(bg, accent, 0.25)
    if contrast(fg, selection) < 4.5:
        selection = mix(bg, selection, 0.5)
    secondary = ensure_contrast(mix(fg, bg, 0.32), bg, 4.6, fg)
    tertiary = ensure_contrast(mix(fg, bg, 0.5), bg, 3.0, fg)
    red = c("red") or parse_hex("#e06c75")
    red_text = ensure_contrast(red, bg, 4.5, toward_text)
    green = c("green") or parse_hex("#98c379")
    yellow = c("yellow") or parse_hex("#e5c07b")
    controls = theme.controls
    return {
        "bg": bg, "bg_dark": bg_dark, "bg_darker": bg_darker, "bg_light": bg_light, "fg": fg,
        "fg2": secondary, "fg3": tertiary, "accent": accent, "accent_text": accent_text, "accent_on": accent_on,
        "selection": selection, "red": red, "red_text": red_text, "green": green, "yellow": yellow,
        "a_normal": _alpha_token(controls, "normal-fill-alpha", 0.04),
        "a_hover": _alpha_token(controls, "hover-cursor-fill-alpha", 0.08),
        "a_selected": _alpha_token(controls, "selected-fill-alpha", 0.18),
        "a_pressed": _alpha_token(controls, "pressed-fill-alpha", 0.22),
        "a_border": _alpha_token(controls, "normal-border-alpha", 0.4),
        "a_border_hover": _alpha_token(controls, "hover-cursor-border-alpha", 0.25),
    }


def render_css(theme):
    p = palette(theme)
    base = theme.base_size
    scale = base / 12
    values = {
        "bg": to_hex(p["bg"]), "bg_dark": to_hex(p["bg_dark"]), "bg_darker": to_hex(p["bg_darker"]),
        "bg_light": to_hex(p["bg_light"]), "fg": to_hex(p["fg"]), "fg2": to_hex(p["fg2"]), "fg3": to_hex(p["fg3"]),
        "accent": to_hex(p["accent"]), "accent_text": to_hex(p["accent_text"]), "accent_on": to_hex(p["accent_on"]),
        "selection": to_hex(p["selection"]), "red": to_hex(p["red"]), "red_text": to_hex(p["red_text"]),
        "green": to_hex(p["green"]), "yellow": to_hex(p["yellow"]),
        "fill": rgba(p["fg"], p["a_normal"]), "fill_hover": rgba(p["fg"], p["a_hover"]),
        "fill_selected": rgba(p["fg"], p["a_selected"]), "fill_pressed": rgba(p["fg"], p["a_pressed"]),
        "border": rgba(p["fg"], p["a_border"]), "border_hover": rgba(p["fg"], p["a_border_hover"]),
        "hairline": rgba(p["fg"], 0.12), "hairline_strong": rgba(p["fg"], 0.22),
        "accent_soft": rgba(p["accent"], 0.18), "accent_glow": rgba(p["accent"], 0.35),
        "shade": rgba((0, 0, 0), 0.32 if theme.dark else 0.12),
        "scrim": rgba(p["bg"], 0.72),
        "radius": f"{theme.radius}px", "radius_small": f"{min(theme.radius, 6)}px",
        "font": theme.font,
        "fs_caption": f"{round(base * 0.833)}px", "fs_small": f"{round(base * 0.917)}px", "fs_body": f"{base}px",
        "fs_subtitle": f"{round(base * 1.083)}px", "fs_title": f"{round(base * 1.167)}px",
        "fs_heading": f"{round(base * 1.333)}px", "fs_display": f"{round(base * 2.0)}px",
        "fs_display_large": f"{round(base * 2.333)}px", "fs_hero": f"{round(base * 4)}px",
        "sp_xs": f"{round(3 * scale)}px", "sp_sm": f"{round(4 * scale)}px", "sp_md": f"{round(6 * scale)}px",
        "sp_lg": f"{round(8 * scale)}px", "sp_xl": f"{round(10 * scale)}px", "sp_xxl": f"{round(12 * scale)}px",
        "sp_huge": f"{round(18 * scale)}px", "sp_page": f"{round(24 * scale)}px",
        "control_h": f"{round(28 * scale)}px",
        "scheme": "dark" if theme.dark else "light",
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    css = re.sub(r"@\{([a-z0-9_]+)\}", lambda m: values.get(m.group(1), m.group(0)), template)
    return css, values


class ThemeManager:
    def __init__(self):
        self.theme = None
        self.values = {}
        self.provider = Gtk.CssProvider()
        self._monitors = []
        self._pending = 0
        self._display = None

    def start(self):
        self._display = Gdk.Display.get_default()
        if self._display is not None:
            Gtk.StyleContext.add_provider_for_display(self._display, self.provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.reload()
        self._watch()
        threading.Thread(target=self._fetch_rounding, daemon=True).start()

    def _fetch_rounding(self):
        radius = hypr_rounding()
        if radius is not None:
            GLib.idle_add(self._set_radius, radius)

    def _set_radius(self, radius):
        if self.theme is not None and self.theme.radius != radius:
            self.theme.radius = radius
            self._apply()
        return False

    def reload(self):
        radius = self.theme.radius if self.theme is not None else 0
        self.theme = read_theme()
        self.theme.radius = radius
        self._apply()

    def _apply(self):
        css, self.values = render_css(self.theme)
        try:
            self.provider.load_from_string(css)
        except (AttributeError, TypeError):
            self.provider.load_from_data(css, -1)
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK if self.theme.dark else Adw.ColorScheme.FORCE_LIGHT)
        bus.emit("theme-changed", self.theme)

    # -- watching ------------------------------------------------------------

    def _watch(self):
        targets = [theme_dir(), STATE_CURRENT, USER_SHELL.parent]
        for path in targets:
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue
            monitor.connect("changed", self._on_changed)
            self._monitors.append(monitor)

    def _on_changed(self, _monitor, file, _other, _event):
        name = file.get_basename() if file is not None else ""
        if name and not (name in ("colors.toml", "shell.toml", "theme.name", "theme") or name.endswith(".toml")):
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(400, self._debounced_reload)

    def _debounced_reload(self):
        self._pending = 0
        old = (self.theme.colors, self.theme.base_size, self.theme.dark) if self.theme else None
        self.reload()
        # The theme directory may have been replaced: watch the new one.
        if old != (self.theme.colors, self.theme.base_size, self.theme.dark):
            for monitor in self._monitors:
                monitor.cancel()
            self._monitors = []
            self._watch()
        return False

    def color(self, key):
        return self.values.get(key, "#888888")
