"""Small shared widgets: covers, empty states, icon buttons, helpers."""

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from ...i18n import _


def wrap_box(spacing=8, **props):
    """A row that goes on in the next line when a narrow tile has no room (libadwaita before 1.7 has no WrapBox)."""
    wrap_type = getattr(Adw, "WrapBox", None)
    if wrap_type:
        return wrap_type(child_spacing=spacing, line_spacing=spacing, **props)
    return Gtk.Box(spacing=spacing, **props)


def text_row(title, subtitle="", **props):
    """An Adw.ActionRow that shows its title and subtitle as they are. Rows read both as markup by default, so an
    "&", common in addresses and names, would leave them empty, and a feed could format them. Markup is switched
    off before the texts are set: given together to the constructor, the texts can come first."""
    row = Adw.ActionRow(use_markup=False, **props)
    row.set_title(title)
    row.set_subtitle(subtitle)
    return row


def icon_button(icon, tooltip, callback=None, css=("flat", "omp-icon"), *args):
    # focus_on_click off here and on other buttons pressed again and again: every change of the keyboard focus makes
    # GTK check the style of each visible widget in the window, which takes a moment on a long list.
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, focus_on_click=False)
    for name in css:
        button.add_css_class(name)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    if callback is not None:
        button.connect("clicked", lambda _b: callback(*args))
    return button


def label(text="", css=(), xalign=0.0, wrap=False, lines=0, ellipsize=True, selectable=False, markup=False):
    widget = Gtk.Label(xalign=xalign)
    if markup:
        widget.set_markup(text)
    else:
        widget.set_text(text)
    for name in css:
        widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if lines:
            widget.set_lines(lines)
            widget.set_ellipsize(Pango.EllipsizeMode.END)
    elif ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    widget.set_selectable(selectable)
    return widget


def initials(title):
    words = [w for w in (title or "").replace("-", " ").split() if w[:1].isalnum()]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


class Cover(Gtk.Overlay):
    """Square cover art with initials as a fallback, loaded through the texture cache."""

    def __init__(self, textures, size=48):
        super().__init__()
        self.textures = textures
        self.size = size
        self._url = None
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("omp-cover")
        self.fallback = Gtk.Label(css_classes=["omp-cover-fallback"])
        self.fallback.set_size_request(size, size)
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_size_new_absolute(int(max(9, size * 0.32)) * Pango.SCALE))
        self.fallback.set_attributes(attributes)
        self.set_child(self.fallback)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.set_size_request(size, size)
        self.add_overlay(self.picture)

    def do_get_request_mode(self):
        # A picture says its height depends on its width. Next to a wrapping title, a row would then search for
        # the best way to share its width, laying out the title many times; that took 1 ms per row.
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def set_source(self, url, title=""):
        self.fallback.set_text(initials(title))
        if url == self._url and self.picture.get_paintable() is not None:
            return
        self._url = url
        self.picture.set_paintable(None)
        if not url:
            return
        scale = max(1, self.get_scale_factor() or 1)
        requested = url

        def ready(texture):
            if self._url == requested:
                self.picture.set_paintable(texture)

        texture = self.textures.get(url, self.size * max(scale, 2), ready)
        if texture is not None:
            self.picture.set_paintable(texture)

    def set_playing(self, playing):
        if playing:
            self.add_css_class("omp-playing")
        else:
            self.remove_css_class("omp-playing")


class EmptyState(Gtk.ScrolledWindow):
    """Scrolls: a stack is as tall as its tallest page, so a fixed height here would keep the list next to it from
    fitting into a low window, even with no empty state on screen."""

    def __init__(self, icon, title, text, button_label=None, on_button=None):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER,
                           halign=Gtk.Align.CENTER, css_classes=["omp-empty"])
        self.set_child(self.box)
        image = Gtk.Image(icon_name=icon, pixel_size=48, css_classes=["omp-empty-icon"])
        self.append(image)
        self.title = label(title, ("omp-empty-title",), xalign=0.5, wrap=True, ellipsize=False)
        self.title.set_justify(Gtk.Justification.CENTER)
        self.append(self.title)
        self.text = label(text, ("omp-empty-text",), xalign=0.5, wrap=True, ellipsize=False)
        self.text.set_justify(Gtk.Justification.CENTER)
        self.text.set_max_width_chars(52)
        self.append(self.text)
        self.button = None
        if button_label:
            self.button = Gtk.Button(label=button_label, halign=Gtk.Align.CENTER, css_classes=["omp-primary"])
            self.button.set_margin_top(8)
            if on_button:
                self.button.connect("clicked", lambda _b: on_button())
            self.append(self.button)

    def append(self, widget):
        self.box.append(widget)

    def update(self, title=None, text=None):
        if title is not None:
            self.title.set_text(title)
        if text is not None:
            self.text.set_text(text)


class Debouncer:
    def __init__(self, delay_ms, callback):
        self.delay = delay_ms
        self.callback = callback
        self.source = 0

    def __call__(self, *_args):
        if self.source:
            GLib.source_remove(self.source)
        self.source = GLib.timeout_add(self.delay, self._fire)

    def _fire(self):
        self.source = 0
        self.callback()
        return False

    def cancel(self):
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0


class EpisodeItem(GObject.Object):
    __gtype_name__ = "OmaPodEpisodeItem"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, episode):
        super().__init__()
        self.episode = episode

    @property
    def id(self):
        return self.episode.id


class FeedItem(GObject.Object):
    __gtype_name__ = "OmaPodFeedItem"

    def __init__(self, feed):
        super().__init__()
        self.feed = feed


def clear_box(box):
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def toast(widget, text, timeout=3):
    from gi.repository import Adw

    root = widget.get_root() if widget is not None else None
    overlay = getattr(root, "toasts", None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))


def confirm(parent, heading, body, confirm_label, callback, destructive=True):
    from gi.repository import Adw

    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", confirm_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response):
        if response == "ok":
            callback()

    dialog.connect("response", on_response)
    dialog.present(parent)
