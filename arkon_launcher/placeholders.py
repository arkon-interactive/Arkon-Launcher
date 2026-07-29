"""Placeholders and formatting codes, in one place.

Both the help text the user reads and the substitution the code performs come
from here, so the documentation cannot drift away from the behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Placeholder:
    token: str
    meaning: str
    example: str = ""


PLAYER_PLACEHOLDERS: tuple[Placeholder, ...] = (
    Placeholder("{player}", "The player the action was used on", "kick {player}"),
    Placeholder("{world}", "The world the server is running", "say Welcome to {world}"),
    Placeholder("{online}", "How many players are online now", "say {online} online"),
    Placeholder("{max}", "The player limit", "say {online}/{max} playing"),
    Placeholder("{version}", "The Minecraft version", "say Running {version}"),
)

# Minecraft's own formatting codes. The section sign is awkward to type, so an
# ampersand is accepted and translated on the way out.
COLOUR_CODES: tuple[tuple[str, str], ...] = (
    ("&0", "Black"),
    ("&1", "Dark blue"),
    ("&2", "Dark green"),
    ("&3", "Dark aqua"),
    ("&4", "Dark red"),
    ("&5", "Dark purple"),
    ("&6", "Gold"),
    ("&7", "Grey"),
    ("&8", "Dark grey"),
    ("&9", "Blue"),
    ("&a", "Green"),
    ("&b", "Aqua"),
    ("&c", "Red"),
    ("&d", "Light purple"),
    ("&e", "Yellow"),
    ("&f", "White"),
)

FORMAT_CODES: tuple[tuple[str, str], ...] = (
    ("&l", "Bold"),
    ("&o", "Italic"),
    ("&n", "Underline"),
    ("&m", "Strikethrough"),
    ("&k", "Obfuscated (scrambling characters)"),
    ("&r", "Reset back to normal"),
)


def substitute(template: str, **values: object) -> str:
    """Fill placeholders and translate & colour codes into section signs.

    Unknown placeholders are left alone rather than raising - a typo in a custom
    command should produce a visibly wrong message, not a crash mid-command.
    """
    result = template
    for placeholder in PLAYER_PLACEHOLDERS:
        key = placeholder.token.strip("{}")
        if key in values and values[key] is not None:
            result = result.replace(placeholder.token, str(values[key]))
    return translate_colours(result)


def translate_colours(text: str) -> str:
    """`&a` becomes `§a`. `&&` is an escaped literal ampersand."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "&" and index + 1 < len(text):
            following = text[index + 1]
            if following == "&":
                out.append("&")
                index += 2
                continue
            if following.lower() in "0123456789abcdefklmnor":
                out.append("§")
                out.append(following)
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def help_html() -> str:
    """The reference shown in the help dialog."""
    rows = "".join(
        f"<tr><td><code>{p.token}</code></td><td>{p.meaning}</td></tr>"
        for p in PLAYER_PLACEHOLDERS
    )
    colours = "".join(
        f"<tr><td><code>{code}</code></td><td>{name}</td></tr>"
        for code, name in COLOUR_CODES
    )
    formats = "".join(
        f"<tr><td><code>{code}</code></td><td>{name}</td></tr>"
        for code, name in FORMAT_CODES
    )
    return f"""
<h3>Placeholders</h3>
<p>These are replaced when the command runs.</p>
<table cellpadding="4">{rows}</table>

<h3>Colours</h3>
<p>Write <code>&amp;</code> followed by a code. It is turned into the section
sign Minecraft expects, so you never have to type <code>§</code>.
Use <code>&amp;&amp;</code> for a literal ampersand.</p>
<table cellpadding="4">{colours}</table>

<h3>Formatting</h3>
<table cellpadding="4">{formats}</table>

<h3>Examples</h3>
<pre>say &amp;aWelcome, &amp;e{{player}}&amp;a!
kick {{player}} &amp;cTaking a break - back soon
say &amp;6{{online}}&amp;r of &amp;6{{max}}&amp;r players online</pre>

<p style="color:#8b949e">Colours apply to chat messages such as
<code>say</code>, <code>tell</code> and kick reasons. They do not apply to
command arguments like player names.</p>
"""
