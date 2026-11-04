"""Navigation contracts plus real ncurses keyboard, resize and restoration tests."""
import contextlib
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from unittest.mock import patch

from test_installer import BaseTest, QuietUI
import installer as app
from ptero.cli import ENTRIES
from ptero.navigation import Item, inventory, menu_items, prerequisite, prepare_action
from ptero.terminal_text import cell_width, clip
from ptero.tui import Terminal

ROOT = Path(__file__).resolve().parents[1]


class NavigationTests(BaseTest):
    def test_new_machine_actions_are_selectable_and_explain_prerequisites(self):
        state = inventory(self.installer)
        self.assertEqual(state["panel"], "absent")
        items = {item.value: item for item in menu_items(ENTRIES, state)}
        for action in ("install-all", "install-panel", "install-wings", "diagnose", "restore-panel", "firewall", "language", "exit"):
            self.assertTrue(items[action].enabled, action)
        for action in ("configure-database", "smtp", "admin", "update-panel", "configure-wings", "restart", "logs", "database-credentials"):
            self.assertTrue(items[action].enabled, action)
            self.assertTrue(items[action].reason, action)
        self.assertEqual(self.runner.calls, [])

    def test_reproduced_option_20_never_dispatches_or_reads_missing_state(self):
        self.ui.answers = ["20", "0"]
        with patch.object(self.installer, "configure_game_database") as configure, contextlib.redirect_stdout(io.StringIO()):
            self.installer.menu()
        configure.assert_not_called()
        messages = "\n".join(text for text, _ in self.ui.messages)
        self.assertIn("Install the Panel first", messages)
        self.assertNotIn("state.json", messages)
        self.assertNotIn("JSON", messages)

    def test_guided_installation_requires_explicit_consent_and_rechecks_state(self):
        state = inventory(self.installer)
        with patch.object(self.ui, "yes", return_value=False), patch.object(self.installer, "install_panel") as setup:
            self.assertFalse(prepare_action(self.ui, "smtp", state, lambda _: setup(), lambda: state))
        setup.assert_not_called()
        def installed(_):
            self.installer.save_panel(self.state)
        with patch.object(self.ui, "yes", return_value=True):
            self.assertTrue(prepare_action(self.ui, "smtp", state, installed, lambda: inventory(self.installer)))

    def test_removal_on_empty_machine_never_offers_installation(self):
        state = inventory(self.installer)
        for action in ("uninstall-panel", "uninstall-wings", "uninstall-all"):
            with self.subTest(action=action), patch.object(self.ui, "yes", return_value=True) as question, \
                 patch.object(self.installer, "install_panel") as setup:
                self.assertFalse(prepare_action(self.ui, action, state, lambda _: setup(), lambda: state))
            question.assert_not_called()
            setup.assert_not_called()

    def test_schedule_requires_complete_panel_but_supports_wings_only(self):
        state = inventory(self.installer)
        state.update(panel="installing", backup_profile="ready")
        for action in ("backup-schedule", "backup-run"):
            self.assertIn("Finish or resume", prerequisite(action, state))
        state.update(panel="absent", wings="pending")
        for action in ("backup-schedule", "backup-run"):
            self.assertEqual(prerequisite(action, state), "")

    def test_guided_setup_cancellation_never_runs_the_original_operation(self):
        self.ui.answers = ["17", "0"]
        with patch.object(self.ui, "yes", return_value=True), \
             patch.object(self.installer, "install_panel", side_effect=app.Cancelled()), \
             patch.object(self.installer, "mail") as mail, contextlib.redirect_stdout(io.StringIO()):
            self.installer.menu()
        mail.assert_not_called()

    def test_missing_backup_profile_and_resume_have_accurate_requirements(self):
        state = inventory(self.installer)
        self.assertIn("Configure scheduled", prerequisite("backup-run", state))
        self.assertIn("Configure scheduled", prerequisite("backup-download", state))
        self.assertIn("No interrupted", prerequisite("resume", state))
        self.assertEqual(prerequisite("health", state), "")
        self.assertEqual(prerequisite("diagnose", state), "")
        state.update(wings="ready", backup_profile="ready")
        self.assertEqual(prerequisite("backup-schedule", state), "")
        self.assertEqual(prerequisite("backup-run", state), "")

    def test_retained_shortcuts_are_stable_and_game_backup_actions_are_removed(self):
        items = {item.value: item for item in menu_items(ENTRIES, inventory(self.installer))}
        for action, shortcut in {"language": "23", "create-node": "24", "backup-schedule": "28", "backup-run": "29",
                                  "backup-download": "30", "health": "31", "resume": "32", "remote-node": "33", "remote-database": "34"}.items():
            self.assertEqual(items[action].shortcut, shortcut)
        self.assertFalse({"22", "25", "26", "27"} & {item.shortcut for item in items.values()})
        self.assertFalse({"backup-game-database", "restore-game-database", "backup-games", "restore-games"} & items.keys())

    def test_foreign_state_and_unmanaged_files_are_never_adopted_by_guided_setup(self):
        self.paths.state.parent.mkdir(parents=True)
        self.paths.state.write_text(json.dumps({**self.state, "owner": "another-installer"}))
        state = inventory(self.installer)
        self.assertEqual(state["panel"], "foreign")
        self.assertIn("another installer", prerequisite("update-panel", state))
        with patch.object(self.ui, "yes") as question, patch.object(self.installer, "install_panel") as setup:
            self.assertFalse(prepare_action(self.ui, "smtp", state, lambda _: setup(), lambda: state))
        question.assert_not_called()
        setup.assert_not_called()
        self.paths.state.unlink()
        external = self.paths.root / "var/www/pterodactyl/artisan"
        external.parent.mkdir(parents=True)
        external.write_text("existing external Panel")
        state = inventory(self.installer)
        self.assertEqual(state["panel"], "unmanaged")
        self.assertIn("without managed state", prerequisite("install-panel", state))
        self.assertEqual(external.read_text(), "existing external Panel")
        self.assertEqual(self.runner.calls, [])

    def test_direct_action_guides_setup_and_never_reports_success_when_declined(self):
        from ptero.cli import main
        from unittest.mock import Mock
        original = Mock()
        setup = Mock(side_effect=lambda: self.installer.save_panel(self.state))
        actions = {"smtp": original, "install-panel": setup}
        with patch("ptero.core.Installer", return_value=self.installer), patch("ptero.cli.UI", return_value=self.ui), \
             patch("os.geteuid", return_value=0), patch.object(self.installer, "actions", return_value=actions):
            self.assertEqual(main(["--language", "en", "--plain", "smtp"]), 1)
            original.assert_not_called()
            setup.assert_not_called()
            with patch.object(self.ui, "yes", return_value=True):
                self.assertEqual(main(["--language", "en", "--plain", "smtp"]), 0)
        setup.assert_called_once()
        original.assert_called_once()
        self.assertEqual(self.runner.calls, [])

    def test_startup_migrates_owned_schedule_only_for_menu_or_mutating_actions(self):
        from ptero.cli import main
        self.installer.save_panel(self.state)
        app.write_json(self.paths.base / "backup-profile.json", {"schema": 1, "owner": app.PROJECT,
            "repository": "/private/backups", "backup_id": "ptero-test", "calendar": "daily"})
        for action in ("admin", "diagnose", "health", "firewall", "logs", "database-credentials", "backup-download"):
            with self.subTest(action=action), patch("ptero.core.Installer", return_value=self.installer), \
                 patch("ptero.cli.UI", return_value=self.ui), patch("os.geteuid", return_value=0), \
                 patch.object(self.installer, "actions", return_value={action: lambda: None}), \
                 patch("ptero.backups.ScheduledBackups.migrate") as migrate, \
                 patch("ptero.navigation.prepare_action", return_value=True):
                self.assertEqual(main(["--language", "en", "--plain", action]), 0)
                self.assertEqual(migrate.call_count, 1 if action == "admin" else 0)

    def test_direct_missing_panel_has_an_actionable_error(self):
        with self.assertRaisesRegex(app.InstallerError, "Install the Panel first"):
            self.installer.configure_game_database()
        self.assertEqual(self.runner.calls, [])

    def test_fresh_diagnostics_give_a_next_step_without_service_commands(self):
        self.installer.diagnostics()
        messages = "\n".join(text for text, _ in self.ui.messages)
        self.assertIn("Ready for your first installation", messages)
        self.assertIn("Next: choose", messages)
        self.assertIn("Architecture:", messages)
        self.assertNotIn("state.json", messages)
        self.assertEqual(self.runner.calls, [])

    def test_configured_and_incomplete_states_are_distinguished(self):
        self.installer.save_panel(self.state)
        state = inventory(self.installer)
        self.assertFalse(prerequisite("configure-database", state))
        self.state["status"] = "installing"
        self.installer.save_panel(self.state)
        self.assertIn("resume", prerequisite("configure-database", inventory(self.installer)))
        self.assertIn("resume", prerequisite("backup-panel", inventory(self.installer)))

    def test_corrupt_state_is_a_dashboard_condition_and_is_not_overwritten(self):
        self.paths.state.parent.mkdir(parents=True)
        self.paths.state.write_text("{broken")
        state = inventory(self.installer)
        self.assertEqual(state["panel"], "invalid")
        self.assertIn("invalid or unreadable", prerequisite("configure-database", state))
        self.assertEqual(self.paths.state.read_text(), "{broken")

    def test_invalid_status_and_incomplete_backup_profile_are_not_ready(self):
        self.paths.state.parent.mkdir(parents=True)
        for invalid in ([], {}, None, "unexpected"):
            self.paths.state.write_text(json.dumps({**self.state, "status": invalid}))
            state = inventory(self.installer)
            self.assertEqual(state["panel"], "invalid")
            self.assertTrue(prerequisite("update-panel", state))
        app.write_json(self.paths.base / "backup-profile.json", {"schema": 1, "owner": app.PROJECT})
        self.assertEqual(inventory(self.installer)["backup_profile"], "invalid")

    def test_language_switch_defaults_to_the_current_language(self):
        from ptero.cli import select_language
        ui = app.UI(False, "pt-BR", plain=True)
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(select_language(ui), "pt-BR")

    def test_terminal_width_and_untrusted_control_characters(self):
        self.assertEqual(cell_width("Español"), 7)
        self.assertEqual(cell_width("界e\u0301"), 3)
        self.assertEqual(clip("界abc", 3), "界a")
        self.assertNotIn("\x1b", clip("user\x1b[2J", 20))
        ui = app.UI(False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ui.card("Español", ["Português", "界" * 60])
        lengths = [cell_width(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(set(lengths)), 1)


class Window:
    def __init__(self, keys, size=(28, 100)):
        self.keys, self.size, self.drawn = list(keys), size, []

    def getmaxyx(self):
        return self.size

    def erase(self):
        pass

    def addstr(self, row, column, text, attribute=0):
        assert row < self.size[0] and column + cell_width(text) < self.size[1]
        self.drawn.append(text)

    def noutrefresh(self):
        pass

    def move(self, *_):
        pass

    def get_wch(self):
        if not self.keys:
            raise AssertionError("Unexpected additional TUI input")
        key = self.keys.pop(0)
        if isinstance(key, tuple):
            self.size = key
            return curses.KEY_RESIZE
        return key


class WidgetTests(BaseTest):
    def terminal(self, keys, size=(28, 100)):
        terminal = Terminal(app.UI(False))
        terminal.window = Window(keys, size)
        self.addCleanup(patch.stopall)
        patch("ptero.tui.curses.doupdate").start()
        return terminal

    def test_arrow_navigation_and_numeric_shortcuts(self):
        for keys in ([curses.KEY_DOWN, "\n"], ["2", "\n"]):
            terminal = self.terminal(keys)
            result = terminal.select("Panel deployment", [Item("native", "Standalone", shortcut="1"), Item("docker", "Docker", shortcut="2")])
            self.assertEqual(result, "docker")

    def test_mouse_click_selects_options_and_sidebar_groups(self):
        terminal = self.terminal([curses.KEY_MOUSE])
        with patch("ptero.tui.curses.getmouse", return_value=(0, 8, 9, 0, curses.BUTTON1_CLICKED)):
            self.assertEqual(terminal.select("Panel deployment", [Item("native", "Standalone"), Item("docker", "Docker")]), "docker")
        items = [Item("install", "Install", group="Install"), Item("diagnose", "Diagnostics", group="Maintenance")]
        terminal = self.terminal([curses.KEY_MOUSE, curses.KEY_MOUSE])
        with patch("ptero.tui.curses.getmouse", side_effect=[(0, 4, 10, 0, curses.BUTTON1_CLICKED), (0, 35, 8, 0, curses.BUTTON1_CLICKED)]):
            self.assertEqual(terminal.select("Overview", items, grouped=True), "diagnose")

    def test_keyboard_and_mouse_select_actions_with_prerequisite_information(self):
        item = Item("update-panel", "Update Panel v2", reason="Install the Panel first", shortcut="7")
        for keys in (["7", "\n"], [curses.KEY_MOUSE]):
            terminal = self.terminal(keys)
            with patch("ptero.tui.curses.getmouse", return_value=(0, 8, 8, 0, curses.BUTTON1_CLICKED)):
                self.assertEqual(terminal.select("Maintenance", [item]), "update-panel")
            text = " ".join(terminal.window.drawn)
            self.assertIn("Setup required", text)
            self.assertIn("Install the Panel first", text)
            self.assertNotIn("Unavailable", text)

    def test_mouse_cannot_activate_disabled_options_or_empty_space(self):
        terminal = self.terminal([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_HOME, "\n"])
        with patch("ptero.tui.curses.getmouse", side_effect=[(0, 9, 9, 0, curses.BUTTON1_CLICKED), (0, 9, 20, 0, curses.BUTTON1_CLICKED)]):
            result = terminal.select("Overview", [Item("ok", "Install"), Item("blocked", "Database", enabled=False, reason="Install the Panel first")])
        self.assertEqual(result, "ok")
        self.assertTrue(any("Install the Panel first" in text for text in terminal.window.drawn))

    def test_mouse_wheel_and_compact_group_arrows(self):
        terminal = self.terminal([curses.KEY_MOUSE, "\n"])
        with patch("ptero.tui.curses.getmouse", return_value=(0, 8, 9, 0, curses.BUTTON5_PRESSED)):
            self.assertEqual(terminal.select("Options", [Item(1, "First"), Item(2, "Second")]), 2)
        terminal = self.terminal([curses.KEY_MOUSE, "\n"], size=(24, 80))
        with patch("ptero.tui.curses.getmouse", return_value=(0, 12, 7, 0, curses.BUTTON1_CLICKED)):
            self.assertEqual(terminal.select("Groups", [Item(1, "First", group="Install"), Item(2, "Second", group="Maintenance")], grouped=True), 2)

    def test_disabled_actions_explain_the_prerequisite_without_returning_it(self):
        terminal = self.terminal(["2", "\n", "1", "\n"])
        result = terminal.select("Overview", [Item("ok", "Install", shortcut="1"),
                                Item("blocked", "Database", enabled=False, reason="Install the Panel first", shortcut="2")])
        self.assertEqual(result, "ok")
        self.assertTrue(any("Install the Panel first" in text for text in terminal.window.drawn))

    def test_group_navigation_and_search(self):
        items = [Item("install", "Install Panel", group="Install"),
                 Item("diagnose", "Status and diagnostics", group="Maintenance")]
        terminal = self.terminal(["\t", "\n"])
        self.assertEqual(terminal.select("Overview", items, grouped=True), "diagnose")

    def test_refresh_and_numeric_navigation_preserve_the_actual_action_group(self):
        items = [Item("install", "Install Panel", group="Install", shortcut="1"),
                 Item("diagnose", "Status and diagnostics", group="Maintenance", shortcut="14")]
        terminal = self.terminal([curses.KEY_F5])
        self.assertEqual(terminal.select("Overview", items, grouped=True), "refresh")
        terminal = self.terminal(["1", "4", "\n"])
        self.assertEqual(terminal.select("Overview", items, grouped=True), "diagnose")
        self.assertEqual(terminal.navigation, ("Maintenance", 0))
        terminal = self.terminal(["/", *"diagnostics", "\n", "\n"])
        self.assertEqual(terminal.select("Overview", items, grouped=True), "diagnose")

    def test_escape_and_small_resized_terminal_can_cancel(self):
        for keys in (["\x1b"], [(10, 35), "q"]):
            terminal = self.terminal(keys)
            with self.assertRaises(app.Cancelled):
                terminal.select("Overview", [Item(1, "Install")])

    def test_unicode_editing_and_password_masking(self):
        terminal = self.terminal([*"abc", curses.KEY_LEFT, curses.KEY_BACKSPACE, "ñ", "\n"])
        self.assertEqual(terminal.prompt("Value"), "añc")
        terminal = self.terminal([*"Do-not-echo-this-123!", "\n"])
        self.assertEqual(terminal.prompt("Password", secret=True), "Do-not-echo-this-123!")
        self.assertNotIn("Do-not-echo", "".join(terminal.window.drawn))
        self.assertIn("•", "".join(terminal.window.drawn))

    def test_form_defaults_clear_and_cancel(self):
        terminal = self.terminal(["\n"])
        self.assertEqual(terminal.prompt("Domain", "panel.example.test"), "panel.example.test")
        terminal = self.terminal([*"incorrect", "\x15", *"correct", "\n"])
        self.assertEqual(terminal.prompt("Value"), "correct")
        terminal = self.terminal(["\x1b"])
        with self.assertRaises(app.Cancelled):
            terminal.prompt("Password", secret=True)

    def test_review_and_generated_password_are_acknowledged_before_continuing(self):
        ui = app.UI(False)
        terminal = self.terminal(["\n", "\n"])
        ui.screen = terminal
        ui.card("Ready to install", ["Selected plan"])
        ui.card("Save your generated password", ["Save-this-password-123!"])
        self.assertEqual(terminal.window.keys, [])
        self.assertIn("Save-this-password-123!", "".join(terminal.window.drawn))

    def test_initialization_failure_falls_back_after_restoring_terminal(self):
        ui = app.UI(False)
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm"}), patch("ptero.tui.Terminal.start", side_effect=RuntimeError("unsupported")), \
             patch("ptero.tui.Terminal.close") as close, patch.object(ui, "say"):
            with ui.session():
                self.assertIsNone(ui.screen)
        close.assert_called_once()


class PseudoTerminalTests(BaseTest):
    def spawn(self, code, env=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
        original = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm-256color", "LANG": "C.UTF-8", **(env or {})})
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(stop)
        self.addCleanup(lambda: os.close(master))
        self.addCleanup(lambda: os.close(slave))
        return process, master, slave, original

    def receive(self, process, master, marker, timeout=8):
        data = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                data.extend(os.read(master, 65536))
                if marker.encode() in data:
                    process.tui_output = getattr(process, "tui_output", b"") + bytes(data)
                    return bytes(data)
            elif process.poll() is not None:
                break
        self.fail("Missing PTY marker " + marker + ": " + data.decode("utf-8", "replace")[-3000:])

    def test_real_menu_guided_prerequisite_diagnostics_and_clean_exit(self):
        code = ("import tempfile;from pathlib import Path;from ptero.core import Installer,Paths;from ptero.ui import UI;"
                "root=tempfile.TemporaryDirectory();ui=UI(language='es');i=Installer(Paths(Path(root.name)),ui);\n"
                "with ui.session():\n assert ui.screen is not None\n i.menu()\n"
                "print('TERMINAL_RESTORED',flush=True)")
        process, master, slave, original = self.spawn(code)
        self.receive(process, master, "PTERO")
        os.write(master, b"20\r")
        self.receive(process, master, app.UI(False, "es").tr("Install the Panel now and return to this action?"))
        os.write(master, b"\r")  # Default No safely returns without installing.
        disabled = self.receive(process, master, "Instala el Panel")
        self.assertNotIn(b"state.json", disabled)
        os.write(master, b"\r")
        time.sleep(0.12)
        os.write(master, b"14\r")
        self.receive(process, master, "primera instalaci")
        os.write(master, b"\r")
        time.sleep(0.12)
        os.write(master, b"q")
        self.receive(process, master, "TERMINAL_RESTORED")
        self.assertEqual(process.wait(timeout=5), 0)
        restored = termios.tcgetattr(slave)
        for bit in (termios.ECHO, termios.ICANON):
            self.assertEqual(restored[3] & bit, original[3] & bit)

    def test_real_mouse_click_selects_choice_and_restores_terminal(self):
        code = ("from ptero.ui import UI;ui=UI();\nwith ui.session():\n"
                " value=ui.choice('Panel deployment',[('native','Standalone',''),('docker','Docker','')])\n"
                "print('MOUSE_RESULT='+value,flush=True)")
        process, master, slave, original = self.spawn(code)
        self.receive(process, master, "Panel deployment")
        os.write(master, b"\x1b[<0;10;10M\x1b[<0;10;10m")
        output = self.receive(process, master, "MOUSE_RESULT=docker")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertIn(b"1000l", output)
        restored = termios.tcgetattr(slave)
        for bit in (termios.ECHO, termios.ICANON):
            self.assertEqual(restored[3] & bit, original[3] & bit)

    def test_bootstrap_mouse_selects_spanish_and_opens_tui(self):
        import test_distribution
        fixture = test_distribution.BootstrapTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.environment.update(TERM="xterm-256color", LANG="C.UTF-8")
        code = "import os;os.execv('/bin/bash',['bash','install.sh','--preview','--no-animation'])"
        process, master, _, _ = self.spawn(code, fixture.environment)
        self.receive(process, master, "Which language would you like to use?")
        os.write(master, b"\x1b[<0;30;11M\x1b[<0;30;11m")
        self.receive(process, master, "Vista previa de la interfaz", timeout=15)
        os.write(master, b"q")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(fixture.downloads()[-1]["url"].endswith("/installer-es.tar.gz"))

    def test_resize_and_sigint_restore_real_terminal(self):
        code = ("from ptero.ui import UI;ui=UI();\ntry:\n with ui.session():\n"
                "  ui.choice('Panel deployment',[(1,'Standalone',''),(2,'Docker','')])\n"
                "except KeyboardInterrupt: pass\nprint('INTERRUPTED_CLEANLY',flush=True)")
        process, master, slave, original = self.spawn(code)
        self.receive(process, master, "PTERO")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 35, 0, 0))
        process.send_signal(signal.SIGWINCH)
        time.sleep(0.15)
        process.send_signal(signal.SIGINT)
        self.receive(process, master, "INTERRUPTED_CLEANLY")
        self.assertEqual(process.wait(timeout=5), 0)
        restored = termios.tcgetattr(slave)
        for bit in (termios.ECHO, termios.ICANON):
            self.assertEqual(restored[3] & bit, original[3] & bit)

    def test_plain_flag_and_dumb_terminal_do_not_enter_alternate_screen(self):
        for plain, term in ((True, "xterm"), (False, "dumb")):
            code = f"from ptero.ui import UI;ui=UI(plain={plain!r});\nwith ui.session():\n assert ui.screen is None\nprint('PLAIN_OK',flush=True)"
            process, master, _, _ = self.spawn(code, {"TERM": term})
            output = self.receive(process, master, "PLAIN_OK")
            self.assertNotIn(b"\x1b[?1049h", output)
            self.assertEqual(process.wait(timeout=5), 0)

    def test_bootstrap_selector_cancels_before_any_language_download(self):
        process, master, slave, original = self.spawn("import os;os.execv('/bin/bash',['bash','install.sh'])")
        output = self.receive(process, master, "Which language would you like to use?")
        self.assertIn(b"\x1b[?1049h", output)
        os.write(master, b"\x1b[B")
        time.sleep(0.1)
        os.write(master, b"\x1b")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 130)
        restored = termios.tcgetattr(slave)
        for bit in (termios.ECHO, termios.ICANON):
            self.assertEqual(restored[3] & bit, original[3] & bit)

    def test_bootstrap_arrows_download_only_selected_package_and_open_real_tui(self):
        import test_distribution
        fixture = test_distribution.BootstrapTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.environment.update(TERM="xterm-256color", LANG="C.UTF-8")
        code = "import os;os.execv('/bin/bash',['bash','install.sh','--preview','--no-animation'])"
        process, master, _, _ = self.spawn(code, fixture.environment)
        self.receive(process, master, "Which language would you like to use?")
        os.write(master, b"\x1b[B\r")
        self.receive(process, master, "Vista previa de la interfaz", timeout=15)
        os.write(master, b"q")
        self.assertEqual(process.wait(timeout=5), 0)
        downloads = fixture.downloads()
        self.assertEqual(len(downloads), 3)
        self.assertTrue(downloads[-1]["url"].endswith("/installer-es.tar.gz"))

    def test_busy_animation_updates_real_terminal_and_restores_it(self):
        code = ("import os,time;os.environ.pop('NO_COLOR',None);from ptero.ui import UI;ui=UI();\n"
                "with ui.session():\n with ui.busy('Checking your installation plan'): time.sleep(0.5)\n"
                "print('ANIMATION_FINISHED',flush=True)")
        process, master, _, _ = self.spawn(code)
        output = self.receive(process, master, "ANIMATION_FINISHED").decode("utf-8", "replace")
        self.assertGreaterEqual(sum(frame in output for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), 2)
        self.assertEqual(process.wait(timeout=5), 0)

    def test_preview_real_forms_validation_confirmation_and_simulated_install(self):
        code = ("import os;os.environ.pop('NO_COLOR',None);from ptero.cli import main;"
                "raise SystemExit(main(['--language','en','--preview']))")
        process, master, slave, original = self.spawn(code)
        self.receive(process, master, "Interface preview")
        os.write(master, b"2\r")
        self.receive(process, master, "Setup experience")
        os.write(master, b"\r")
        self.receive(process, master, "Panel deployment")
        os.write(master, b"\r")
        self.receive(process, master, "Database engine")
        os.write(master, b"\r")
        self.receive(process, master, "beta release")
        os.write(master, b"1\r")
        self.receive(process, master, "How will you access")
        os.write(master, b"\r")
        self.receive(process, master, "without http")
        os.write(master, b"invalid\r")
        self.receive(process, master, "Invalid value")
        os.write(master, b"\x15panel.example.com\r")
        self.receive(process, master, "Enable HTTPS")
        os.write(master, b"\r")
        self.receive(process, master, "Does DNS point")
        os.write(master, b"1\r")
        self.receive(process, master, "Administrator email")
        os.write(master, b"admin@example.com\r")
        self.receive(process, master, "username")
        os.write(master, b"\r")
        self.receive(process, master, "empty generates one")
        os.write(master, b"Hidden-test-password-123!\r")
        self.receive(process, master, "Confirm")
        os.write(master, b"Hidden-test-password-123!\r")
        self.receive(process, master, "Set up SMTP")
        os.write(master, b"\r")
        self.receive(process, master, "Where are the game")
        os.write(master, b"\r")
        self.receive(process, master, "Ready to install")
        os.write(master, b"\r")
        self.receive(process, master, "Type INSTALL PANEL")
        os.write(master, b"INSTALL PANEL\r")
        output = self.receive(process, master, "Simulation complete", timeout=15)
        self.assertIn(b"PREVIEW", process.tui_output)
        self.assertNotIn(b"Hidden-test-password-123!", process.tui_output)
        self.assertGreaterEqual(sum(frame.encode() in output for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), 2)
        os.write(master, b"\r")
        self.receive(process, master, "Interface preview")
        os.write(master, b"q")
        self.assertEqual(process.wait(timeout=5), 0)
        restored = termios.tcgetattr(slave)
        for bit in (termios.ECHO, termios.ICANON):
            self.assertEqual(restored[3] & bit, original[3] & bit)
