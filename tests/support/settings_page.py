"""What the Settings page is meant to hold, for the tests that count its rows.

A total or a per-card count written by hand turned red on every new setting and was only
ever bumped, so the expected rows are worked out from templates/settings.html instead
(dev/changelog/1434). The Basic set stays by hand: promoting a setting to Basic is a
decision (DESIGN.md 15.9), and a test is what makes it a visible one.
"""
import os
import re
from html.parser import HTMLParser

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         'templates', 'settings.html')

BASIC_SETTINGS = frozenset({
    'display.timezone', 'display.time_format', 'display.guide_collapse_gaps',
    'recording.dvr_output_dir', 'recording.images_dir', 'recording.filename_template', 'recording.retention_days',
    'recording.retention_delete_file', 'recording.post_process.enabled',
    'recording.post_process.format', 'recording.post_process.reencode_mode',
    'recording.move_on_complete.enabled', 'recording.move_on_complete.destination',
    'recording.metadata_sidecar.enabled',
    'recording.live_thumbnail.enabled', 'recording.logo_cache.enabled', 'ffmpeg.path',
    'sync.sync_interval_hours', 'sync.epg_days_ahead', 'channel_testing.screenshots_enabled',
    'channel_testing.window.start', 'channel_testing.window.end', 'auth.enabled',
    'integrations.home_assistant.enabled',
    'integrations.output_feed.enabled', 'integrations.output_feed.username',
    'integrations.output_feed.password', 'integrations.output_feed.mode',
})


def declared_setting_paths():
    """Every setting path templates/settings.html passes to a field_* macro, in template order.

    An empty path is a row that names no setting, such as the LAN-exposure warning. A row
    that reaches the page any other way (a loop, a hand-written .frow) or only sometimes
    (under an {% if %}) is absent from or extra to this list, and
    TierTests.test_the_page_renders_every_row_the_template_declares_once_and_in_order says so.
    """
    with open(_TEMPLATE, encoding='utf-8') as f:
        source = re.sub(r'\{#.*?#\}', '', f.read(), flags=re.S)
    return [p for p in re.findall(r"field_[a-z]+\(\s*'([^']*)'", source) if p]


class _CardParser(HTMLParser):
    """Reads the Settings page into {card id: [(group id, heading, [row paths])]}.

    Rows outside any group are listed under group id None, so a row that escapes its
    group is visible to the assertions rather than silently skipped.
    """

    def __init__(self):
        super().__init__()
        self.cards = {}
        self._stack = []   # (tag, kind) for every open section/div
        self._card = None
        self._group = None
        self._in_heading = False

    def handle_starttag(self, tag, attrs):
        if tag not in ('section', 'div', 'strong'):
            return
        a = dict(attrs)
        if tag == 'strong':
            if self._stack and self._stack[-1][1] == 'heading':
                self._in_heading = True
            return
        kind = None
        if tag == 'section' and 'data-sec' in a:
            kind = 'card'
            self._card = a['data-sec']
            self.cards[self._card] = []
        elif 'data-sub' in a and self._card:
            kind = 'group'
            self._group = [a['data-sub'], '', []]
            self.cards[self._card].append(self._group)
        elif 'sec-sub' in (a.get('class') or '').split() and self._group:
            kind = 'heading'
        elif a.get('data-path') and self._card:
            groups = self.cards[self._card]
            if self._group is None:
                if not groups or groups[-1][0] is not None:
                    groups.append([None, '', []])
                groups[-1][2].append(a['data-path'])
            else:
                self._group[2].append(a['data-path'])
        self._stack.append((tag, kind))

    def handle_endtag(self, tag):
        if tag == 'strong':
            self._in_heading = False
            return
        if tag not in ('section', 'div') or not self._stack:
            return
        _, kind = self._stack.pop()
        if kind == 'group':
            self._group = None
        elif kind == 'card':
            self._card = None

    def handle_data(self, data):
        if self._in_heading and self._group is not None:
            self._group[1] += data.strip()


def page_cards(html):
    """The Settings page as {card id: [[group id, heading, [row paths]]]}."""
    parser = _CardParser()
    parser.feed(html)
    return parser.cards


def card_paths(cards, card):
    """Every row path in one card, across its groups."""
    return [path for _, _, paths in cards[card] for path in paths]
