"""Shared UI constants used across route blueprints."""

# (hex, label) preset swatches for the account and tag color pickers. Each one is a color
# its label names and no two read alike: Teal was a second green and Orange a pink until
# dev/changelog/1291. None is a red, orange, yellow, green or blue, the colors the app
# already spends on status and alerts, so an account or tag never reads as a warning.
PRESET_COLORS = [
    ('#bc8cff', 'Purple'),
    ('#2bb3a6', 'Teal'),
    ('#f778ba', 'Pink'),
    ('#d946ef', 'Fuchsia'),
    ('#8f6a50', 'Brown'),
    ('#9aa3ae', 'Gray'),
]

# The color an account or tag gets when none was chosen: the model defaults, the two
# write routes and the two pickers' preselected swatch all read it from here, and the
# pages hand it to their modals (tags.html's TAG_CONFIG, the account JSON) rather than
# the JS carrying a copy. It is the first preset swatch.
DEFAULT_COLOR = PRESET_COLORS[0][0]


def preset_color(label: str) -> str:
    """The hex of the preset swatch named `label`."""
    return next(hexval for hexval, name in PRESET_COLORS if name == label)
