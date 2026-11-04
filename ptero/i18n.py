"""Small, dependency-free message catalogue with formatted-message support."""

import json
from pathlib import Path
import re
import string

LANGUAGES = {"en": "English", "es": "Español", "pt-BR": "Português (Brasil)"}


class Translator:
    def __init__(self, language="en"):
        if language not in LANGUAGES:
            raise ValueError("Unknown language: " + language)
        self.language = language
        self.catalogue = json.loads((Path(__file__).parent / "locales/messages.json").read_text())
        from .distribution import ensure_language

        self.catalogue = ensure_language(self.catalogue, language)
        self.patterns = []
        for source, translations in self.catalogue.items():
            if "{" not in source:
                continue
            pattern = ""
            fields = []
            for literal, field, _, _ in string.Formatter().parse(source):
                pattern += re.escape(literal)
                if field is not None:
                    fields.append(field)
                    pattern += "(.*?)"
            self.patterns.append((re.compile("^" + pattern + "$", re.S), fields, translations))

    def __call__(self, text):
        if text in self.catalogue:
            return self.catalogue[text].get(self.language, text)
        for pattern, fields, translations in self.patterns:
            match = pattern.match(text)
            if match:
                values = dict(zip(fields, match.groups()))
                values = {key: self(value) for key, value in values.items()}
                return translations.get(self.language, text).format(**values)
        return text
