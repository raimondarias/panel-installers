"""Fetch another language only on request, from the running code's GitHub Release."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request


def ensure_language(catalogue, language, directory=None):
    if catalogue and all(language in values for values in catalogue.values()):
        return catalogue
    from .core import InstallerError, atomic_write
    directory = Path(directory) if directory else Path(__file__).parent
    metadata = json.loads((directory / "distribution.json").read_text())
    repository = metadata["repository"]
    tag = os.environ.get("PTERO_RELEASE_TAG", "")
    # Language files come from the same published release as the running code.
    if (repository != "raimondarias/pterodactyl-installer" or not re.fullmatch(r"v\d+\.\d+\.\d+", tag)
            or tag != "v" + str(metadata.get("version", ""))):
        raise InstallerError("The language download source is missing. Restart the online installer to choose another language.")
    expected = metadata["locale_sha256"].get(language, "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise InstallerError("The requested language is not in this distribution.")
    url = f"https://github.com/{repository}/releases/download/{tag}/messages-{language}.json"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ptero-installer"})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("checksum")
        translated = json.loads(content)
        if set(translated) != set(catalogue) or any(
                set(values) != {language} or not isinstance(values[language], str)
                for values in translated.values()):
            raise ValueError("catalogue")
        updated = {source: {**values, **translated[source]} for source, values in catalogue.items()}
        target = directory / "locales/messages.json"
        atomic_write(target, json.dumps(updated, ensure_ascii=False) + "\n")
        return updated
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise InstallerError("The selected language could not be downloaded or verified. Your current language is unchanged.") from exc
