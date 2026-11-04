"""Rebuild the locale catalogue from the maintained translation table.

Every message is keyed by its English source text. Each line of
``scripts/translations.txt`` is ``English|||Spanish|||Brazilian Portuguese``.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    messages = {}
    for number, line in enumerate((ROOT / "scripts/translations.txt").read_text().splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        fields = line.split("|||")
        if len(fields) != 3:
            raise SystemExit(f"translations.txt:{number}: expected English|||Spanish|||Portuguese")
        en, es, pt = (value.replace("\\n", "\n") for value in fields)
        if en in messages:
            raise SystemExit(f"translations.txt:{number}: duplicate English source: {en}")
        messages[en] = {"en": en, "es": es, "pt-BR": pt}
    (ROOT / "ptero/locales/messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(messages)} translated messages in English, Spanish and Brazilian Portuguese")


if __name__ == "__main__":
    build()
