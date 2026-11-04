"""Minimal translation layer.

Source strings are English. A translation is a JSON file in
omapod/locale/<lang>.json mapping the English string to the translated one;
plural entries map the singular to a list [singular, plural].
"""

import json
import os
from pathlib import Path

_table = {}
_language = "en"


def _detect():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value.split(":")[0].split(".")[0].split("_")[0].lower() or "en"
    return "en"


def set_language(lang):
    """Load a translation; 'auto' follows the environment."""
    global _table, _language
    if not lang or lang == "auto":
        lang = _detect()
    _language = lang
    _table = {}
    if lang in ("en", "c", "posix"):
        return
    path = Path(__file__).with_name("locale") / f"{lang}.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _table = data
    except (OSError, ValueError):
        _table = {}


def language():
    return _language


def _(text):
    value = _table.get(text)
    if isinstance(value, str) and value:
        return value
    return text


def ngettext(singular, plural, count):
    value = _table.get(singular)
    if isinstance(value, list) and len(value) == 2:
        return value[0] if count == 1 else value[1]
    return singular if count == 1 else plural


set_language("auto")
