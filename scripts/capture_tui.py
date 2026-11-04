#!/usr/bin/env python3
"""Capture the actual ncurses screen buffer as SVG (run inside a real PTY)."""
import argparse
import curses
import html
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptero.core import Cancelled
from ptero.cli import ENTRIES
from ptero.navigation import menu_items
from ptero.terminal_text import cell_width
from ptero.ui import UI

PALETTE = {0: ("#d0d0d0", "#1c1c1c"), 1: ("#87d7ff", "#1c1c1c"),
           2: ("#d0d0d0", "#1c1c1c"), 3: ("#8a8a8a", "#1c1c1c"),
           4: ("#000000", "#5fd7d7"), 5: ("#87d787", "#1c1c1c"),
           6: ("#ffd75f", "#1c1c1c"), 7: ("#ff5f5f", "#1c1c1c")}


def capture(screen, target):
    rows, columns = screen.window.getmaxyx()
    cell, line, margin = 9, 20, 12
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{columns * cell + 2 * margin}" height="{rows * line + 2 * margin}" role="img">',
                '<title>Pterodactyl Installer — actual ncurses screen capture</title>',
                '<rect width="100%" height="100%" fill="#1c1c1c"/>',
                '<g font-family="DejaVu Sans Mono,Consolas,monospace" font-size="15" xml:space="preserve">']
    for row in range(rows):
        text = screen.window.instr(row, 0).decode("utf-8", "replace")
        column = 0
        for character in text:
            size = cell_width(character)
            if column >= columns:
                break
            attribute = screen.window.inch(row, column)
            pair = curses.pair_number(attribute) if screen.colors else 0
            foreground, background = PALETTE.get(pair, PALETTE[0])
            if not screen.colors and attribute & curses.A_REVERSE:
                foreground, background = background, foreground
            x, y = margin + column * cell, margin + row * line
            if background != "#1c1c1c":
                elements.append(f'<rect x="{x}" y="{y}" width="{max(1, size) * cell}" height="{line}" fill="{background}"/>')
            if character != " ":
                elements.append(f'<text x="{x}" y="{y + 15}" fill="{foreground}">{html.escape(character)}</text>')
            column += size
    elements += ["</g>", "</svg>"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(elements) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("en", "es", "pt-BR"), default="es")
    parser.add_argument("--scene", choices=("overview", "database", "maintenance", "backups", "nodes", "remove", "password", "preview", "review", "update-incomplete"), default="overview")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ui = UI(False, args.language)
    with ui.session():
        if not ui.screen:
            raise SystemExit("A terminal of at least 60x18 with ncurses is required.")
        screen = ui.screen
        screen.dashboard = {"panel": "absent", "wings": "absent", "database": "absent", "credentials": False}
        refresh = screen.refresh
        def snapshot():
            refresh()
            capture(screen, args.output)
            curses.unget_wch("\x1b")
        screen.refresh = snapshot
        if args.scene == "update-incomplete":
            screen.dashboard = {"panel": "updating", "wings": "ready", "database": "ready", "credentials": True,
                                "interrupted": True, "rollback": True, "panel_version": "v2.0.0-beta.3",
                                "wings_version": "v1.13.4", "url": "https://panel.example.com"}
        groups = {"update-incomplete": "Maintenance", "database": "Game databases", "maintenance": "Maintenance", "backups": "Backups", "nodes": "Nodes", "remove": "Remove"}
        if args.scene in groups:
            screen.navigation = (groups[args.scene], 0)
        try:
            if args.scene == "preview":
                from ptero.preview import show
                show(ui)
            elif args.scene == "review":
                from ptero.preview import Simulation, TourUI
                ui.preview = True
                screen.begin()
                Simulation(TourUI(ui)).install_panel()
            elif args.scene == "password":
                screen.wizard = (3, 4, "Administrator and email")
                screen.prompt("Preview password (not saved)", secret=True, initial="Preview-only-123!")
            else:
                screen.select("Overview", menu_items(ENTRIES, screen.dashboard), grouped=True)
        except Cancelled:
            pass
    print(args.output)


if __name__ == "__main__":
    main()
