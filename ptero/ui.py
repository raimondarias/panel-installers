"""Responsive terminal cards, menus, prompts and cancellable progress animation."""

import contextlib
import getpass
import os
import shutil
import signal
import sys
import threading
import time

from .i18n import Translator
from .terminal_text import cell_width, wrap


class Literal(str):
    """Already-formatted user data: never translate credentials or addresses."""


class UI:
    def __init__(self, animate=True, language="en", plain=False):
        self.color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.animate = animate and self.color and os.environ.get("TERM") != "dumb"
        self.language = language
        self.tr = Translator(language)
        self.plain = plain
        self.screen = None
        self.preview = False

    @contextlib.contextmanager
    def session(self):
        """Restore terminal modes on normal exit, errors, Ctrl+C and hangup."""
        renderer = None
        if not self.plain and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb":
            size = shutil.get_terminal_size((80, 24))
            if size.columns >= 60 and size.lines >= 18:
                try:
                    from .tui import Terminal

                    renderer = Terminal(self)
                    renderer.start()
                except BaseException as exc:
                    # Initialization failure must never leave echo/cbreak altered.
                    if renderer is not None:
                        renderer.close()
                    if not isinstance(exc, Exception):
                        raise
                    renderer = None
                    self.say("Full-screen mode is unavailable. Using the numbered interface.", "muted")
        old_handlers = {}

        def interrupt(*_):
            raise KeyboardInterrupt

        try:
            if renderer:
                self.screen = renderer
            if threading.current_thread() is threading.main_thread():
                for name in ("SIGTERM", "SIGHUP"):
                    number = getattr(signal, name, None)
                    if number is not None:
                        old_handlers[number] = signal.signal(number, interrupt)
            yield self
        finally:
            self.screen = None
            if renderer:
                renderer.close()
            for number, handler in old_handlers.items():
                signal.signal(number, handler)

    def suspend(self):
        return self.screen.suspend() if self.screen else contextlib.nullcontext()

    def set_language(self, language):
        self.language, self.tr = language, Translator(language)

    def paint(self, text, color="36"):
        return f"\033[{color}m{text}\033[0m" if self.color else text

    def say(self, text, kind="info"):
        if self.screen:
            self.screen.record(
                self.tr(str(text)), style={"info": "text", "muted": "muted", "ok": "ok", "warn": "warn", "error": "error"}.get(kind, "text")
            )
            return
        colors = {"info": "36", "ok": "32", "warn": "33", "error": "31", "muted": "90"}
        print(self.paint(self.tr(str(text)), colors.get(kind, "36")), flush=True)

    def card(self, title, rows=(), subtitle=None):
        if self.screen:
            translated = [
                *([self.tr(subtitle)] if subtitle else []),
                *(str(row) if isinstance(row, Literal) else self.tr(str(row)) for row in rows),
            ]
            self.screen.record(title, translated)
            if title in ("Ready to install", "Save your generated password", "Game database host is ready"):
                self.screen.result(title, translated, can_cancel=title != "Game database host is ready")
            return
        width = max(24, min(shutil.get_terminal_size((80, 24)).columns - 4, 78))
        print(self.paint("╭" + "─" * width + "╮", "36"))
        lines = [self.tr(title)]
        if subtitle:
            lines += [self.tr(subtitle)]
        lines += [str(row) if isinstance(row, Literal) else self.tr(str(row)) for row in rows]
        for line in lines:
            for part in wrap(line, width - 4) or [""]:
                print(self.paint("│ ", "36") + part + " " * max(0, width - 2 - cell_width(part)) + self.paint(" │", "36"))
        print(self.paint("╰" + "─" * width + "╯", "36"), flush=True)

    def banner(self):
        self.card(
            "Pterodactyl Installer",
            [
                "Panel v2  ·  Wings  ·  Ubuntu / Debian",
                "Install. Configure. Maintain.",
            ],
            "A guided setup, from the first question to your first server.",
        )

    def step(self, number, total, title):
        if self.screen:
            self.screen.wizard = number, total, title
        count = round(number / total * 20)
        self.say(f"[{number}/{total}] " + self.tr(title))
        self.say("  " + "━" * count + "─" * (20 - count), "muted")

    def ask(self, label, default=None, validator=None, secret=False):
        from .core import Cancelled

        initial, error = None, ""
        while True:
            suffix = f" [{default}]" if default is not None and not secret else ""
            prompt = self.paint("  › ", "35") + self.tr(label) + suffix + ": "
            try:
                value = (
                    self.screen.prompt(label, default, secret, initial, error)
                    if self.screen
                    else (getpass.getpass if secret else input)(prompt)
                )
            except EOFError as exc:
                raise Cancelled("Input closed. The operation was not confirmed.") from exc
            if not secret:
                value = value.strip()
            if not value and default is not None:
                value = default
            if validator is None or validator(value):
                return value
            initial, error = value, "Invalid value. Please try again."
            if not self.screen:
                self.say(error, "warn")

    def yes(self, label, default=False):
        if self.screen:
            return self.choice(label, [(True, "Yes", ""), (False, "No", "")], 0 if default else 1)
        yes_letter = "y" if self.language == "en" else "s"
        default_value = yes_letter if default else "n"
        value = self.ask(
            self.tr(label) + f" ({yes_letter}/n)",
            default_value,
            lambda s: s.lower() in ("y", "yes", "s", "si", "sí", "sim", "n", "no", "não", "nao"),
        )
        return value.lower() in ("y", "yes", "s", "si", "sí", "sim")

    def confirm(self, phrase):
        from .core import Cancelled

        phrase = self.tr(phrase)
        prompt = self.tr("Type {phrase} to continue").format(phrase=phrase)
        if self.ask(prompt) != phrase:
            raise Cancelled("Operation cancelled.")

    def choice(self, title, options, default=0):
        """options = [(value, label, description), ...]. Keep numeric input accessible."""
        if self.screen:
            from .navigation import Item

            return self.screen.select(
                title, [Item(value, label, note, shortcut=str(index)) for index, (value, label, note) in enumerate(options, 1)], default
            )
        self.card(
            title,
            [f"{index}. {self.tr(label)}" + (f" — {self.tr(note)}" if note else "") for index, (_, label, note) in enumerate(options, 1)],
        )
        answer = self.ask("Choose an option", str(default + 1), lambda s: s.isdigit() and 1 <= int(s) <= len(options))
        return options[int(answer) - 1][0]

    @contextlib.contextmanager
    def busy(self, label):
        if self.screen:
            with self.screen.busy(label):
                yield
            return
        done = threading.Event()
        label = self.tr(label)

        def spin():
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            started, index = time.monotonic(), 0
            while not done.wait(0.09):
                width = max(20, shutil.get_terminal_size((80, 24)).columns - 15)
                text = f"{frames[index % len(frames)]} {label[:width]}  {int(time.monotonic() - started)}s"
                print("\r\033[2K" + self.paint(text, "36"), end="", flush=True)
                index += 1

        thread = threading.Thread(target=spin, daemon=True) if self.animate else None
        if thread:
            thread.start()
        else:
            self.say("→ " + label)
        try:
            yield
        finally:
            done.set()
            if thread:
                thread.join()
                print("\r\033[2K", end="", flush=True)
