"""Execute the real Bash bootstrap against a local, recorded GitHub fixture."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from unittest.mock import patch

from test_installer import BaseTest
import installer as app
from ptero import __version__
from ptero.distribution import ensure_language

ROOT = Path(__file__).resolve().parents[1]
TAG = "v" + __version__
REPOSITORY = "raimondarias/pterodactyl-installer"


def builder():
    spec = importlib.util.spec_from_file_location("language_builder", ROOT / "scripts/build_language_packs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self.temporary.name)
        self.releases = self.base / "releases"
        self.packages = self.releases / TAG
        with contextlib.redirect_stdout(io.StringIO()):
            builder().build(self.packages)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.download_log = self.base / "downloads.jsonl"
        self.work = self.base / "work"
        self.work.mkdir()
        fake = self.bin / "curl"
        fake.write_text(
            "#!"
            + sys.executable
            + "\n"
            + '''
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
url = next(value for value in args if value.startswith('https://'))
target = pathlib.Path(args[args.index('--output') + 1])
with open(os.environ['FIXTURE_LOG'], 'a') as stream:
    stream.write(json.dumps({'url': url, 'args': args}) + '\\n')
if url == 'https://api.github.com/repos/raimondarias/pterodactyl-installer/releases/latest':
    latest = os.environ.get('FIXTURE_LATEST')
    if not latest:
        raise SystemExit(22)
    target.write_text(json.dumps({'tag_name': latest, 'draft': False}))
else:
    prefix = 'https://github.com/raimondarias/pterodactyl-installer/releases/download/'
    if not url.startswith(prefix):
        raise SystemExit(22)
    source = pathlib.Path(os.environ['FIXTURE_RELEASES']) / url[len(prefix):]
    if not source.is_file():
        raise SystemExit(22)
    shutil.copyfile(source, target)
'''
        )
        fake.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FIXTURE_LOG": str(self.download_log),
            "FIXTURE_RELEASES": str(self.releases),
            "FIXTURE_LATEST": TAG,
            "TMPDIR": str(self.work),
        }
        for name in ("PTERO_RELEASE_TAG", "PTERO_NO_SELF_UPDATE", "PTERO_SELF_UPDATED", "PTERO_UPDATE_DIR"):
            self.environment.pop(name, None)

    def run_bootstrap(self, selection="", args=("--preview", "--no-animation")):
        return subprocess.run(
            ["bash", str(ROOT / "install.sh"), *args], input=selection, text=True, capture_output=True, env=self.environment, timeout=30
        )

    def downloads(self):
        return [json.loads(line) for line in self.download_log.read_text().splitlines()] if self.download_log.exists() else []

    def test_first_question_is_english_then_only_selected_language_is_downloaded(self):
        for number, language, text in (
            (1, "en", "Ready to install"),
            (2, "es", "Listo para instalar"),
            (3, "pt-BR", "Pronto para instalar"),
        ):
            with self.subTest(language=language):
                self.download_log.unlink(missing_ok=True)
                result = self.run_bootstrap(str(number) + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Which language would you like to use?", result.stdout)
                self.assertLess(result.stdout.index("Select [1]"), result.stdout.index("GitHub"))
                self.assertIn(text, result.stdout)
                downloads = self.downloads()
                self.assertEqual(len(downloads), 3)
                self.assertTrue(downloads[0]["url"].endswith("/releases/latest"))
                self.assertTrue(all(f"/releases/download/{TAG}/" in item["url"] for item in downloads[1:]))
                self.assertTrue(downloads[2]["url"].endswith(f"/installer-{language}.tar.gz"))
                self.assertEqual(result.stdout.count("Which language would you like to use?"), 1)
                self.assertEqual(list(self.work.iterdir()), [])

    def test_closed_input_does_not_download_anything(self):
        result = self.run_bootstrap("")
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.downloads(), [])

    def test_invalid_input_reprompts_and_default_selects_english(self):
        result = self.run_bootstrap("invalid\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Invalid selection", result.stdout)
        self.assertIn("Ready to install", result.stdout)

    def test_language_flag_skips_selector_and_no_self_update_skips_the_release_check(self):
        result = self.run_bootstrap(args=("--language", "es", "--preview", "--no-animation", "--no-self-update"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Which language", result.stdout)
        self.assertEqual(len(self.downloads()), 2)
        self.assertIn("Listo para instalar", result.stdout)

    def test_invalid_language_never_downloads_code(self):
        result = self.run_bootstrap(args=("--language", "../es", "--preview"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.downloads(), [])

    def newer_release(self, version="9.9.9", tamper=False):
        """Publish a fixture release whose bootstrap reports a newer version."""
        directory = self.releases / ("v" + version)
        directory.mkdir()
        script = (ROOT / "install.sh").read_text().replace(f'VERSION="{__version__}"', f'VERSION="{version}"')
        script = script.replace("set -Eeuo pipefail\n", "set -Eeuo pipefail\nprintf 'UPDATED BOOTSTRAP %s\\n' \"$*\"\n", 1)
        # The newer bootstrap downloads its own release: reuse this version's packages.
        for item in self.packages.iterdir():
            if item.name.startswith(("installer-", "messages-")):
                shutil.copyfile(item, directory / item.name)
        (directory / "install.sh").write_text(script)
        digest = hashlib.sha256(script.encode()).hexdigest() if not tamper else "0" * 64
        (directory / "SHA256SUMS").write_text(f"{digest}  install.sh\n")
        self.environment["FIXTURE_LATEST"] = "v" + version
        return script

    def test_newer_release_replaces_and_restarts_the_saved_bootstrap(self):
        script = self.newer_release()
        saved = self.base / "ptero-setup.sh"
        shutil.copyfile(ROOT / "install.sh", saved)
        saved.chmod(0o750)
        result = subprocess.run(
            ["bash", str(saved), "--preview", "--no-animation"],
            input="2\n",
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=30,
        )
        self.assertIn(f"Actualizando el instalador de {__version__} a 9.9.9", result.stdout)
        self.assertIn("UPDATED BOOTSTRAP --preview --no-animation --language es", result.stdout)
        self.assertEqual(saved.read_text(), script)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o750)
        self.assertEqual(result.stdout.count("Which language would you like to use?"), 1)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_unverified_update_keeps_the_current_bootstrap(self):
        self.newer_release(tamper=True)
        saved = self.base / "ptero-setup.sh"
        shutil.copyfile(ROOT / "install.sh", saved)
        result = subprocess.run(
            ["bash", str(saved), "--preview", "--no-animation"],
            input="1\n",
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not be verified", result.stderr)
        self.assertEqual(saved.read_bytes(), (ROOT / "install.sh").read_bytes())
        self.assertIn("Ready to install", result.stdout)

    def test_older_or_unreachable_latest_release_continues_without_update(self):
        for latest, notice in (("v0.0.1", ""), ("", "Could not check for installer updates")):
            with self.subTest(latest=latest):
                self.environment["FIXTURE_LATEST"] = latest
                result = self.run_bootstrap("1\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Ready to install", result.stdout)
                self.assertIn(notice, result.stderr)
                self.assertNotIn("Updating the installer", result.stdout)

    def test_version_and_help_do_not_download_or_ask_questions(self):
        for option in ("--version", "--help"):
            result = self.run_bootstrap(args=(option,))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Select [1]", result.stdout)
            self.assertEqual(self.downloads(), [])

    def test_missing_unpublished_package_stops_before_running_installer(self):
        (self.packages / "installer-es.tar.gz").unlink()
        result = self.run_bootstrap("2\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No se ejecutó el código", result.stderr)
        self.assertNotIn("Listo para instalar", result.stdout)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_checksum_mismatch_is_rejected_before_extraction(self):
        sums = self.packages / "SHA256SUMS"
        sums.write_text(
            "".join(
                ("0" * 64 + "  installer-es.tar.gz\n") if line.endswith("installer-es.tar.gz\n") else line
                for line in sums.read_text().splitlines(keepends=True)
            )
        )
        result = self.run_bootstrap("2\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_wrong_language_package_is_rejected(self):
        shutil.copyfile(self.packages / "installer-en.tar.gz", self.packages / "installer-es.tar.gz")
        sums = self.packages / "SHA256SUMS"
        english = next(line.split()[0] for line in sums.read_text().splitlines() if line.endswith("installer-en.tar.gz"))
        sums.write_text(
            "".join(
                (english + "  installer-es.tar.gz\n") if line.endswith("installer-es.tar.gz\n") else line
                for line in sums.read_text().splitlines(keepends=True)
            )
        )
        result = self.run_bootstrap("2\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertNotIn("Ready to install", result.stdout)

    def test_traversal_links_devices_and_duplicate_members_are_rejected(self):
        for kind in ("traversal", "absolute", "symlink", "device", "duplicate"):
            with self.subTest(kind=kind):
                archive = self.packages / "installer-en.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    name = "../escaped" if kind == "traversal" else str(self.base / "escaped") if kind == "absolute" else "unsafe"
                    item = tarfile.TarInfo(name)
                    item.size = 0
                    if kind == "symlink":
                        item.type, item.linkname = tarfile.SYMTYPE, str(self.base)
                    elif kind == "device":
                        item.type = tarfile.CHRTYPE
                    stream.addfile(item, io.BytesIO())
                    if kind == "duplicate":
                        stream.addfile(item, io.BytesIO())
                checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
                sums = self.packages / "SHA256SUMS"
                sums.write_text(
                    "".join(
                        (checksum + "  installer-en.tar.gz\n") if line.endswith("installer-en.tar.gz\n") else line
                        for line in sums.read_text().splitlines(keepends=True)
                    )
                )
                result = self.run_bootstrap("1\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsafe or duplicate", result.stderr)
                self.assertFalse((self.base / "escaped").exists())
                self.assertEqual(list(self.work.iterdir()), [])

    def test_release_assets_are_reproducible_checksummed_and_single_language(self):
        with contextlib.redirect_stdout(io.StringIO()):
            builder().build(self.base / "second")
        sums = {line.split()[1]: line.split()[0] for line in (self.packages / "SHA256SUMS").read_text().splitlines()}
        self.assertEqual(
            set(sums),
            {
                "install.sh",
                *(f"installer-{lang}.tar.gz" for lang in ("en", "es", "pt-BR")),
                *(f"messages-{lang}.json" for lang in ("en", "es", "pt-BR")),
            },
        )
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((self.packages / name).read_bytes()).hexdigest(), digest)
            self.assertEqual((self.packages / name).read_bytes(), (self.base / "second" / name).read_bytes())
        for language in ("en", "es", "pt-BR"):
            with tarfile.open(self.packages / f"installer-{language}.tar.gz") as stream:
                catalogue = json.load(stream.extractfile("ptero/locales/messages.json"))
                self.assertTrue(all(set(values) == {language} for values in catalogue.values()))
                self.assertNotIn("tests/test_distribution.py", stream.getnames())
                self.assertIn("ptero/native.py", stream.getnames())
                metadata = json.load(stream.extractfile("ptero/distribution.json"))
                self.assertEqual(metadata["locale_sha256"][language], sums[f"messages-{language}.json"])

    def test_generated_packages_are_not_committed(self):
        self.assertFalse((ROOT / "languages").exists())


class LanguageChangeTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.temporary.name) / "ptero"
        (self.directory / "locales").mkdir(parents=True)
        self.original = {"Choose an option": {"en": "Choose an option"}}
        self.translated = json.dumps({"Choose an option": {"es": "Elige una opción"}}).encode()
        self.target = self.directory / "locales/messages.json"
        self.target.write_text(json.dumps(self.original))
        self.metadata = {
            "repository": REPOSITORY,
            "version": __version__,
            "locale_sha256": {"es": hashlib.sha256(self.translated).hexdigest()},
        }
        (self.directory / "distribution.json").write_text(json.dumps(self.metadata))

    def test_switch_downloads_only_requested_locale_from_same_release_and_caches_it(self):
        with (
            patch.dict(os.environ, {"PTERO_RELEASE_TAG": TAG}),
            patch("ptero.distribution.urllib.request.urlopen", return_value=io.BytesIO(self.translated)) as request,
        ):
            updated = ensure_language(self.original, "es", self.directory)
        self.assertIn(f"/releases/download/{TAG}/messages-es.json", request.call_args.args[0].full_url)
        self.assertEqual(updated["Choose an option"]["es"], "Elige una opción")
        self.assertEqual(updated["Choose an option"]["en"], "Choose an option")
        self.assertEqual(json.loads(self.target.read_text()), updated)
        with patch("ptero.distribution.urllib.request.urlopen") as request:
            self.assertEqual(ensure_language(updated, "en", self.directory), updated)
            self.assertEqual(ensure_language(updated, "es", self.directory), updated)
        request.assert_not_called()

    def test_bad_hash_or_failed_download_preserves_existing_catalogue(self):
        for failure in (b"corrupted", OSError("offline")):
            kwargs = {"side_effect": failure} if isinstance(failure, OSError) else {"return_value": io.BytesIO(failure)}
            with patch.dict(os.environ, {"PTERO_RELEASE_TAG": TAG}), patch("ptero.distribution.urllib.request.urlopen", **kwargs):
                with self.assertRaises(app.InstallerError):
                    ensure_language(self.original, "es", self.directory)
            self.assertEqual(json.loads(self.target.read_text()), self.original)

    def test_source_catalogue_needs_no_network_for_any_language(self):
        catalogue = json.loads((ROOT / "ptero/locales/messages.json").read_text())
        with patch("ptero.distribution.urllib.request.urlopen") as request:
            for language in ("en", "es", "pt-BR"):
                ensure_language(catalogue, language)
        request.assert_not_called()

    def test_language_switch_requires_the_running_release(self):
        for tag in ("main", "v0.0.1", ""):
            with self.subTest(tag=tag), patch.dict(os.environ, {"PTERO_RELEASE_TAG": tag}):
                with self.assertRaises(app.InstallerError):
                    ensure_language(self.original, "es", self.directory)
