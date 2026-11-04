"""Cell-aware terminal text helpers, without third-party dependencies."""

import unicodedata


def clean(text):
    return "".join(character for character in str(text) if character == "\n" or not unicodedata.category(character).startswith("C"))


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in clean(text))


def clip(text, width):
    result, used = [], 0
    for char in clean(text).replace("\n", " "):
        size = cell_width(char)
        if used + size > max(0, width):
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap(text, width):
    """Wrap at cells while preserving literal password/address characters."""
    result = []
    for paragraph in clean(text).split("\n"):
        line, used = "", 0
        for char in paragraph:
            size = cell_width(char)
            if used + size > max(1, width):
                if char == " ":
                    result.append(line)
                    line, used = "", 0
                    continue
                space = line.rfind(" ")
                if space > 0:
                    result.append(line[:space])
                    line = line[space + 1 :]
                    used = cell_width(line)
                else:
                    result.append(line)
                    line, used = "", 0
            line += char
            used += size
        result.append(line)
    return result
