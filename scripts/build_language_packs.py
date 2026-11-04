#!/usr/bin/env python3
"""Build the release assets: one single-language runtime package per language.

Output (default ``dist/``), published as GitHub Release assets by CI:

- ``installer-<language>.tar.gz``: shared runtime code plus only that language's catalogue.
- ``messages-<language>.json``: the same catalogue, for switching language at runtime.
- ``install.sh``: the online bootstrap for this version.
- ``SHA256SUMS``: checksums of every asset above, in ``sha256sum`` format.

Rebuilding unchanged sources produces byte-identical archives.
"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "raimondarias/pterodactyl-installer"
LANGUAGES = {"en": "English", "es": "Español", "pt-BR": "Português (Brasil)"}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def version():
    value = re.search(r'__version__ = "([^"]+)"', (ROOT / "ptero/__init__.py").read_text())[1]
    if f'VERSION="{value}"' not in (ROOT / "install.sh").read_text():
        raise ValueError("Bootstrap and runtime versions differ")
    return value


def archive(content):
    memory = io.BytesIO()
    # Stable metadata makes rebuilding unchanged source byte-for-byte reproducible.
    with gzip.GzipFile(fileobj=memory, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as package:
            for name, data in sorted(content.items()):
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.mtime = len(data), 0o644, 0
                package.addfile(item, io.BytesIO(data))
    return memory.getvalue()


def build(output=None):
    output = Path(output) if output else ROOT / "dist"
    output.mkdir(parents=True, exist_ok=True)
    current = version()
    catalogue = json.loads((ROOT / "ptero/locales/messages.json").read_text())
    locales = {language: encoded({source: {language: translations[language]}
                                 for source, translations in catalogue.items()}) for language in LANGUAGES}
    hashes = {language: hashlib.sha256(data).hexdigest() for language, data in locales.items()}
    files = {str(path.relative_to(ROOT)): path.read_bytes() for path in (ROOT / "ptero").rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json")
             and path.name not in ("messages.json", "distribution.json")}
    for name in ("installer.py", "scripts/run_local.sh", "LICENSE"):
        files[name] = (ROOT / name).read_bytes()
    assets = {"install.sh": (ROOT / "install.sh").read_bytes()}
    for language in LANGUAGES:
        metadata = {"schema": 1, "repository": REPOSITORY, "language": language, "version": current,
                    "locale_sha256": hashes}
        assets[f"installer-{language}.tar.gz"] = archive({**files, "ptero/locales/messages.json": locales[language],
                                                          "ptero/distribution.json": encoded(metadata)})
        assets[f"messages-{language}.json"] = locales[language]
    for name, data in assets.items():
        (output / name).write_bytes(data)
    (output / "install.sh").chmod(0o755)
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(assets.items()))
    (output / "SHA256SUMS").write_text(sums)
    for name, data in sorted(assets.items()):
        print(f"{output.name}/{name}: {len(data):,} bytes")
    return output


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else None)
