"""Statistics: time played per podcast, per month, and storage per podcast."""

import math
from datetime import date

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk, Pango

from ...i18n import _, ngettext
from ...util import days_ago, fmt_long_duration, fmt_month, fmt_size
from ..widgets.common import Cover, EmptyState, clear_box, confirm, label, wrap_box
from ..widgets.chapterbar import _rgba
from .base import BasePage

PERIODS = [("all", _("All time")), ("year", _("Past year")), ("month", _("Past 30 days"))]


def month_label(key):
    _year, month = key.split("-")
    return fmt_month(0, int(month), long=False)


def nice_ceiling(value):
    if value <= 0:
        return 1
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exponent:
            return step * exponent
    return 10 * exponent


class MonthlyChart(Gtk.Widget):
    """One series, so one colour: the theme's accent. Hours per month, last 24 months."""

    __gtype_name__ = "OmaPodMonthlyChart"

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.months = []
        self.hover = None
        self.set_size_request(280, 220)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self._set_hover(self._index_at(x)))
        motion.connect("leave", lambda _c: self._set_hover(None))
        self.add_controller(motion)
        self.update_property([Gtk.AccessibleProperty.LABEL], [_("Time played per month")])

    def set_data(self, months):
        self.months = months
        self.queue_draw()

    def _layout(self):
        width, height = self.get_width(), self.get_height()
        left, bottom, top = 44, 22, 18
        plot_w = max(1, width - left - 8)
        plot_h = max(1, height - bottom - top)
        count = max(1, len(self.months))
        band = plot_w / count
        bar = min(24, max(4, band - 2))
        return left, top, plot_w, plot_h, band, bar

    def _index_at(self, x):
        if not self.months:
            return None
        left, _top, plot_w, _h, band, _bar = self._layout()
        index = int((x - left) // band)
        return index if 0 <= index < len(self.months) else None

    def _set_hover(self, index):
        if index != self.hover:
            self.hover = index
            self.queue_draw()

    def do_snapshot(self, snapshot):
        if not self.months:
            return
        values = self.theme.values
        bar_color = _rgba(values.get("accent_text", "#888888"))
        hover_color = _rgba(values.get("fg", "#888888"))
        grid = _rgba(values.get("fg", "#888888"), 0.12)
        text = _rgba(values.get("fg2", "#888888"))
        left, top, plot_w, plot_h, band, bar = self._layout()
        hours = [ms / 3600000 for _key, ms in self.months]
        ceiling = nice_ceiling(max(hours) if hours else 1)
        context = self.get_pango_context()
        font = context.get_font_description()
        font = font.copy() if font else Pango.FontDescription()
        font.set_size(max(7, int((font.get_size() or 9 * Pango.SCALE) / Pango.SCALE * 0.85)) * Pango.SCALE)

        def text_layout(string):
            layout = Pango.Layout.new(context)
            layout.set_font_description(font)
            layout.set_text(string, -1)
            return layout

        def text_at(string, x, y, align="left"):
            layout = text_layout(string)
            width, height = layout.get_pixel_size()
            if align == "right":
                x -= width
            elif align == "center":
                x -= width / 2
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x, y - height / 2))
            snapshot.append_layout(layout, text)
            snapshot.restore()

        for step in range(0, 5):
            value = ceiling * step / 4
            y = top + plot_h - plot_h * step / 4
            snapshot.append_color(grid, Graphene.Rect().init(left, round(y), plot_w, 1))
            label_text = f"{value:g} h" if value < 10 else f"{int(value)} h"
            text_at(label_text, left - 6, y, "right")
        radius = min(4, int(str(values.get("radius", "0px")).rstrip("px") or 0))
        peak = max(range(len(hours)), key=lambda i: hours[i]) if hours else None
        # As many month names as fit, counted back from the newest, which always shows.
        names = [month_label(key) for key, _ms in self.months]
        every = math.ceil((max(text_layout(name).get_pixel_size()[0] for name in names) + 6) / band)
        for index, value in enumerate(hours):
            x = left + index * band + (band - bar) / 2
            height = plot_h * value / ceiling if ceiling else 0
            if value > 0:
                height = max(height, 2)
                rect = Graphene.Rect().init(x, top + plot_h - height, bar, height)
                color = hover_color if index == self.hover else bar_color
                if radius:
                    rounded = Gsk_rounded(rect, radius)
                    snapshot.push_rounded_clip(rounded)
                    snapshot.append_color(color, Graphene.Rect().init(x, top + plot_h - height, bar, height + radius))
                    snapshot.pop()
                else:
                    snapshot.append_color(color, rect)
            if (len(hours) - 1 - index) % every == 0:
                text_at(names[index], x + bar / 2, top + plot_h + 12, "center")
        if peak is not None and hours[peak] > 0:
            x = left + peak * band + band / 2
            y = top + plot_h - plot_h * hours[peak] / ceiling - 9
            text_at(fmt_long_duration(self.months[peak][1]), x, max(8, y), "center")

    def _tooltip(self, _widget, x, _y, _keyboard, tooltip):
        index = self._index_at(x)
        if index is None:
            return False
        key, ms = self.months[index]
        year, month = key.split("-")
        tooltip.set_text(f"{fmt_month(int(year), int(month))}: {fmt_long_duration(ms)}")
        return True


