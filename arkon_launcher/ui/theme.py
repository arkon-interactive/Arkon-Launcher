"""The dark theme.

Material-inspired rather than Material: layered surfaces, one accent colour,
generous corner radius and a clear type scale - but no elevation shadows or
ripple, which Qt does badly and which would fight the density this app needs.

Colours are defined once here and referenced everywhere else, so a widget that
hardcodes a hex value is a bug rather than a style choice.
"""

from __future__ import annotations

# --- Palette ------------------------------------------------------------------
# Three surface levels rather than two: the window, the panels sitting on it, and
# the inputs sitting on those. Without the third, fields disappear into cards.

BACKGROUND = "#16191d"
SURFACE = "#1e2227"
SURFACE_HIGH = "#252a31"
INPUT = "#1a1e23"

BORDER = "#2f353d"
BORDER_STRONG = "#3c434d"

TEXT = "#e4e7ea"
TEXT_MUTED = "#8b949e"
TEXT_DISABLED = "#5c646e"

ACCENT = "#4bb768"
ACCENT_HOVER = "#57c976"
ACCENT_PRESSED = "#3fa055"
ACCENT_MUTED = "#2c5c3a"

DANGER = "#e06c75"
WARNING = "#d4a244"
INFO = "#5aa9e6"

# Status colours, used by the online bubble and anything else showing state.
ONLINE = "#4bb768"
OFFLINE = "#5c646e"
PRIMED = "#d4a244"
BANNED = "#e06c75"

RADIUS = "6px"
RADIUS_SMALL = "4px"


# --- Icons --------------------------------------------------------------------
# Qt's stylesheet parser resolves url() through QFile, which has no data: scheme,
# so an inline SVG silently renders as nothing. The icons are therefore written
# out once per run and referenced by path.

_TICK = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' "
    "viewBox='0 0 16 16'><path d='M3.5 8.4l3 3 5.5-6.6' fill='none' "
    "stroke='#12331c' stroke-width='2.2' stroke-linecap='round' "
    "stroke-linejoin='round'/></svg>"
)


def _chevron(direction: str, size: int, colour: str) -> str:
    """A chevron pointing up or down, drawn to fill its box."""
    path = "M3 6l5 5 5-5" if direction == "down" else "M3 10l5-5 5 5"
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' "
        f"viewBox='0 0 16 16'><path d='{path}' fill='none' stroke='{colour}' "
        "stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/></svg>"
    )


def _icon_dir():
    from pathlib import Path
    from tempfile import gettempdir

    directory = Path(gettempdir()) / "arkon-launcher-icons"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_icon(name: str, svg: str) -> str:
    path = _icon_dir() / name
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != svg:
            path.write_text(svg, encoding="utf-8")
    except OSError:
        return ""
    # Qt wants forward slashes even on Windows.
    return str(path).replace("\\", "/")


def stylesheet() -> str:
    tick = _write_icon("tick.svg", _TICK)
    tick_rule = f'image: url("{tick}");' if tick else ""
    chevron = _write_icon("chevron-down.svg", _chevron("down", 12, TEXT_MUTED))
    spin_up = _write_icon("spin-up.svg", _chevron("up", 8, TEXT_MUTED))
    spin_down = _write_icon("spin-down.svg", _chevron("down", 8, TEXT_MUTED))
    return f"""
/* --- Base ------------------------------------------------------------- */
QWidget {{
    background: {BACKGROUND};
    color: {TEXT};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BACKGROUND}; }}

QLabel {{ background: transparent; }}
QLabel:disabled {{ color: {TEXT_DISABLED}; }}

QToolTip {{
    background: {SURFACE_HIGH};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL};
    padding: 6px 8px;
}}

/* --- Panels ----------------------------------------------------------- */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QGroupBox::indicator {{ width: 16px; height: 16px; }}

/* --- Tabs ------------------------------------------------------------- */
QTabWidget::pane {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    top: -1px;
}}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 8px 16px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}

/* --- Buttons ---------------------------------------------------------- */
QPushButton {{
    background: {SURFACE_HIGH};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS};
    padding: 7px 14px;
    color: {TEXT};
    font-weight: 600;
}}
QPushButton:hover {{ background: #2b313a; border-color: #4a525d; }}
QPushButton:pressed {{ background: #1c2126; }}
QPushButton:disabled {{
    background: {SURFACE};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QPushButton:default {{ border-color: {ACCENT_MUTED}; }}

QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS};
    padding: 4px;
    color: {TEXT_MUTED};
}}
QToolButton:hover {{ background: {SURFACE_HIGH}; color: {TEXT}; }}

/* --- Inputs ----------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {INPUT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 6px 8px;
    color: {TEXT};
    selection-background-color: {ACCENT_MUTED};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_DISABLED};
    background: {SURFACE};
}}
QLineEdit[readOnly="true"] {{ background: {SURFACE}; color: {TEXT_MUTED}; }}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    background: transparent;
    width: 22px;
}}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_HIGH};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS};
    selection-background-color: {ACCENT_MUTED};
    padding: 4px;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    background: {SURFACE_HIGH};
    border: none;
    width: 18px;
}}
QSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: {RADIUS};
}}
QSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: {RADIUS};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {BORDER_STRONG}; }}
QSpinBox::up-arrow {{ image: url("{spin_up}"); width: 8px; height: 8px; }}
QSpinBox::down-arrow {{ image: url("{spin_down}"); width: 8px; height: 8px; }}

/* --- Checkboxes ------------------------------------------------------- */
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL};
    background: {INPUT};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    {tick_rule}
}}
QCheckBox::indicator:disabled {{ background: {SURFACE}; border-color: {BORDER}; }}

/* --- Tables and lists -------------------------------------------------- */
QTableWidget, QTreeWidget, QListWidget {{
    background: {INPUT};
    alternate-background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    gridline-color: {BORDER};
    outline: none;
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 5px 6px;
    border: none;
}}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {ACCENT_MUTED};
    color: {TEXT};
}}
QTableWidget::item:hover, QTreeWidget::item:hover, QListWidget::item:hover {{
    background: {SURFACE_HIGH};
}}
QHeaderView::section {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER_STRONG};
    padding: 7px 6px;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.5px;
}}
QHeaderView::section:hover {{ color: {TEXT}; }}

/* --- Scrollbars -------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #4d5661; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG}; border-radius: 5px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: #4d5661; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* --- Misc -------------------------------------------------------------- */
QProgressBar {{
    background: {INPUT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SMALL};
    height: 16px;
    text-align: center;
    color: {TEXT};
    font-size: 11px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}

QMenu {{
    background: {SURFACE_HIGH};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS};
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: {RADIUS_SMALL}; }}
QMenu::item:selected {{ background: {ACCENT_MUTED}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QStatusBar {{ background: {SURFACE}; color: {TEXT_MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}

QScrollArea {{ background: transparent; border: none; }}
"""


def apply(app) -> None:
    """Install the theme on a QApplication."""
    from PySide6.QtGui import QColor, QPalette

    app.setStyle("Fusion")

    # Fusion reads the palette for things the stylesheet does not reach, such as
    # the text cursor and some native dialogs.
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(INPUT))
    palette.setColor(QPalette.AlternateBase, QColor(SURFACE))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(SURFACE_HIGH))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT_MUTED))
    palette.setColor(QPalette.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.ToolTipBase, QColor(SURFACE_HIGH))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.PlaceholderText, QColor(TEXT_DISABLED))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_DISABLED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_DISABLED))
    app.setPalette(palette)

    app.setStyleSheet(stylesheet())
