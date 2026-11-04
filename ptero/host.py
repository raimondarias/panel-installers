"""Host checks, Docker runtime, official releases and image pinning."""

import datetime as dt
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .common import Cancelled, InstallerError, atomic_write, valid_version, version_key


class HostMixin:
    def check_host(self, wings=False):
        if os.geteuid() != 0:
            raise InstallerError("Run with sudo or as root.")
        release = {}
        for line in (self.p.root / "etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip('"')
        supported = {"ubuntu": ("22.04", "24.04", "26.04"), "debian": ("12", "13")}
        if release.get("VERSION_ID") not in supported.get(release.get("ID"), ()):
            raise InstallerError("Supported systems: Ubuntu 22.04/24.04/26.04 and Debian 12/13.")
        if platform.machine() not in ("x86_64", "aarch64"):
            raise InstallerError("Supported architecture: amd64 or arm64.")
        if not (self.p.root / "run/systemd/system").is_dir():
            raise InstallerError("A server/VPS running systemd is required.")
        if shutil.disk_usage(self.p.root).free < 4 * 1024**3:
            raise InstallerError("At least 4 GiB of free space is required for images and installation.")
        meminfo = self.p.root / "proc/meminfo"
        if meminfo.exists():
            match = re.search(r"MemTotal:\s+(\d+)", meminfo.read_text())
            if match and int(match[1]) < 2 * 1024**2:
                self.ui.say("At least 2 GiB RAM is recommended, plus memory for your games.", "warn")
        if wings:
            _, virt = self.run.run(["systemd-detect-virt"], check=False)
            if virt.strip() in ("openvz", "lxc", "lxc-libvirt", "docker", "podman"):
                raise InstallerError(f"Wings requires Docker on a compatible host; virtualization: {virt.strip()}.")
        return release

    def docker(self):
        release = self.check_host()
        installed = bool(shutil.which("docker"))
        has_compose = installed and self.run.run(["docker", "compose", "version"], check=False)[0] == 0
        if not has_compose:
            self.ui.say("Docker Engine/Compose will be installed from Docker's official repository.")
            self.run.run(["apt-get", "update"], label="Updating APT indexes")
            self.run.run(["apt-get", "install", "-y", "ca-certificates", "curl"], label="Docker prerequisites")
            distro = release["ID"]
            key = self.p.root / "etc/apt/keyrings/docker.asc"
            key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            data = self.fetch(f"https://download.docker.com/linux/{distro}/gpg").decode("ascii")
            if "BEGIN PGP PUBLIC KEY BLOCK" not in data:
                raise InstallerError("Invalid Docker repository signing key.")
            atomic_write(key, data, 0o644)
            arch = "amd64" if platform.machine() == "x86_64" else "arm64"
            codename = release.get("VERSION_CODENAME", "")
            if not re.fullmatch(r"[a-z]+", codename):
                raise InstallerError("Unable to detect the APT distribution codename.")
            source = f"deb [arch={arch} signed-by={key}] https://download.docker.com/linux/{distro} " f"{codename} stable\n"
            atomic_write(self.p.root / "etc/apt/sources.list.d/ptero-docker.list", source, 0o644)
            self.run.run(["apt-get", "update"], label="Checking the Docker repository")
            packages = (
                ["docker-compose-plugin"]
                if installed
                else ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
            )
            self.run.run(["apt-get", "install", "-y", *packages], label="Installing Docker and Compose")
        self.run.run(["systemctl", "enable", "--now", "docker"], label="Starting Docker")
        self.run.run(["docker", "info"], label="Checking Docker")

    def fetch(self, url, limit=8 * 1024 * 1024):
        headers = {"User-Agent": "ptero-installer/" + __version__, "Accept": "application/json"}
        token = os.environ.get("PTERO_GITHUB_TOKEN")
        parsed = urllib.parse.urlsplit(url)
        if token and parsed.scheme == "https" and parsed.hostname == "api.github.com":
            self.run.secrets.add(token)
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = response.read(limit + 1)
            if len(result) > limit:
                raise InstallerError("Remote response is too large.")
            return result
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InstallerError(f"Unable to download {url}: {exc}") from exc

    def release(self, component, version):
        base = f"https://api.github.com/repos/pterodactyl/{component}/releases"
        if version == "auto":
            with self.ui.busy("Checking official releases"):
                releases = json.loads(self.fetch(base + "?per_page=100"))
            candidates = [
                item
                for item in releases
                if not item["draft"] and valid_version(item["tag_name"], component) and (component != "wings" or not item["prerelease"])
            ]
            if not candidates:
                raise InstallerError(f"No compatible official releases found for {component}.")
            release = max(candidates, key=lambda item: version_key(item["tag_name"]))
        else:
            if not valid_version(version, component):
                raise InstallerError("Invalid version or unsupported major release.")
            with self.ui.busy("Checking official releases"):
                release = json.loads(self.fetch(base + "/tags/" + version))
        if release.get("draft") or not valid_version(release.get("tag_name", ""), component):
            raise InstallerError("GitHub did not return a compatible release.")
        return release

    def choose_release(self, component, default):
        version = self.ui.ask(
            f"{component} version (auto finds the latest compatible release)", default, lambda x: x == "auto" or valid_version(x, component)
        )
        release = self.release(component, version)
        self.ui.say(f"Selected release: {release['tag_name']}")
        if release["prerelease"] and not self.ui.yes("This is a prerelease. Continue?"):
            raise Cancelled("Beta installation cancelled.")
        return release

    def pin_image(self, ref):
        self.run.run(["docker", "pull", ref], label=f"Downloading {ref}", timeout=3600)
        _, text = self.run.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref])
        digests = json.loads(text)
        if not digests or not re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", digests[0]):
            raise InstallerError("The downloaded image does not have a verifiable digest.")
        return digests[0]

    def port_free(self, address, port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((address, port))
        except OSError as exc:
            raise InstallerError(f"Port {address}:{port} is occupied or unavailable.") from exc

    @staticmethod
    def valid_port(value):
        return value.isdigit() and 1 <= int(value) <= 65535

    @staticmethod
    def is_ipv4(value):
        try:
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
        except ValueError:
            return False

    @staticmethod
    def stamp():
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
