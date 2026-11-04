#!/usr/bin/env python3
"""Build a relocatable source archive and a single-file, self-extracting launcher."""

import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent


def build():
    files = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in ("__pycache__", ".venv", ".git") for part in path.relative_to(ROOT).parts)
    )
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w:gz") as archive:
        for file in files:
            archive.add(file, arcname=str(Path("pterodactyl-installer") / file.relative_to(ROOT)), recursive=False)
    payload = memory.getvalue()
    source = OUTPUT / "pterodactyl-installer.tar.gz"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    launcher = '''#!/usr/bin/env bash
set -Eeuo pipefail
WORK_DIR="$(mktemp -d -t ptero-installer.XXXXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT
PAYLOAD_LINE="$(awk '/^__PTERO_PAYLOAD_BELOW__$/ {print NR + 1; exit}' "$0")"
tail -n +"$PAYLOAD_LINE" "$0" > "$WORK_DIR/payload.tar.gz"
printf '%s  %s\\n' '__SHA256__' "$WORK_DIR/payload.tar.gz" | sha256sum --check --status
tar -xzf "$WORK_DIR/payload.tar.gz" -C "$WORK_DIR"
bash "$WORK_DIR/pterodactyl-installer/scripts/run_local.sh" "$@"
exit "$?"
__PTERO_PAYLOAD_BELOW__
'''.replace("__SHA256__", digest)
    single = OUTPUT / "pterodactyl-installer.run"
    single.write_bytes(launcher.encode() + payload)
    single.chmod(0o755)
    repository = OUTPUT / "pterodactyl-installer-repo.zip"
    with zipfile.ZipFile(repository, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, str(Path("pterodactyl-installer") / file.relative_to(ROOT)))
    for artifact in (source, single, repository):
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        Path(str(artifact) + ".sha256").write_text(f"{checksum}  {artifact.name}\n")
        print(f"{artifact} ({artifact.stat().st_size:,} bytes)")
    return source, single


if __name__ == "__main__":
    build()
