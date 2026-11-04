#!/usr/bin/env python3
"""Read-only smoke check of the actual published GitHub distribution."""

import hashlib
import fcntl
import json
import os
import pty
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer as app
from ptero import __version__

REPOSITORY = "raimondarias/pterodactyl-installer"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "ptero-installer-published-check"})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = response.read(16 * 1024 * 1024 + 1)
    if len(result) > 16 * 1024 * 1024:
        raise app.InstallerError("Published file exceeds the smoke check size limit.")
    return result


def tui_preview(launcher, number, language, environment):
    """Run the actual online picker and ncurses preview in an isolated PTY."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    original = termios.tcgetattr(slave)
    process = subprocess.Popen(
        ["bash", str(launcher), "--preview", "--no-animation"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**environment, "TERM": "xterm-256color", "LANG": "C.UTF-8"},
    )
    output = bytearray()

    def until(marker, start=0):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                output.extend(os.read(master, 65536))
                if marker.encode() in output[start:]:
                    return
            elif process.poll() is not None:
                break
        raise app.InstallerError(f"Published {language} TUI failed: " + output.decode("utf-8", "replace")[-1500:])

    try:
        until("Which language would you like to use?")
        row = 7 + number * 2
        os.write(master, f"\x1b[<0;30;{row}M\x1b[<0;30;{row}m".encode())
        until(app.UI(False, language).tr("Interface preview"))
        if output.count(b"\x1b[?1049h") < 2:
            raise app.InstallerError("The published runtime did not open the full-screen TUI.")
        os.write(master, b"\x1b[<0;35;10M\x1b[<0;35;10m")
        until(app.UI(False, language).tr("Setup experience"))
        checkpoint = len(output)
        os.write(master, b"\x1b")
        until(app.UI(False, language).tr("Interface preview"), checkpoint)
        os.write(master, b"q")
        if process.wait(timeout=10):
            raise app.InstallerError("The published TUI did not exit cleanly.")
        restored = termios.tcgetattr(slave)
        if any(restored[3] & bit != original[3] & bit for bit in (termios.ECHO, termios.ICANON)):
            raise app.InstallerError("The published TUI did not restore terminal input modes.")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def main():
    tag = "v" + __version__
    latest = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/releases/latest"))["tag_name"]
    if latest != tag:
        raise app.InstallerError(f"The latest published release is {latest}, expected {tag}.")
    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}"
    sums = {line.split()[1]: line.split()[0] for line in fetch(base + "/SHA256SUMS").decode().splitlines()}
    bootstrap = fetch(base + "/install.sh")
    if hashlib.sha256(bootstrap).hexdigest() != sums.get("install.sh") or f'VERSION="{__version__}"'.encode() not in bootstrap:
        raise app.InstallerError("The published bootstrap does not match its release checksum or version.")
    if fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/main/install.sh") != bootstrap:
        raise app.InstallerError("main's install.sh differs from the latest release's bootstrap.")
    print(f"OK published release: {tag}; bootstrap checksum verified", flush=True)
    environment = dict(os.environ)
    for name in ("PTERO_RELEASE_TAG", "PTERO_NO_SELF_UPDATE", "PTERO_SELF_UPDATED"):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="ptero-published-check-") as directory:
        work = Path(directory)
        launcher = work / "install.sh"
        launcher.write_bytes(bootstrap)
        for number, language, expected in (
            (1, "en", "Ready to install"),
            (2, "es", "Listo para instalar"),
            (3, "pt-BR", "Pronto para instalar"),
        ):
            result = subprocess.run(
                ["bash", str(launcher), "--preview", "--no-animation"],
                input=f"{number}\n",
                text=True,
                capture_output=True,
                timeout=240,
                env=environment,
            )
            if result.returncode or expected not in result.stdout or result.stdout.count("Which language would you like to use?") != 1:
                raise app.InstallerError(f"Published {language} preview failed: " + result.stderr[-2000:])
            print(f"OK real GitHub release download and preview: {language}; one English language question", flush=True)
            tui_preview(launcher, number, language, environment)
            print(
                f"OK published full-screen TUI: {language}; mouse language/action selection, wizard cancellation and terminal restoration",
                flush=True,
            )
        payload = fetch(base + "/installer-en.tar.gz")
        if hashlib.sha256(payload).hexdigest() != sums.get("installer-en.tar.gz"):
            raise app.InstallerError("The published English archive checksum is invalid.")
        archive = work / "runtime.tar.gz"
        archive.write_bytes(payload)
        runtime = work / "runtime"
        app.secure_extract(archive, runtime, preserve_ownership=False)
        code = (
            "import json;from ptero.i18n import Translator;"
            "print(json.dumps({language:Translator(language)('Choose an option') "
            "for language in ('en','es','pt-BR')},ensure_ascii=False))"
        )
        environment.update(PTERO_RELEASE_TAG=tag, PYTHONPATH="")
        result = subprocess.run([sys.executable, "-c", code], cwd=runtime, capture_output=True, text=True, env=environment, timeout=120)
        if result.returncode:
            raise app.InstallerError("Published language change failed: " + result.stderr[-2000:])
        translated = json.loads(result.stdout)
        if translated != {"en": "Choose an option", "es": "Elige una opción", "pt-BR": "Escolha uma opção"}:
            raise app.InstallerError("Published language changes returned unexpected translations.")
        print("OK real GitHub language changes: en -> es -> pt-BR; release assets and hashes verified", flush=True)
    print("This distribution smoke check installs no services. Real VM deployment results are recorded separately.", flush=True)


if __name__ == "__main__":
    main()
