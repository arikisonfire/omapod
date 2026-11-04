#!/usr/bin/env bash
# Install omaPOD for the current user. No sudo needed.
#
# Installed from the Omarchy plugin marketplace, app and bar widget are there already. Run this script from that
# folder for the extras: an entry in the app launcher, podcast links and, with --timer, an hourly refresh while
# omaPOD is closed. They then run omaPOD from that folder, so omarchy plugin update updates them too.
#
#   ./install.sh                 install or update the app (copies the code)
#   ./install.sh --link          development install: run straight from this folder
#   ./install.sh --plugin        also install the bar widget (a copy; the marketplace brings its own)
#   ./install.sh --timer         also refresh podcasts hourly while omaPOD is closed
#   ./install.sh --uninstall     remove the app, launcher, widget and timer (keeps your library)
#
# Links starting with podcast://, itpc://, pcast:// and antennapod-subscribe:// open omaPOD afterwards,
# unless you chose another app for them in ~/.config/mimeapps.list.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID="io.github.arikisonfire.OmaPod"
APP_DIR="$HOME/.local/share/omapod-app"
BIN="$HOME/.local/bin/omapod"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
PLUGIN_ID="io.github.arikisonfire.pod"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
UNITS="$HOME/.config/systemd/user"
MIMEAPPS="${XDG_CONFIG_HOME:-$HOME/.config}/mimeapps.list"
SCHEMES=(podcast itpc pcast antennapod-subscribe)
LINKS="^x-scheme-handler/($(IFS='|'; echo "${SCHEMES[*]}"))=${APP_ID//./\\.}\\.desktop;?\$"

link=0 plugin=0 timer=0 uninstall=0
for arg in "$@"; do
  case "$arg" in
    --link) link=1 ;;
    --plugin) plugin=1 ;;
    --timer) timer=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '/^set -euo pipefail/q; 2,$p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# In the plugins folder this is the marketplace's copy of omaPOD: the app runs from here, and omarchy plugin
# update keeps it current along with the widget.
case "$SRC/" in
  "$HOME/.config/omarchy/plugins/"*) link=1 ;;
esac

if [ "$uninstall" = 1 ]; then
  systemctl --user disable --now omapod-refresh.timer 2>/dev/null || true
  rm -f "$UNITS/omapod-refresh.timer" "$UNITS/omapod-refresh.service"
  systemctl --user daemon-reload 2>/dev/null || true
  rm -rf "$APP_DIR" "$BIN" "$DESKTOP" "$ICON" "${XDG_CACHE_HOME:-$HOME/.cache}/omapod"
  # Undo what xdg-mime default wrote; through a possible symlink (dotfile managers), so it stays one.
  if [ -f "$MIMEAPPS" ] && grep -qE "$LINKS" "$MIMEAPPS"; then
    kept="$(grep -vE "$LINKS" "$MIMEAPPS" || true)"
    printf '%s\n' "$kept" > "$MIMEAPPS"
  fi
  if [ -e "$PLUGIN_DIR/.git" ]; then
    # Installed from the Omarchy plugin marketplace: a git clone this script did not create.
    echo "Left the bar widget, which came from the Omarchy plugin marketplace. Remove it with:" \
      "omarchy plugin remove $PLUGIN_ID"
  elif [ -d "$PLUGIN_DIR" ]; then
    rm -rf "$PLUGIN_DIR"
    echo "Removed the bar widget. Remove it from the bar with: omarchy plugin disable $PLUGIN_ID"
  fi
  command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
  echo "omaPOD removed. Your library is still in ${XDG_DATA_HOME:-$HOME/.local/share}/omapod and" \
    "${XDG_CONFIG_HOME:-$HOME/.config}/omapod."
  exit 0
fi

missing=()
python3 - <<'PY' || missing+=("python-gobject gtk4 libadwaita")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
PY
command -v mpv >/dev/null || missing+=("mpv")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing packages: ${missing[*]}" >&2
  echo "Install them with: sudo pacman -S --needed ${missing[*]}" >&2
  exit 1
fi

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")" "$HOME/.config"
if [ "$link" = 1 ]; then
  rm -rf "$APP_DIR"
  ln -s "$SRC" "$APP_DIR"
else
  if [ -L "$APP_DIR" ]; then rm -f "$APP_DIR"; fi
  rm -rf "$APP_DIR.new"
  mkdir -p "$APP_DIR.new"
  cp -r "$SRC/omapod" "$APP_DIR.new/"
  find "$APP_DIR.new" -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf "$APP_DIR"
  mv "$APP_DIR.new" "$APP_DIR"
fi