def Gsk_rounded(rect, radius):
    from gi.repository import Gsk

    rounded = Gsk.RoundedRect()
    size = Graphene.Size().init(radius, radius)
    zero = Graphene.Size().init(0, 0)
    rounded.init(rect, size, size, zero, zero)
    return rounded


class StatRow(Gtk.Box):
    """Podcast, a thin bar proportional to the leader, and the value at its tip."""

    def __init__(self, ctx, title, image, value_text, fraction, detail=""):
        super().__init__(spacing=12, css_classes=["omp-row"])
        cover = Cover(ctx.textures, 40)
        cover.set_source(image, title)
        self.append(cover)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        head = Gtk.Box(spacing=8)
        name = label(title, ("omp-row-title",))
        name.set_hexpand(True)
        head.append(name)
        head.append(label(value_text, ("omp-numeric",), xalign=1.0, ellipsize=False))
        box.append(head)
        bar = Gtk.ProgressBar(fraction=max(0.0, min(1.0, fraction)), css_classes=["omp-stat-bar"])
        box.append(bar)
        if detail:
            box.append(label(detail, ("omp-row-meta",)))
        self.append(box)


class StatisticsPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Statistics"), "statistics")
        self.period = "all"
        self._months = None
        self._shown = {}  # list box -> what it shows
        from .lists import _menu

        self.add_menu(_menu(self, [(_("Reset statistics data"), self._reset)]))
        self.stack = Adw.ViewStack(vexpand=True)
        switcher_type = getattr(Adw, "InlineViewSwitcher", None)
        switcher = switcher_type(stack=self.stack) if switcher_type else Adw.ViewSwitcher(stack=self.stack)
        switcher.set_halign(Gtk.Align.START)
        switcher.set_margin_start(24)
        switcher.set_margin_bottom(8)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(switcher)
        box.append(self.stack)
        self.set_content(box)

        # Subscriptions
        subs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls = wrap_box(css_classes=["omp-toolbar"])
        group = None
        for key, text in PERIODS:
            chip = Gtk.ToggleButton(label=text, css_classes=["omp-chip"], active=key == self.period,
                                    focus_on_click=False)
            if group is None:
                group = chip
            else:
                chip.set_group(group)
            chip.connect("toggled", self._on_period, key)
            controls.append(chip)
        controls.append(Gtk.Box(hexpand=True))
        # The built-in label cannot wrap. With one of our own the button loses its style class and accessible name.
        include_text = _("Include episodes that are only marked as played")
        include = Gtk.CheckButton(child=label(include_text, wrap=True),
                                  active=bool(ctx.settings.get("stats_include_marked")))
        include.add_css_class("text-button")
        include.update_property([Gtk.AccessibleProperty.LABEL], [include_text])
        include.connect("toggled", lambda b: (ctx.settings.set("stats_include_marked", b.get_active()),
                                              self.refresh()))
        controls.append(include)
        subs.append(controls)
        self.total = label("", ("omp-stat-total",), wrap=True)
        self.total.set_margin_start(24)
        self.total.set_margin_end(24)
        subs.append(self.total)
        self.total_note = label("", ("omp-dim",), wrap=True)
        self.total_note.set_margin_start(24)
        self.total_note.set_margin_end(24)
        subs.append(self.total_note)
        self.sub_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(self.sub_list)
        subs.append(scrolled)
        self.stack.add_titled_with_icon(subs, "subscriptions", _("Subscriptions"), "view-grid-symbolic")

        # Months
        months = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        months.set_margin_start(24)
        months.set_margin_end(24)
        months.append(label(_("Time played per month"), ("omp-heading",)))
        self.chart = MonthlyChart(ctx.theme)
        months.append(self.chart)
        self.table_toggle = Gtk.ToggleButton(label=_("Show as table"), css_classes=["flat"],
                                             halign=Gtk.Align.START)
        self.table_toggle.connect("toggled", lambda b: self.table.set_visible(b.get_active()))
        months.append(self.table_toggle)
        self.table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.table.set_visible(False)
        months.append(self.table)
        self.months_empty = label(_("Listen to something and your months will show up here."), ("omp-dim",))
        months.append(self.months_empty)
        months_scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        months_scroller.set_child(months)
        self.stack.add_titled_with_icon(months_scroller, "months", _("Years"), "omapod-statistics-symbolic")

        # Downloads
        downloads = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.space_total = label("", ("omp-stat-total",), wrap=True)
        self.space_total.set_margin_start(24)
        self.space_total.set_margin_end(24)
        downloads.append(self.space_total)
        self.space_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        space_scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        space_scroller.set_child(self.space_list)
        downloads.append(space_scroller)
        self.stack.add_titled_with_icon(downloads, "downloads", _("Downloads"), "folder-download-symbolic")

        self.watch(400)
        self.listen("theme-changed", lambda _t: self.chart.queue_draw())

    def _on_period(self, chip, key):
        if chip.get_active():
            self.period = key
            self.refresh()

    def refresh(self):
        library = self.ctx.library
        start = {"all": 0, "year": days_ago(365), "month": days_ago(30)}[self.period]
        stats = [s for s in library.statistics(start=start) if s["episodes"]]
        total = sum(s["played"] for s in stats)
        self.total.set_text(fmt_long_duration(total))
        since = {"all": _("Played in total"), "year": _("Played in the past year"),
                 "month": _("Played in the past 30 days")}[self.period]
        self.total_note.set_text(since + "  ·  " + _("Playback speed is not taken into account."))
        self._fill(self.sub_list, [s for s in stats if s["played"] > 0], lambda s: s["played"],
                   lambda s: fmt_long_duration(s["played"]),
                   lambda s: ngettext("{started} of {total} episode started", "{started} of {total} episodes started",
                                      s["episodes"]).format(started=s["started"], total=s["episodes"]),
                   _("No listening time yet"))
        months = library.monthly_statistics()
        months = self._last_24(months)
        self.chart.set_data(months)
        has_months = any(ms for _k, ms in months)
        self.chart.set_visible(has_months)
        self.table_toggle.set_visible(has_months)
        self.months_empty.set_visible(not has_months)
        if months != self._months:
            self._months = months
            clear_box(self.table)
            for key, ms in reversed(months):
                if not ms:
                    continue
                row = Gtk.Box(spacing=12, css_classes=["omp-chapter-row"])
                year, month = key.split("-")
                name = label(fmt_month(int(year), int(month)))
                name.set_hexpand(True)
                row.append(name)
                row.append(label(fmt_long_duration(ms), ("omp-numeric",), ellipsize=False))
                self.table.append(row)
        space = [s for s in library.statistics() if s["space"] > 0]
        self.space_total.set_text(fmt_size(sum(s["space"] for s in space)) or "0 MB")
        self._fill(self.space_list, space, lambda s: s["space"], lambda s: fmt_size(s["space"]),
                   lambda s: ngettext("{n} episode downloaded", "{n} episodes downloaded", s["downloaded"]).format(
                       n=s["downloaded"]), _("No downloads"))

    def _fill(self, box, rows, value, text, detail, empty_text):
        rows = sorted(rows, key=value, reverse=True)
        shown = [(row["feed_id"], row["title"], row["image_url"], text(row), value(row), detail(row)) for row in rows]
        if shown == self._shown.get(box):
            return
        self._shown[box] = shown
        clear_box(box)
        if not rows:
            empty = label(empty_text, ("omp-dim",))
            empty.set_margin_start(24)
            empty.set_margin_top(12)
            box.append(empty)
            return
        top = value(rows[0]) or 1
        for row in rows:
            widget = StatRow(self.ctx, row["title"] or "", row["image_url"], text(row), value(row) / top, detail(row))
            click = Gtk.GestureClick()
            click.connect("released", lambda *_a, fid=row["feed_id"]: self.ctx.show_feed(fid))
            widget.add_controller(click)
            widget.set_cursor_from_name("pointer")
            box.append(widget)

    @staticmethod
    def _last_24(months):
        data = dict(months)
        today = date.today()
        result = []
        year, month = today.year, today.month
        for _i in range(24):
            key = f"{year:04d}-{month:02d}"
            result.append((key, data.get(key, 0)))
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        result.reverse()
        first = next((i for i, (_k, ms) in enumerate(result) if ms), len(result) - 12)
        return result[max(0, min(first, len(result) - 12)):]

    def _reset(self):
        confirm(self.ctx.window, _("Reset statistics data?"),
                _("This will erase the history of duration played for all episodes. Are you sure you want to "
                  "proceed?"), _("Reset"), lambda: (self.ctx.library.reset_statistics(), self.refresh()))
