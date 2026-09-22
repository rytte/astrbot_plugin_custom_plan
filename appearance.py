"""Trusted layout and theme registries shared by validation and rendering."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    width: int
    page_size: int
    stylesheet: str
    template_prefix: str


@dataclass(frozen=True)
class Theme:
    name: str
    stylesheet: str


LAYOUTS = {
    "mobile": Layout(640, 8, "layouts/mobile.css", "mobile/"),
    "desktop": Layout(1000, 20, "layouts/desktop.css", ""),
}
DEFAULT_THEME = "forest"
THEMES = {
    "forest": Theme("清新绿", "themes/forest.css"),
    "midnight": Theme("深色", "themes/midnight.css"),
}
