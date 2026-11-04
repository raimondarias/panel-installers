"""Full-screen ncurses interface. All ncurses access is serialized by one lock."""

import contextlib
import curses
import os
import platform
import threading
import time

from . import __version__
from .navigation import GROUPS, Item, attention, status_label
from .terminal_text import cell_width, clip, wrap


class Terminal:
    def __init__(self, ui):
        self.ui = ui
        self.window = None
        self.lock = threading.RLock()
        self.events = []
        self.dashboard = None
        self.wizard = None
        self.started = time.monotonic()
        self.last_card = []
        self.colors = {}
        self.previous_mouse_mask = None
        self.previous_mouse_interval = None

    def start(self):
        self.window = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.window.keypad(True)
        self.window.timeout(80)
        with contextlib.suppress(curses.error):
            self.previous_mouse_interval = curses.mouseinterval(150)
            _, self.previous_mouse_mask = curses.mousemask(
                curses.BUTTON1_CLICKED | curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)
            )
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(100)
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        if curses.has_colors() and "NO_COLOR" not in os.environ:
            curses.start_color()
            with contextlib.suppress(curses.error):
                curses.use_default_colors()
            palette = (
                ((117, 234), (252, 234), (245, 234), (16, 80), (114, 234), (221, 234), (203, 234))
                if curses.COLORS >= 256
                else (
                    (curses.COLOR_CYAN, -1),
                    (curses.COLOR_WHITE, -1),
                    (curses.COLOR_WHITE, -1),
                    (curses.COLOR_BLACK, curses.COLOR_CYAN),
                    (curses.COLOR_GREEN, -1),
                    (curses.COLOR_YELLOW, -1),
                    (curses.COLOR_RED, -1),
                )
            )
            for number, (name, pair) in enumerate(zip(("accent", "text", "muted", "selected", "ok", "warn", "error"), palette), 1):
                curses.init_pair(number, *pair)
                self.colors[name] = curses.color_pair(number)
            self.window.bkgd(" ", self.colors["text"])

    def close(self):
        with self.lock:
            if self.window is not None:
                if self.previous_mouse_mask is not None:
                    with contextlib.suppress(curses.error):
                        curses.mousemask(self.previous_mouse_mask)
                if self.previous_mouse_interval is not None:
                    with contextlib.suppress(curses.error):
                        curses.mouseinterval(self.previous_mouse_interval)
                for restore in (lambda: self.window.keypad(False), curses.nocbreak, curses.echo, lambda: curses.curs_set(1), curses.endwin):
                    with contextlib.suppress(curses.error):
                        restore()
                self.window = None

    @contextlib.contextmanager
    def suspend(self):
        with self.lock:
            curses.def_prog_mode()
            curses.endwin()
        try:
            yield
        finally:
            with self.lock:
                curses.reset_prog_mode()
                self.window.keypad(True)
                self.window.clearok(True)

    def tr(self, text):
        return self.ui.tr(text)

    def put(self, row, column, text, style="text", width=None, bold=False):
        rows, columns = self.window.getmaxyx()
        if row < 0 or row >= rows or column < 0 or column >= columns - 1:
            return
        width = min(columns - column - 1, width if width is not None else columns)
        attribute = self.colors.get(style, 0) | (curses.A_BOLD if bold else 0)
        if style == "selected" and not self.colors:
            attribute |= curses.A_REVERSE
        with contextlib.suppress(curses.error):
            self.window.addstr(row, column, clip(text, width), attribute)

    def paragraph(self, row, column, text, width, limit=3, style="muted"):
        lines = wrap(text, width)
        for index, line in enumerate(lines[:limit]):
            self.put(row + index, column, line, style, width)
        return min(len(lines), limit)

    def frame(self, title, subtitle=""):
        self.window.erase()
        self.height, columns = self.window.getmaxyx()
        self.width = min(columns, 140)
        self.left = max(0, (columns - self.width) // 2)
        self.small = self.height < 18 or self.width < 60
        if self.small:
            self.put(1, 1, self.tr("Enlarge the terminal to at least 60 columns and 18 rows."), "warn")
            self.put(3, 1, self.tr("Esc / Q: back or exit"), "muted")
            return
        self.put(1, self.left + 2, "PTERODACTYL", "accent", bold=True)
        self.put(1, self.left + 15, "/ " + self.tr("Installer") + "  " + __version__, "text")
        self.put(
            1,
            self.left + self.width - 25,
            self.ui.language + "  ·  " + ("●" if not self.ui.animate else "◉●◉○"[int(time.monotonic() * 3) % 4]),
            "accent",
        )
        heading = (self.tr("PREVIEW") + " · " if getattr(self.ui, "preview", False) else "") + self.tr(title)
        self.put(2, self.left + 2, heading, "text", self.width - 4, True)
        self.put(3, self.left + 2, "─" * (self.width - 4), "muted")
        if self.wizard:
            number, total, name = self.wizard
            self.put(
                4, self.left + 2, self.tr("Step {number} of {total}").format(number=number, total=total) + " · " + self.tr(name), "accent"
            )
            bars = max(8, self.width - 6)
            filled = round(number * bars / total)
            self.put(5, self.left + 2, "━" * filled + "─" * (bars - filled), "accent")
        elif self.dashboard:
            size = (self.width - 6) // 3
            for index, (key, label) in enumerate((("panel", "Panel"), ("wings", "Wings"), ("database", "Game SQL"))):
                state = self.dashboard[key]
                text = (
                    ("● " if state == "ready" else "○ " if state == "absent" else "▲ ")
                    + self.tr(label)
                    + " · "
                    + self.tr(status_label(key, self.dashboard))
                )
                version = self.dashboard.get(key + "_version")
                if version and state != "absent" and cell_width(text + " · " + version) <= size - 1:
                    text += " · " + version
                self.put(
                    4, self.left + 2 + index * size, text, "ok" if state == "ready" else "muted" if state == "absent" else "warn", size - 1
                )
            hint = attention(self.dashboard)
            if hint:
                self.put(5, self.left + 2, self.tr(hint), "warn", self.width - 4)
            else:
                self.put(
                    5,
                    self.left + 2,
                    self.dashboard.get("url") or self.tr("New machine? Start with Install Panel and Wings."),
                    "muted",
                    self.width - 4,
                )
        else:
            self.put(4, self.left + 2, subtitle or self.tr("A guided setup for your Panel and game nodes."), "muted", self.width - 4)
            self.put(5, self.left + 2, platform.node(), "muted", self.width - 4)
        self.put(self.height - 3, self.left + 2, "─" * (self.width - 4), "muted")

    def footer(self, text):
        translated = self.tr(text)
        if cell_width(translated) > self.width - 4:
            translated = (
                self.tr("↑↓ · Click · Enter · Esc")
                if "Click" in text
                else "↑↓ · Tab · Enter · / · Esc"
                if "Groups" in text
                else "Enter · Esc"
            )
        self.put(self.height - 2, self.left + 2, translated, "muted", self.width - 4)

    def refresh(self):
        self.window.noutrefresh()
        curses.doupdate()

    def key(self):
        with self.lock:
            try:
                key = self.window.get_wch()
                if key == curses.KEY_MOUSE:
                    _, x, y, _, buttons = curses.getmouse()
                    return ("mouse", x, y, buttons)
                return key
            except curses.error:
                return None

    def record(self, title, rows=(), style="text"):
        with self.lock:
            if title:
                self.events.append((self.tr(title), style))
            self.events.extend((str(row), style) for row in rows)
            self.events = self.events[-500:]
            if rows:
                self.last_card = [self.tr(title), *map(str, rows)]

    def begin(self):
        self.events.clear()
        self.last_card.clear()
        self.wizard = None

    def select(self, title, items, default=0, grouped=False):
        from .core import Cancelled

        category = items[default].group if grouped else ""
        selection, scroll, digits, query, warning = default, 0, "", "", ""
        if grouped and hasattr(self, "navigation"):
            category, selection = self.navigation
        while True:
            visible = [
                item
                for item in items
                if ((not grouped or item.group == category) and not query)
                or (query and query.casefold() in (self.tr(item.label) + " " + self.tr(item.note)).casefold())
            ]
            if not visible:
                visible = [Item(None, "No matching actions", enabled=False)]
            selection = max(0, min(selection, len(visible) - 1))
            with self.lock:
                self.frame(title)
                if not self.small:
                    sidebar = grouped and self.width >= 95
                    x = self.left + (27 if sidebar else 2)
                    width = self.width - (29 if sidebar else 4)
                    top = 8
                    if grouped:
                        if sidebar:
                            for index, name in enumerate(GROUPS):
                                self.put(
                                    8 + index * 2,
                                    self.left + 2,
                                    self.tr(name),
                                    "accent" if name == category and not query else "muted",
                                    22,
                                    name == category,
                                )
                            for row in range(7, self.height - 3):
                                self.put(row, self.left + 25, "│", "muted")
                        else:
                            self.put(7, x, "‹ " + self.tr(category) + " ›", "accent", width, True)
                            top = 9
                    capacity = max(1, self.height - top - 8)
                    scroll = max(0, min(scroll, selection))
                    if selection >= scroll + capacity:
                        scroll = selection - capacity + 1
                    for offset, item in enumerate(visible[scroll : scroll + capacity]):
                        index = scroll + offset
                        prefix = "› " if index == selection else "  "
                        prefix += (item.shortcut + "  ") if item.shortcut else ""
                        text = prefix + self.tr(item.label)
                        if item.reason:
                            text += "  · " + self.tr("Setup required")
                        elif not item.enabled:
                            text += "  · " + self.tr("Unavailable")
                        style = "selected" if index == selection else "text" if item.enabled else "muted"
                        self.put(top + offset, x, text + " " * max(0, width - cell_width(text)), style, width)
                    chosen = visible[selection]
                    self.put(
                        self.height - 7,
                        x,
                        self.tr("Before you continue") if chosen.reason else self.tr("Next step"),
                        "warn" if chosen.reason else "accent",
                        width,
                        True,
                    )
                    self.paragraph(
                        self.height - 6,
                        x,
                        self.tr(warning or chosen.reason or chosen.note),
                        width,
                        2,
                        "warn" if warning or chosen.reason else "muted",
                    )
                    if query or digits:
                        self.put(6, x, (self.tr("Search") + ": " + query) if query else self.tr("Option") + ": " + digits, "accent", width)
                    self.footer("↑↓ / Click Choose · Tab Groups · / Search · Esc Back" if grouped else "↑↓ / Click Choose · Esc Back")
                self.refresh()
            key = self.key()
            if key is None or key == curses.KEY_RESIZE:
                continue
            if key in ("\x1b", "\x04", "q", "Q"):
                if query or digits:
                    query, digits, warning = "", "", ""
                    continue
                raise Cancelled("Operation cancelled.")
            warning = ""
            if self.small:
                continue
            if isinstance(key, tuple) and key[0] == "mouse":
                _, mouse_x, mouse_y, buttons = key
                if buttons & (curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)):
                    selection = max(0, min(len(visible) - 1, selection + (-1 if buttons & curses.BUTTON4_PRESSED else 1)))
                    digits = ""
                    continue
                if not buttons & curses.BUTTON1_CLICKED:
                    continue
                if sidebar and self.left + 2 <= mouse_x < self.left + 24:
                    index = (mouse_y - 8) // 2
                    if mouse_y >= 8 and (mouse_y - 8) % 2 == 0 and index < len(GROUPS):
                        category = GROUPS[index]
                        selection, scroll, query, digits = 0, 0, "", ""
                    continue
                if grouped and not sidebar and mouse_y == 7:
                    label_end = x + cell_width("‹ " + self.tr(category) + " ›") - 1
                    if mouse_x in (x, label_end):
                        index = GROUPS.index(category)
                        category = GROUPS[(index + (-1 if mouse_x == x else 1)) % len(GROUPS)]
                        selection, scroll, query, digits = 0, 0, "", ""
                    continue
                index = scroll + mouse_y - top
                if not (x <= mouse_x < x + width and top <= mouse_y < top + capacity and index < len(visible)):
                    continue
                selection, digits = index, ""
                key = "\n"  # Use the same prerequisite checks as keyboard selection.
            if key in (curses.KEY_DOWN, "j"):
                selection = (selection + 1) % len(visible)
                digits = ""
            elif key in (curses.KEY_UP, "k"):
                selection = (selection - 1) % len(visible)
                digits = ""
            elif key in (curses.KEY_HOME,):
                selection = 0
            elif key in (curses.KEY_END,):
                selection = len(visible) - 1
            elif grouped and key == curses.KEY_F5:
                return "refresh"
            elif grouped and key in ("\t", curses.KEY_RIGHT, curses.KEY_LEFT):
                index = GROUPS.index(category)
                category = GROUPS[(index + (-1 if key == curses.KEY_LEFT else 1)) % len(GROUPS)]
                selection, scroll, query, digits = 0, 0, "", ""
            elif key == "/" and grouped:
                try:
                    query = self.prompt("Search actions", "", False)
                except Cancelled:
                    query = ""
                selection, scroll, digits = 0, 0, ""
            elif isinstance(key, str) and key.isascii() and key.isdigit():
                digits = (digits + key)[-3:]
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                digits = digits[:-1]
            elif key in ("\n", "\r", curses.KEY_ENTER):
                chosen = next((item for item in items if item.shortcut == digits), None) if digits else visible[selection]
                digits = ""
                if chosen is None:
                    warning = "Choose one of the displayed options."
                elif not chosen.enabled:
                    warning = chosen.reason or "This action is unavailable."
                    if grouped and chosen in items:
                        category = chosen.group
                        selection = [item for item in items if item.group == category].index(chosen)
                        query = ""
                else:
                    if grouped:
                        self.navigation = chosen.group, [item for item in items if item.group == chosen.group].index(chosen)
                    return chosen.value

    def prompt(self, label, default=None, secret=False, initial=None, error=""):
        from .core import Cancelled

        value = initial if initial is not None else ""
        cursor = len(value)
        try:
            while True:
                with self.lock:
                    self.frame("Your configuration")
                    if not self.small:
                        x, width = self.left + 3, self.width - 6
                        self.paragraph(8, x, self.tr(label), width, 2, "text")
                        if default is not None and not secret:
                            self.put(11, x, self.tr("Default: {value}").format(value=default), "muted", width)
                        display = "•" * len(value) if secret else value
                        before = "•" * cursor if secret else value[:cursor]
                        offset = max(0, cell_width(before) - width + 3)
                        visible = display
                        removed = 0
                        while offset > 0 and visible:
                            offset -= cell_width(visible[0])
                            removed += cell_width(visible[0])
                            visible = visible[1:]
                        self.put(13, x, " " * width, "selected", width)
                        self.put(13, x, visible or ("" if secret else self.tr("Type here")), "selected", width)
                        self.put(
                            min(15, self.height - 4),
                            x,
                            self.tr(error or ("Your input is hidden." if secret else "Your choices are reviewed before installation.")),
                            "warn" if error else "muted",
                            width,
                        )
                        with contextlib.suppress(curses.error):
                            curses.curs_set(1)
                            self.window.move(13, min(x + cell_width(before) - removed, x + width - 1))
                        self.footer("Enter Continue · Esc Cancel · Ctrl+U Clear")
                    self.refresh()
                key = self.key()
                if key is None or key == curses.KEY_RESIZE:
                    continue
                if key in ("\x1b", "\x04"):
                    raise Cancelled("Operation cancelled.")
                if self.small:
                    continue
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value if value or default is None else str(default)
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
                    value, cursor = value[: cursor - 1] + value[cursor:], cursor - 1
                elif key == curses.KEY_DC:
                    value = value[:cursor] + value[cursor + 1 :]
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key == curses.KEY_HOME:
                    cursor = 0
                elif key == curses.KEY_END:
                    cursor = len(value)
                elif key == "\x15":
                    value, cursor = "", 0
                elif isinstance(key, str) and key.isprintable() and len(value) < 4096:
                    value, cursor = value[:cursor] + key + value[cursor:], cursor + len(key)
        finally:
            with contextlib.suppress(curses.error):
                curses.curs_set(0)

    def result(self, title="Operation complete", rows=None, can_cancel=False):
        from .core import Cancelled

        offset = 0
        while True:
            with self.lock:
                self.frame(title)
                if not self.small:
                    width = self.width - 6
                    entries = self.events if rows is None else [(str(row), "text") for row in rows]
                    lines = [(line, style) for text, style in entries for line in wrap(text, width)]
                    capacity = max(1, self.height - 11)
                    offset = max(0, min(offset, max(0, len(lines) - capacity)))
                    for row, (line, style) in enumerate(lines[offset : offset + capacity], 8):
                        self.put(row, self.left + 3, line, style, width)
                    self.footer("↑↓ Scroll · Enter / Esc Back")
                self.refresh()
            key = self.key()
            if key in ("\n", "\r", curses.KEY_ENTER, "\x1b", "q", "Q"):
                if can_cancel and key in ("\x1b", "q", "Q"):
                    raise Cancelled("Operation cancelled.")
                return
            if key == curses.KEY_DOWN:
                offset += 1
            elif key == curses.KEY_UP:
                offset -= 1
            elif key == curses.KEY_NPAGE:
                offset += 8
            elif key == curses.KEY_PPAGE:
                offset -= 8
            elif key == curses.KEY_END:
                offset = 10**9
            elif key == curses.KEY_HOME:
                offset = 0

    @contextlib.contextmanager
    def busy(self, label):
        stop = threading.Event()
        started = time.monotonic()

        def render():
            while not stop.is_set():
                with self.lock:
                    self.frame("Working")
                    if not self.small:
                        frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10] if self.ui.animate else "→"
                        self.paragraph(8, self.left + 3, frame + "  " + self.tr(label), self.width - 6, 3, "accent")
                        self.put(12, self.left + 3, self.tr("Elapsed: {seconds}s").format(seconds=int(time.monotonic() - started)), "muted")
                        capacity = max(0, self.height - 18)
                        for row, (line, style) in enumerate(self.events[-capacity:] if capacity else [], 15):
                            self.put(row, self.left + 3, line, style, self.width - 6)
                        self.footer("Please wait. Ctrl+C interrupts the current operation.")
                    self.refresh()
                stop.wait(0.12 if self.ui.animate else 0.5)

        thread = threading.Thread(target=render, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
