#!/usr/bin/env python3
"""List user-visible source strings that have no entry in the locale catalogue.

User-visible means: the first text argument of UI calls (say, ask, yes, confirm,
card, busy, step, choice, tr, record, result) and InstallerError/Cancelled
messages. f-strings are compared as catalogue templates ({value1}, {value2}...).
Exit status 1 lists the missing messages; 0 means the catalogue is complete.
"""

import ast
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CALLS = {"say", "ask", "yes", "confirm", "card", "busy", "step", "choice", "tr", "result"}
ERRORS = {"InstallerError", "Cancelled"}
# Not translatable: literal command output, version tags, symbols and fixed product names.
IGNORED = {"", "Panel", "Wings", "MariaDB", "MySQL", "English", "Español", "Português (Brasil)"}


def template(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts, count = [], 0
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                count += 1
                parts.append("{value%d}" % count)
        return "".join(parts)
    return None


def texts(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "card":
            yield node.lineno, template(node.args[0])
            if len(node.args) > 1 and isinstance(node.args[1], (ast.List, ast.Tuple)):
                for row in node.args[1].elts:
                    yield row.lineno, template(row)
        elif name == "step" and len(node.args) >= 3:
            yield node.lineno, template(node.args[2])
        elif name == "choice":
            yield node.lineno, template(node.args[0])
            if len(node.args) > 1 and isinstance(node.args[1], ast.List):
                for option in node.args[1].elts:
                    if isinstance(option, ast.Tuple) and len(option.elts) == 3:
                        for item in option.elts[1:]:
                            yield option.lineno, template(item)
        elif name in CALLS or name in ERRORS:
            yield node.lineno, template(node.args[0])


def missing():
    catalogue = json.loads((ROOT / "ptero/locales/messages.json").read_text())
    result = []
    for path in sorted((ROOT / "ptero").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for line, text in texts(tree):
            if text is None or text in IGNORED or text in catalogue:
                continue
            if not any(character.isalpha() for character in re.sub(r"\{value\d+\}", "", text)):
                continue
            result.append((str(path.relative_to(ROOT)), line, text))
    return result


if __name__ == "__main__":
    found = missing()
    for path, line, text in found:
        print(f"{path}:{line}: {text}")
    print(f"{len(found)} untranslated user-visible messages")
    sys.exit(1 if found else 0)
