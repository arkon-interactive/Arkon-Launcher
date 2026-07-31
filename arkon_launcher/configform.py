"""Turning a mod's config file into something you can fill in rather than type.

Mod configs are hand-maintained text with comments that are often the only
documentation a setting has. So this does **not** parse a file into data and
write it back out - that would reformat everything and throw the comments away.
Instead it reads the file into a list of entries that each remember the line
they came from, and editing rewrites only the value on that line. A file that
has been through the editor differs from the original exactly where you changed
something, and nowhere else.

Four formats are recognised, chosen by what the packs actually contain:
``.toml``/``.ini``/``.cfg``, ``.json``/``.json5``, and ``.properties``. Anything
else, or anything that does not parse cleanly, falls back to the text editor -
a form that silently drops half a file would be worse than no form at all.

Nesting is flattened to dotted paths (``items.enableNbtFix``), and values that
span several lines - lists, objects - are marked complex and left to the text
editor, because rewriting those in place is where this approach would start
corrupting files.

**Comments are ambiguous and the ambiguity is not fully resolvable.** A line
like ``#enableFoo = true`` is a switched-off setting; ``#Default Value: []`` is
documentation. Both are a comment marker followed by something shaped like
``key <separator> value``. :func:`_looks_like_setting` decides, and it is
deliberately biased towards calling things documentation: describing a setting
that was really disabled costs the user nothing, while offering to "re-enable" a
line of prose would write nonsense into their config.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

TOML_SUFFIXES = {".toml", ".ini", ".cfg", ".conf"}
JSON_SUFFIXES = {".json", ".json5"}
PROPERTIES_SUFFIXES = {".properties"}

SUPPORTED = TOML_SUFFIXES | JSON_SUFFIXES | PROPERTIES_SUFFIXES

# Words that begin a documentation line rather than a setting. Every one of
# these was seen leading a comment in the reference pack.
DOC_WORDS = {
    "default", "defaults", "default value", "range", "valid", "valid values",
    "allowed", "allowed values", "options", "example", "examples", "note",
    "notes", "todo", "warning", "min", "max", "minimum", "maximum", "format",
    "supports", "requires", "see", "usage", "type", "unit", "units",
}

# Leading whitespace matters: Forge-style TOML indents every key under its
# [section] with a tab, and anchoring this at the start of the line silently
# matched none of them.
_SETTING = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*([=:])\s*(\S.*?)\s*$")
_COMMENT = re.compile(r"^(\s*)([#;]|//)\s?(.*)$")
_TOML_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# A json scalar on its own line: "key": value  (with optional trailing comma)
_JSON_SCALAR = re.compile(r'^(\s*)"([^"]+)"\s*:\s*(.+?)(,?)\s*$')


@dataclass
class Entry:
    """One setting, and where in the file it lives."""

    key: str
    raw_value: str
    line: int
    description: str = ""
    section: str = ""
    disabled: bool = False
    complex: bool = False
    # Byte-free slice of the line holding the value, so editing is surgical.
    value_start: int = 0
    value_end: int = 0

    @property
    def label(self) -> str:
        return self.key.rsplit(".", 1)[-1]

    @property
    def kind(self) -> str:
        """Widget to use: bool, integer, number, text, or complex."""
        if self.complex:
            return "complex"
        text = self.raw_value.strip().strip(",").strip('"')
        low = text.lower()
        if low in ("true", "false"):
            return "bool"
        try:
            int(text)
            return "integer"
        except ValueError:
            pass
        try:
            float(text)
            return "number"
        except ValueError:
            pass
        return "text"

    @property
    def value(self):
        text = self.raw_value.strip().rstrip(",")
        low = text.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if self.kind == "integer":
            return int(text)
        if self.kind == "number":
            return float(text)
        return text.strip('"')


@dataclass
class ConfigDocument:
    path: Path
    lines: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    format: str = ""
    newline: str = "\n"

    # A form is for settings. Past this many rows it is not a form any more,
    # it is a spreadsheet, and the text editor is the better tool.
    MAX_ENTRIES = 150

    @property
    def usable(self) -> bool:
        """Whether a form is worth showing at all.

        Repeated keys are the giveaway for a data file rather than a settings
        file: a loot table or recipe list is an array of objects that all have
        the same fields, so flattening it yields ``key`` fifty times over. Those
        belong in the text editor, where the structure is visible.
        """
        editable = [entry for entry in self.entries if not entry.complex]
        if not editable or len(self.entries) > self.MAX_ENTRIES:
            return False
        unique = len({entry.key for entry in self.entries})
        return unique / len(self.entries) >= 0.8

    def sections(self) -> dict[str, list[Entry]]:
        grouped: dict[str, list[Entry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.section, []).append(entry)
        return grouped

    def set_value(self, entry: Entry, text: str) -> None:
        """Replace one value in place, leaving the rest of the line alone."""
        line = self.lines[entry.line]
        self.lines[entry.line] = (
            line[: entry.value_start] + text + line[entry.value_end :]
        )
        entry.raw_value = text

    def text(self) -> str:
        return self.newline.join(self.lines)


def _looks_like_setting(body: str) -> tuple[str, str] | None:
    """Decide whether a comment body is a disabled setting or documentation.

    Returns ``(key, value)`` when it reads as a setting, otherwise None. The
    checks, in order of how much they catch:

    * It has to be shaped like ``key = value`` at all.
    * The key must not be a documentation word - ``Default: 1.5`` and
      ``Range: 0.5 ~ 8.0`` are the common false positives, and both appear
      above almost every setting in a Forge-style TOML.
    * A capitalised word with no underscore or dot is prose, not a key. Config
      keys in these files are ``snake_case``, ``camelCase`` or ``dotted.path``;
      ``Note``, ``Supports`` and ``Example`` are sentences starting up.
    """
    match = _SETTING.match(body.strip())
    if not match:
        return None

    key, _, value = match.groups()

    if key.lower() in DOC_WORDS:
        return None
    # "Default Value: []" - two words before the separator.
    leading = body.strip().split(":")[0].strip().lower()
    if leading in DOC_WORDS:
        return None
    if key[0].isupper() and "_" not in key and "." not in key:
        return None
    if not value:
        return None

    return key, value


def _flush(pending: list[str]) -> str:
    """Join a run of comment lines into one description."""
    return " ".join(part.strip() for part in pending if part.strip()).strip()


def _read(path: Path) -> tuple[list[str], str] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n").split("\n"), newline


def parse(path: Path) -> ConfigDocument | None:
    """Read a config into an editable form model, or None if unsupported."""
    read = _read(Path(path))
    if read is None:
        return None
    return parse_text(path, read[0], read[1])


def parse_text(path: Path, lines, newline: str = "\n") -> ConfigDocument | None:
    """Same, from text already in hand.

    Needed so the form can be rebuilt from unsaved edits in the text view
    instead of from what is still on disk.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        return None

    if isinstance(lines, str):
        newline = "\r\n" if "\r\n" in lines else "\n"
        lines = lines.replace("\r\n", "\n").split("\n")
    lines = list(lines)

    if suffix in JSON_SUFFIXES:
        document = _parse_json(path, lines, newline)
    elif suffix in PROPERTIES_SUFFIXES:
        document = _parse_properties(path, lines, newline)
    else:
        document = _parse_toml(path, lines, newline)

    return document if document and document.usable else None


