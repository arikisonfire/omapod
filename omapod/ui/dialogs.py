"""Dialogs: podcast settings and info, credentials, prompts, tags, log details."""

import hashlib
import hmac
import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..i18n import _, ngettext
from ..models import VOLUME_ADAPTATIONS
from ..util import fmt_datetime, fmt_long_duration, fmt_speed, html_to_text
from .widgets.common import label, text_row
from .widgets.shownotes import ShowNotes

GLOBAL_CHOICE = -1


def _combo_row(title, subtitle, options, current, on_change):
    """options: list of (value, label)."""
    row = Adw.ComboRow(title=title, subtitle=subtitle or "")
    model = Gtk.StringList()
    for _value, text in options:
        model.append(text)
    row.set_model(model)
    values = [value for value, _text in options]
    row.set_selected(values.index(current) if current in values else 0)
    row.connect("notify::selected", lambda r, _p: on_change(values[r.get_selected()]))
    return row


class FeedSettingsDialog(Adw.PreferencesDialog):
    """AntennaPod's per-podcast settings."""

    def __init__(self, ctx, feed_id):
        super().__init__(title=_("Podcast settings"))
        self.ctx = ctx
        self.feed_id = feed_id
        feed = ctx.db.get_feed(feed_id)
        self.feed = feed
        lib = ctx.library
        page = Adw.PreferencesPage(title=_("Podcast"), icon_name="emblem-system-symbolic")
        self.add(page)

        def update(**values):
            lib.update_feed_settings(feed_id, **values)
            ctx.player.feed_settings_changed(feed_id)

        # Group titles are markup, with no switch to turn that off: "Q&A" would leave the title empty.
        general = Adw.PreferencesGroup(title=GLib.markup_escape_text(feed.display_title))
        keep = Adw.SwitchRow(title=_("Keep updated"),
                             subtitle=_("Include this podcast when refreshing all podcasts"),
                             active=bool(feed.keep_updated))
        keep.connect("notify::active", lambda r, _p: update(keep_updated=1 if r.get_active() else 0))
        general.add(keep)
        notify = Adw.SwitchRow(title=_("Episode notifications"),
                               subtitle=_("Show a notification when a new episode is released"),
                               active=bool(feed.notify))
        notify.connect("notify::active", lambda r, _p: update(notify=1 if r.get_active() else 0))
        general.add(notify)
        general.add(_combo_row(_("New episodes"), _("What happens with new episodes of this podcast"),
                               [(-1, _("Global default")), (1, _("Add to inbox")), (3, _("Add to queue")),
                                (2, _("Nothing"))], feed.new_episodes_action,
                               lambda v: update(new_episodes_action=v)))
        tags = text_row(_("Tags"), ", ".join(feed.tags) or _("No tags"), activatable=True)
        tags.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        tags.connect("activated", lambda _r: edit_tags(ctx, [feed_id], on_done=lambda: tags.set_subtitle(
            ", ".join(ctx.db.get_feed(feed_id).tags) or _("No tags"))))
        general.add(tags)
        page.add(general)

        playback = Adw.PreferencesGroup(title=_("Playback"))
        speeds = [(-1.0, _("Global default ({speed})").format(speed=fmt_speed(ctx.settings.get("speed") or 1.0)))]
        for speed in (0.75, 0.9, 1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.75, 2.0, 2.5, 3.0):
            speeds.append((speed, fmt_speed(speed)))
        current_speed = feed.speed if feed.speed and feed.speed > 0 else -1.0
        if current_speed not in [s for s, _t in speeds]:
            speeds.append((current_speed, fmt_speed(current_speed)))
        playback.add(_combo_row(_("Playback speed"), _("Speed used when an episode of this podcast starts"),
                                speeds, current_speed, lambda v: update(speed=v)))
        playback.add(_combo_row(_("Skip silence"), None,
                                [(-1, _("Global default")), (1, _("On")), (0, _("Off"))], feed.skip_silence,
                                lambda v: update(skip_silence=v)))
        intro = Adw.SpinRow.new_with_range(0, 600, 5)
        intro.set_title(_("Skip first"))
        intro.set_subtitle(_("Seconds to skip at the start (introductions)"))
        intro.set_value(feed.skip_intro)
        intro.connect("notify::value", lambda r, _p: update(skip_intro=int(r.get_value())))
        playback.add(intro)
        ending = Adw.SpinRow.new_with_range(0, 600, 5)
        ending.set_title(_("Skip last"))
        ending.set_subtitle(_("Seconds to skip at the end (credits)"))
        ending.set_value(feed.skip_ending)
        ending.connect("notify::value", lambda r, _p: update(skip_ending=int(r.get_value())))
        playback.add(ending)
        names = {"off": _("No adaptation"), "light_reduction": _("Light reduction"),
                 "heavy_reduction": _("Heavy reduction"), "light_boost": _("Light boost"),
                 "medium_boost": _("Medium boost"), "heavy_boost": _("Heavy boost")}
        playback.add(_combo_row(_("Volume adaptation"), _("Turn this podcast up or down"),
                                [(i, names[key]) for i, (key, _f) in enumerate(VOLUME_ADAPTATIONS)],
                                feed.volume_adaptation, lambda v: update(volume_adaptation=v)))
        page.add(playback)

        downloads = Adw.PreferencesGroup(title=_("Downloads"))
        downloads.add(_combo_row(_("Download automatically"), None,
                                 [(-1, _("Global default")), (1, _("Always")), (0, _("Never"))], feed.auto_download,
                                 lambda v: update(auto_download=v)))
        downloads.add(_combo_row(_("Delete after playing"), None,
                                 [(-1, _("Global default")), (1, _("Always")), (0, _("Never"))], feed.auto_delete,
                                 lambda v: update(auto_delete=v)))
        include = Adw.EntryRow(title=_("Only download episodes containing"), text=feed.include_terms)
        include.set_show_apply_button(True)
        include.connect("apply", lambda r: update(include_terms=r.get_text()))
        downloads.add(include)
        exclude = Adw.EntryRow(title=_("Never download episodes containing"), text=feed.exclude_terms)
        exclude.set_show_apply_button(True)
        exclude.connect("apply", lambda r: update(exclude_terms=r.get_text()))
        downloads.add(exclude)
        minimum = Adw.SpinRow.new_with_range(0, 240, 1)
        minimum.set_title(_("Skip episodes shorter than"))
        minimum.set_subtitle(_("Minutes; 0 turns this off"))
        minimum.set_value(max(0, feed.min_duration) // 60 if feed.min_duration > 0 else 0)
        minimum.connect("notify::value", lambda r, _p: update(min_duration=int(r.get_value()) * 60
                                                              if r.get_value() > 0 else -1))
        downloads.add(minimum)
        downloads.set_description(_("Words separated by spaces, \"phrases in quotes\". Applies to automatic "
                                    "downloads."))
        page.add(downloads)

        if not feed.is_local:
            auth = Adw.PreferencesGroup(title=_("Authentication"),
                                        description=_("For podcasts that need a username and password"))
            user = Adw.EntryRow(title=_("Username"), text=feed.username)
            password = Adw.PasswordEntryRow(title=_("Password"), text=feed.password)
            for row in (user, password):
                row.set_show_apply_button(True)
                row.connect("apply", lambda _r: update(username=user.get_text(), password=password.get_text()))
                auth.add(row)
            page.add(auth)


def feed_info(ctx, feed_id):
    feed = ctx.db.get_feed(feed_id, with_counts=True)
    if feed is None:
        return
    dialog = Adw.Dialog(title=feed.display_title, content_width=560, content_height=640)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(20)
    box.append(label(feed.display_title, ("omp-display",), wrap=True))
    if feed.author:
        box.append(label(feed.author, ("omp-feed-author",), wrap=True))
    notes = ShowNotes(ctx)
    notes.set_notes(feed.description, _("No description."))
    box.append(notes)
    group = Adw.PreferencesGroup(title=_("Details"))
    rows = [
        (_("Episodes"), str(feed.count_total)),
        (_("Played"), str(feed.count_played)),
        (_("Downloaded"), str(feed.count_downloaded)),
        (_("Last refresh"), fmt_datetime(feed.last_refresh) or _("Never")),
        (_("Language"), feed.language or "–"),
    ]
    stats = [s for s in ctx.library.statistics() if s["feed_id"] == feed_id]
    if stats:
        rows.append((_("Time played"), fmt_long_duration(stats[0]["played"])))
    for title, value in rows:
        row = Adw.ActionRow(title=title)
        row.add_suffix(label(value, ("omp-dim", "omp-numeric"), xalign=1.0))
        group.add(row)
    # Rows with what the feed says show it as it is (text_row): an address with "&" was empty before.
    url_row = text_row(_("Feed address"), feed.url, subtitle_selectable=True)
    copy = Gtk.Button(icon_name="edit-copy-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                      tooltip_text=_("Copy"))
    copy.connect("clicked", lambda _b: ctx.copy_text(feed.url))
    url_row.add_suffix(copy)
    group.add(url_row)
    if feed.link:
        site = text_row(_("Website"), feed.link, activatable=True)
        site.add_suffix(Gtk.Image(icon_name="web-browser-symbolic"))
        site.connect("activated", lambda _r: ctx.open_uri(feed.link))
        group.add(site)
    box.append(group)
    if feed.funding:
        support = Adw.PreferencesGroup(title=_("Support this podcast"))
        for entry in feed.funding:
            row = text_row(entry.get("title") or entry["url"], entry["url"], activatable=True)
            row.add_suffix(Gtk.Image(icon_name="web-browser-symbolic"))
            row.connect("activated", lambda _r, url=entry["url"]: ctx.open_uri(url))
            support.add(row)
        box.append(support)
    scrolled.set_child(box)
    view.set_content(scrolled)
    dialog.set_child(view)
    dialog.present(ctx.window)
    return dialog


def log_details(ctx, data):
    body = data.get("reason") or ""
    if data.get("detail") and data.get("detail") != body:
        body += "\n\n" + _("Technical details") + ":\n" + data["detail"]
    if data.get("url"):
        body += "\n\n" + _("File URL") + ":\n" + data["url"]
    dialog = Adw.AlertDialog(heading=data.get("title") or _("Download failed"), body=body)
    dialog.set_body_use_markup(False)
    dialog.add_response("close", _("Close"))
    dialog.add_response("copy", _("Copy"))
    if data.get("kind") == "media" and data.get("ref_id"):
        dialog.add_response("retry", _("Retry"))
        dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
    elif data.get("kind") == "feed" and data.get("ref_id"):
        dialog.add_response("open", _("Open podcast"))

    def on_response(_dialog, response):
        if response == "copy":
            ctx.copy_text(body)
        elif response == "retry":
            ctx.library.download([data["ref_id"]])
        elif response == "open":
            ctx.show_feed(data["ref_id"])

    dialog.connect("response", on_response)
    dialog.present(ctx.window)


def text_prompt(parent, heading, value, confirm_label, callback, body="", reset_label=None, on_reset=None):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=value or "", activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    if reset_label and on_reset:
        dialog.add_response("reset", reset_label)
    dialog.add_response("ok", confirm_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response):
        if response == "ok" and entry.get_text().strip():
            callback(entry.get_text())
        elif response == "reset" and on_reset:
            on_reset()

    dialog.connect("response", on_response)
    dialog.present(parent)


def ask_credentials(parent, what, callback, username=""):
    dialog = Adw.AlertDialog(heading=_("Authentication required"),
                             body=_("“{what}” needs a username and password.").format(what=what))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    user = Gtk.Entry(placeholder_text=_("Username"), text=username or "")
    password = Gtk.PasswordEntry(placeholder_text=_("Password"), show_peek_icon=True, activates_default=True)
    box.append(user)
    box.append(password)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("Log in"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")

    def on_response(_dialog, response):
        if response == "ok" and user.get_text():
            callback(user.get_text(), password.get_text())

    dialog.connect("response", on_response)
    dialog.present(parent)


def choose_feed(parent, candidates, callback):
    dialog = Adw.AlertDialog(heading=_("Choose a feed"),
                             body=_("This website offers more than one feed. Which one do you want?"))
    for index, candidate in enumerate(candidates[:6]):
        dialog.add_response(f"c{index}", candidate.get("title") or candidate["url"])
    dialog.add_response("cancel", _("Cancel"))
    dialog.set_close_response("cancel")

    def on_response(_dialog, response):
        if response.startswith("c"):
            callback(candidates[int(response[1:])]["url"])

    dialog.connect("response", on_response)
    dialog.present(parent)


def edit_tags(ctx, feed_ids, on_done=None):
    feeds = [ctx.db.get_feed(i) for i in feed_ids]
    feeds = [f for f in feeds if f is not None]
    common = set(feeds[0].tags) if feeds else set()
    for feed in feeds[1:]:
        common &= set(feed.tags)
    all_tags = ctx.db.all_tags()
    dialog = Adw.AlertDialog(heading=_("Edit tags"),
                             body=_("Tags help to organize your subscriptions.") if len(feeds) == 1 else
                             _("Only common tags from all selected subscriptions are shown. Other tags stay "
                               "unaffected."))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    checks = {}
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, column_spacing=6,
                       row_spacing=6)
    for tag in all_tags:
        check = Gtk.CheckButton(label=tag, active=tag in common)
        checks[tag] = check
        flow.append(check)
    box.append(flow)
    entry = Gtk.Entry(placeholder_text=_("Add tag"))
    box.append(entry)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("Save"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_dialog, response):
        if response != "ok":
            return
        chosen = {tag for tag, check in checks.items() if check.get_active()}
        new = [t.strip() for t in entry.get_text().split(",") if t.strip()]
        chosen |= set(new)
        for feed in feeds:
            keep = set(feed.tags) - common
            ctx.library.set_tags(feed.id, list(keep | chosen))
        if on_done:
            on_done()

    dialog.connect("response", on_response)
    dialog.present(ctx.window)


def password_hash(password):
    """Salted scrypt, so that a copied settings file does not give away a short password right away."""
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def _scrypt(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1)


def password_matches(password, stored):
    stored = stored or ""
    if stored.startswith("scrypt$"):
        try:
            _kind, salt, digest = stored.split("$")
            computed = _scrypt(password, bytes.fromhex(salt)).hex()
        except ValueError:
            return False
        return hmac.compare_digest(computed, digest)
    # Set with omaPOD 0.1.0: unsalted SHA-256; replaced after the next correct entry.
    legacy = hashlib.sha256(("omapod:" + password).encode("utf-8")).hexdigest()
    return bool(stored) and hmac.compare_digest(legacy, stored)


def check_parental(ctx, on_ok):
    dialog = Adw.AlertDialog(heading=_("Parental control"),
                             body=_("Enter the parental control password to continue."))
    entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("Continue"))
    dialog.set_default_response("ok")

    def on_response(_dialog, response):
        if response != "ok":
            return
        stored = ctx.settings.get("parental_password_hash")
        if password_matches(entry.get_text(), stored):
            if not stored.startswith("scrypt$"):
                ctx.settings.set("parental_password_hash", password_hash(entry.get_text()))
            on_ok()
        else:
            ctx.toast(_("Wrong password"))

    dialog.connect("response", on_response)
    dialog.present(ctx.window)
