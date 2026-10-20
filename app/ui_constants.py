"""Shared UI constants used across route blueprints."""

# The color an account or tag gets when none was chosen: the model defaults, the two
# write routes and the two pickers' preselected swatch all read it from here, and the
# pages hand it to their modals (tags.html's TAG_CONFIG, the account JSON) rather than
# the JS carrying a copy. It is the first preset swatch.
DEFAULT_COLOR = '#58a6ff'

# (hex, label) preset swatches for the account and tag color pickers.
PRESET_COLORS = [
    (DEFAULT_COLOR, 'Blue'),
    ('#3fb950', 'Green'),
    ('#f85149', 'Red'),
    ('#d29922', 'Yellow'),
    ('#bc8cff', 'Purple'),
    ('#ff7b72', 'Orange'),
    ('#39d353', 'Teal'),
    ('#e8b4fb', 'Pink'),
]