def _parse_toml(path: Path, lines: list[str], newline: str) -> ConfigDocument:
    document = ConfigDocument(path=path, lines=lines, format="toml", newline=newline)
    section = ""
    pending: list[str] = []
    disabled: list[tuple[str, str, int]] = []

    for number, line in enumerate(lines):
        section_match = _TOML_SECTION.match(line)
        if section_match:
            section = section_match.group(1)
            pending.clear()
            continue

        comment = _COMMENT.match(line)
        if comment:
            body = comment.group(3)
            found = _looks_like_setting(body)
            if found:
                disabled.append((found[0], found[1], number))
            else:
                pending.append(body)
            continue

        setting = _SETTING.match(line)
        if setting:
            key, _, value = setting.groups()
            # A trailing comment is not part of the value.
            value, trailing = _split_trailing_comment(value)
            start = line.rindex(value) if value in line else len(line)
            document.entries.append(
                Entry(
                    key=f"{section}.{key}" if section else key,
                    raw_value=value,
                    line=number,
                    description=_flush(pending + ([trailing] if trailing else [])),
                    section=section,
                    complex=_is_complex(value),
                    value_start=start,
                    value_end=start + len(value),
                )
            )
            pending.clear()
        elif line.strip():
            pending.clear()

    for key, value, number in disabled:
        document.entries.append(
            Entry(
                key=key, raw_value=value, line=number, section=section,
                disabled=True, complex=True,
                description="Commented out in the file.",
            )
        )

    return document


