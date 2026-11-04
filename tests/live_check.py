#!/usr/bin/env python3
"""Remote artifact checks. No system packages, services or containers are installed."""

import json
import http.server
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer as app
from ptero.native import NativeBackend


def main():
    if not shutil.which("docker"):
        raise SystemExit("The Docker CLI is required to inspect OCI manifests.")
    with tempfile.TemporaryDirectory(prefix="ptero-live-check-") as directory:
        paths = app.Paths(Path(directory))
        installer = app.Installer(paths=paths, ui=app.UI(animate=False))
        panel = installer.release("panel", app.PANEL_VERSION)
        wings = installer.release("wings", app.WINGS_VERSION)
        print(f"OK official Panel release: {panel['tag_name']} (beta={panel['prerelease']})")
        print(f"OK official Wings release: {wings['tag_name']}")
        installer.download_wings(wings, paths.wings)
        _, version = installer.run.run([paths.wings, "version"])
        if app.WINGS_VERSION not in version:
            raise app.InstallerError("El binario descargado tiene una versión inesperada.")
        print("OK real Wings binary: official SHA-256 and version command")
        payload = {
            "uuid": "3d8dd7f9-07a1-4d65-8db0-8c8921f2f4f7",
            "token_id": "abcdefgh12345678",
            "token": "public-fixture-token",
            "api": {"host": "0.0.0.0", "port": 8080, "ssl": {"enabled": False, "cert": "", "key": ""}, "upload_limit": 100},
            "system": {"sftp": {"bind_port": 2022}},
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                config = Path(directory) / "fixture-config.yml"
                app.write_json(config, app.node_configuration(payload, url))
                installer.run.run(
                    [
                        paths.wings,
                        "configure",
                        "--panel-url",
                        url,
                        "--node",
                        "1",
                        "--token",
                        "public-test-only",
                        "--config-path",
                        config,
                        "--override",
                    ]
                )
                if "public-fixture-token" not in config.read_text():
                    raise app.InstallerError("El binario no pudo leer y actualizar la configuración JSON/YAML.")
            finally:
                server.shutdown()
                thread.join()
        print("OK JSON/YAML configuration accepted by real Wings against a local mock API")
        state = {
            "schema": 1,
            "owner": app.PROJECT,
            "method": "standalone",
            "version": app.PANEL_VERSION,
            "database_engine": "mariadb",
            "php": "8.3",
            "url": "http://127.0.0.1",
            "http_port": 80,
            "https_port": 443,
            "bind": "0.0.0.0",
            "tls": False,
            "email": "admin@example.test",
            "timezone": "UTC",
            "db_password": "public-fixture",
            "redis_password": "public-fixture",
        }
        NativeBackend(installer, state).source(panel)
        print("OK real Panel archive: official SHA-256, safe extraction and expected release files")
        for image, version in (("panel", app.PANEL_VERSION), ("wings", app.WINGS_VERSION)):
            result = subprocess.run(
                ["docker", "manifest", "inspect", f"ghcr.io/pterodactyl/{image}:{version}"],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            manifest = json.loads(result.stdout)
            arches = {item["platform"]["architecture"] for item in manifest["manifests"] if item["platform"]["os"] == "linux"}
            if not {"amd64", "arm64"}.issubset(arches):
                raise app.InstallerError("The image does not publish the expected architectures.")
            print(f"OK official OCI {image} image for amd64 and arm64")
        print("Full VPS deployment with systemd, Docker, public DNS and TLS remains to be tested.")


if __name__ == "__main__":
    main()
