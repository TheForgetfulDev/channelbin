"""The icon registry: every icon the UI draws, one Material Design Icons (MDI) icon per meaning.

A meaning (`record`, `back`, `more`) names what the icon says, never what it looks like, and
maps to exactly one MDI icon; no two meanings share one, so a picture never means two things
(`tests/test_icon_registry.py`). Templates draw one with the `icon()` Jinja global and scripts
with `util.js::icon()` - the same markup either way - from the generated sprite
`static/icons/mdi-sprite.svg`. Changing a pick is an edit here followed by
`python3 tools/build_icons.py`, which rebuilds the sprite from the pinned `@mdi/js` package.

The picks are the suggested ones from the icon picker in `dev/mockups/45-guide-program-chooser.html`,
which lists the same meanings with their alternatives (`dev/changelog/1319`, `1320`).
"""
from markupsafe import Markup, escape

SPRITE = 'icons/mdi-sprite.svg'

ICONS = {
    # The program-click chooser's actions, and the things it links to.
    'record': 'record',                          # red wherever it appears (style.css)
    'edit-recording': 'calendar-edit',
    'rec-controls': 'tune-vertical',
    'play': 'play',
    'test': 'stethoscope',
    'preview': 'television-play',
    'copy': 'content-copy',
    'airings': 'calendar-search',
    'channel': 'television',
    'group': 'link-variant',
    'recordings': 'filmstrip-box-multiple',

    # Controls.
    'add': 'plus',
    'more': 'dots-horizontal',
    'close': 'close',
    'stop': 'stop',
    'pause': 'pause',
    'resume': 'play-circle-outline',
    'refresh': 'refresh',
    'retrying': 'autorenew',
    'record-again': 'repeat',
    'restart': 'restart-alert',
    'back': 'arrow-left',
    'forward': 'arrow-right',
    'dropdown': 'menu-down',
    'sort-asc': 'arrow-up',
    'sort-desc': 'arrow-down',
    'move-up': 'chevron-up',
    'move-down': 'chevron-down',
    'expand': 'chevron-right',
    'drag': 'drag-vertical',
    'layout': 'tune-variant',
    'search': 'magnify',
    'save-search': 'bookmark-outline',
    'menu': 'menu',
    'reveal': 'eye-outline',
    'conceal': 'eye-off-outline',
    'delete': 'delete-outline',
    'health-check': 'heart-pulse',
    'logout': 'logout',

    # Status marks.
    'ok': 'check',
    'failed': 'close-circle',
    'warning': 'alert-outline',
    'blocking': 'alert-octagon-outline',
    'finishing': 'progress-check',
    'partial': 'circle-slice-6',
    'best': 'star',
    'health-score': 'gauge',
    'note': 'note-text-outline',
    'locked': 'lock-outline',
    'duplicate': 'card-multiple-outline',
    'time': 'clock-outline',
    'active': 'circle',
    'inactive': 'circle-outline',

    # The sidebar's destinations. Recordings and Channel Groups use `recordings` and `group`.
    'nav-dashboard': 'view-dashboard-outline',
    'nav-auto-record': 'calendar-sync',
    'nav-guide': 'television-guide',
    'nav-channel-search': 'text-search',
    'nav-get-started': 'rocket-launch-outline',
    'nav-accounts': 'account-key-outline',
    'nav-rec-profiles': 'movie-cog-outline',
    'nav-hc-profiles': 'clipboard-pulse-outline',
    'nav-tags': 'tag-multiple-outline',
    'nav-hide-rules': 'filter-remove-outline',
    'nav-jobs': 'timer-cog-outline',
    'nav-alerts': 'bell-outline',
    'nav-logs': 'text-box-outline',
    'nav-maintenance': 'wrench-outline',
    'nav-settings': 'cog-outline',
}


def icon_markup(name, cls, sprite_url):
    """The one spelling of an icon's markup; `util.js::icon()` writes the same string.

    An unknown name raises rather than drawing nothing, so a typo is a failed render the
    test suite sees, not a silently missing icon.
    """
    if name not in ICONS:
        raise KeyError(f'no icon named {name!r} in app/icons.py')
    classes = f'icon {cls}' if cls else 'icon'
    return Markup(f'<svg class="{escape(classes)}" data-icon="{name}" aria-hidden="true">'
                  f'<use href="{escape(sprite_url)}#i-{name}"></use></svg>')


def make_jinja_icon(url_for):
    """The `icon(name, cls='')` Jinja global, bound to the app's own static URL."""
    def icon(name, cls=''):
        return icon_markup(name, cls, url_for('static', filename=SPRITE))
    return icon