def _parse_properties(path: Path, lines: list[str], newline: str) -> ConfigDocument:
    document = ConfigDocument(
        path=path, lines=lines, format="properties", newline=newline
    )
    pending: list[str] = []

    for number, line in enumerate(lines):
        comment = _COMMENT.match(line)
        if comment:
            body = comment.group(3)
            found = _looks_like_setting(body)
            if found:
                document.entries.append(
                    Entry(
                        key=found[0], raw_value=found[1], line=number,
                        disabled=True, complex=True,
                        description="Commented out in the file.",
                    )
                )
            else:
                pending.append(body)
            continue

        setting = _SETTING.match(line)
        if setting:
            key, _, value = setting.groups()
            value, trailing = _split_trailing_comment(value)
            start = line.rindex(value) if value in line else len(line)
            document.entries.append(
                Entry(
                    key=key, raw_value=value, line=number,
                    description=_flush(pending + ([trailing] if trailing else [])),
                    complex=_is_complex(value),
                    value_start=start,
                    value_end=start + len(value),
                )
            )
            pending.clear()
        elif line.strip():
            pending.clear()

    return document


def _parse_json(path: Path, lines: list[str], newline: str) -> ConfigDocument | None:
    document = ConfigDocument(path=path, lines=lines, format="json", newline=newline)

    # Confirm the file is actually well-formed before offering to edit it. A
    # form built on a half-understood file is how configs get corrupted.
    if not _json_parses(lines):
        return None

    stack: list[str] = []
    pending: list[str] = []
    in_block_comment = False

    for number, line in enumerate(lines):
        stripped = line.strip()

        if in_block_comment:
            pending.append(stripped.rstrip("*/").strip())
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            pending.append(stripped.lstrip("/*").rstrip("*/").strip())
            continue
        if stripped.startswith("//"):
            body = stripped[2:].strip()
            if not _looks_like_setting(body):
                pending.append(body)
            continue

        scalar = _JSON_SCALAR.match(line)
        if scalar:
            indent, key, value, _ = scalar.groups()
            if value in ("{", "["):
                stack.append(key)
                pending.clear()
                continue
            start = line.rindex(value)
            document.entries.append(
                Entry(
                    key=".".join(stack + [key]),
                    raw_value=value,
                    line=number,
                    description=_flush(pending),
                    section=".".join(stack),
                    complex=_is_complex(value),
                    value_start=start,
                    value_end=start + len(value),
                )
            )
            pending.clear()
            continue

        if stripped in ("}", "]", "},", "],") and stack:
            stack.pop()
            pending.clear()
        elif stripped:
            pending.clear()

    return document


def _json_parses(lines: list[str]) -> bool:
    """True when the file is valid JSON once comments are removed."""
    cleaned = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)
    # Trailing commas are legal in json5 and common in these files.
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        json.loads(text, strict=False)
        return True
    except ValueError:
        return False


def _split_trailing_comment(value: str) -> tuple[str, str]:
    """Separate ``10 #int | default: 10`` into the value and its note."""
    for marker in (" #", "\t#", " ;", " //"):
        if marker in value:
            head, _, tail = value.partition(marker)
            if head.strip():
                return head.strip(), tail.strip()
    return value.strip(), ""


def _is_complex(value: str) -> bool:
    """Values this cannot safely rewrite on one line."""
    text = value.strip().rstrip(",")
    if text in ("[", "{", ""):
        return True
    # A list or object that opens and closes on this line is still fine to show
    # read-only, but not to edit through a simple widget.
    return (text.startswith("[") and text != "[]") or text.startswith("{")
