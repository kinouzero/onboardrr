"""Appearance palettes and read-only macOS preference detection."""

import subprocess
import sys

from textual.theme import Theme


GRAPHITE = Theme(
    name="graphite",
    primary="#edb85d",
    accent="#edb85d",
    background="#181919",
    surface="#222423",
    panel="#2c2e2c",
    foreground="#f1eee8",
    success="#a4d3a3",
    warning="#f3c97f",
    error="#ffa199",
    variables={
        "onboardrr-muted": "#c1c3c0",
        "onboardrr-border": "#656965",
        "onboardrr-highlight": "#29261f",
        "onboardrr-primary-ink": "#211a0c",
        "onboardrr-warning-ink": "#271b09",
        "block-cursor-background": "#edb85d",
        "block-cursor-foreground": "#211a0c",
        "footer-key-foreground": "#edb85d",
    },
)
IVORY = Theme(
    name="ivory",
    primary="#4b42a3",
    accent="#5146a8",
    background="#f5f1e8",
    surface="#fffdf8",
    panel="#e9e3f0",
    foreground="#25263a",
    success="#256843",
    warning="#885400",
    error="#a52d45",
    dark=False,
    variables={
        "onboardrr-muted": "#534d61",
        "onboardrr-border": "#897eaa",
        "onboardrr-highlight": "#ece7f8",
        "onboardrr-primary-ink": "#ffffff",
        "onboardrr-warning-ink": "#ffffff",
        "block-cursor-background": "#4b42a3",
        "block-cursor-foreground": "#ffffff",
        "footer-key-foreground": "#5146a8",
    },
)


def system_theme() -> str | None:
    """Return the current macOS appearance, or None when it is unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return {"dark": GRAPHITE.name, "light": IVORY.name}.get(result.stdout.strip().casefold())
    error = result.stderr.casefold()
    if result.returncode == 1 and ("could not find key" in error or "does not exist" in error):
        return IVORY.name
    return None