# Quoted with %q: a home folder with spaces or quotes must not break the script. Bytecode goes to the cache,
# never next to the code: in the plugins folder every new file makes the shell reload all plugins.
cat > "$BIN" <<SH
#!/usr/bin/env bash
export PYTHONPYCACHEPREFIX="\${XDG_CACHE_HOME:-\$HOME/.cache}/omapod/pycache"
export PYTHONPATH=$(printf %q "$APP_DIR")\${PYTHONPATH:+:\$PYTHONPATH}
exec python3 -m omapod "\$@"
SH
chmod +x "$BIN"

template="$(<"$SRC/data/$APP_ID.desktop.in")"
exec_bin="$BIN"
[[ "$BIN" == *[[:space:]]* ]] && exec_bin="\"$BIN\""  # the desktop entry spec wants such paths quoted
printf '%s\n' "${template//@BIN@/"$exec_bin"}" > "$DESKTOP"
cp "$SRC/data/$APP_ID.svg" "$ICON"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "Installed omaPOD: run 'omapod' or open it from the app launcher."

# Podcast links open omaPOD, unless the user picked another app for them.
kept=()
for scheme in "${SCHEMES[@]}"; do
  chosen="$(awk -F= -v key="x-scheme-handler/$scheme" '/^\[/ { section = $0 }
    section == "[Default Applications]" && $1 == key { print $2; exit }' "$MIMEAPPS" 2>/dev/null || true)"
  chosen="${chosen%%;*}"
  if [ -n "$chosen" ] && [ "$chosen" != "$APP_ID.desktop" ]; then
    kept+=("$scheme:// ($chosen)")
  elif command -v xdg-mime >/dev/null; then
    xdg-mime default "$APP_ID.desktop" "x-scheme-handler/$scheme" || true
  fi
done
if [ "${#kept[@]}" -gt 0 ]; then
  echo "Left your choice of app for ${kept[*]}. To open them with omaPOD instead:"
  echo "  xdg-mime default $APP_ID.desktop x-scheme-handler/<scheme>"
else
  echo "Links starting with podcast://, itpc://, pcast:// and antennapod-subscribe:// now open omaPOD."
fi

if [ "$timer" = 1 ]; then
  mkdir -p "$UNITS"
  cat > "$UNITS/omapod-refresh.service" <<UNIT
[Unit]
Description=Refresh omaPOD podcasts

[Service]
Type=oneshot
# %h is the home folder; quoted, so spaces in it cannot split the command.
ExecStart="%h/${BIN#"$HOME"/}" --refresh
UNIT
  cat > "$UNITS/omapod-refresh.timer" <<UNIT
[Unit]
Description=Refresh omaPOD podcasts every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now omapod-refresh.timer
  echo "Background refresh enabled (systemctl --user status omapod-refresh.timer)."
fi

if [ "$plugin" = 1 ]; then
  if [ -e "$PLUGIN_DIR/.git" ]; then
    # From the Omarchy plugin marketplace: a copy would replace the clone, and omarchy could no longer update it.
    echo "The bar widget came from the Omarchy plugin marketplace; update it with: omarchy plugin update $PLUGIN_ID"
    exit 0
  fi
  # The widget alone, laid out as in the repository; it starts the app through the launcher installed above.
  files=(manifest.json plugin/BarWidget.qml)
  for extra in README.md LICENSE; do
    [ -f "$SRC/$extra" ] && files+=("$extra")
  done
  same=1
  for file in "${files[@]}"; do
    cmp -s "$SRC/$file" "$PLUGIN_DIR/$file" || same=0
  done
  [ -e "$PLUGIN_DIR/BarWidget.qml" ] && same=0  # a copy from before the widget moved into plugin/
  if [ "$same" = 1 ]; then
    echo "The bar widget is up to date."
    exit 0
  fi
  # Any change under ~/.config/omarchy/plugins makes the shell reload every plugin.
  echo "Installing the bar widget makes the Omarchy shell reload all of its plugins; whatever they are doing" \
    "right now (a running sync, for example) is interrupted."
  if [ -t 0 ]; then
    read -r -p "Continue? [Y/n] " answer
    case "$answer" in
      [nN]*) echo "Skipped the bar widget."; exit 0 ;;
    esac
  fi
  mkdir -p "$(dirname "$PLUGIN_DIR")"
  staging="$(dirname "$PLUGIN_DIR")/.$PLUGIN_ID.new"
  rm -rf "$staging"
  mkdir -p "$staging/plugin"
  for file in "${files[@]}"; do
    cp "$SRC/$file" "$staging/$file"
  done
  rm -rf "$PLUGIN_DIR"
  mv "$staging" "$PLUGIN_DIR"
  echo "Installed the bar widget. Turn it on with: omarchy plugin enable $PLUGIN_ID"
fi
