"""Units for the filename templater (app/accounts.py::render_filename_template).

The variable-substitution / sanitization / separator-collapse half is pure (no DB is
touched when the template has no {tag:...} tokens and an explicit tag_cleanup is passed).
The {tag:name} conditional-insert and tag_cleanup remove/replace halves resolve against
Tag / TagPattern rows, so those run on a throwaway make_test_app() DB.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.accounts import render_filename_template  # noqa: E402
from app.database import Tag, TagPattern  # noqa: E402
from app.tz_utils import UTC  # noqa: E402

_START = datetime(2026, 7, 17, 14, 30)   # naive UTC
_STOP = datetime(2026, 7, 17, 15, 30)


def _program(**over):
    p = {
        'title': 'The Big Game',
        'sub_title': 'Finals',
        'description': 'x' * 200,
        'channel_name': 'ESPN',
        'category': 'Sports',
        'start_time': _START,
        'stop_time': _STOP,
    }
    p.update(over)
    return p


class SubstitutionTests(unittest.TestCase):
    """Pure - no DB. tag_cleanup=[] and no {tag:} tokens means no Tag.query / load_config.

    All pass tz=UTC explicitly: these cases exist to test substitution mechanics (the {x}
    replacement, sanitization, separator collapse), not timezone conversion - that has its
    own class below. Without an explicit tz these would fall back to get_display_tz() and
    assert against whatever timezone happens to be configured.
    """

    def test_basic_variables(self):
        out = render_filename_template('{date} {title} {channel}', _program(), tag_cleanup=[],
                                       tz=UTC)
        self.assertEqual(out, '2026-07-17 The Big Game ESPN')

    def test_start_end_times_24h(self):
        out = render_filename_template('{start_time}-{end_time}', _program(), tag_cleanup=[],
                                       tz=UTC)
        self.assertEqual(out, '1430-1530')

    def test_description_truncated_to_80(self):
        out = render_filename_template('{description}', _program(), tag_cleanup=[], tz=UTC)
        self.assertEqual(out, 'x' * 80)

    def test_illegal_filename_chars_sanitized(self):
        out = render_filename_template('{title}', _program(title='A/B:C*D?"E<F>G|H'),
                                       tag_cleanup=[], tz=UTC)
        for bad in '/:*?"<>|':
            self.assertNotIn(bad, out)

    def test_empty_program_fields_collapse_separators(self):
        # missing sub_title leaves "Title -  - Channel" → collapsed to single " - "
        out = render_filename_template('{title} - {sub_title} - {channel}',
                                       _program(sub_title=''), tag_cleanup=[], tz=UTC)
        self.assertEqual(out, 'The Big Game - ESPN')

    def test_leading_trailing_separators_trimmed(self):
        out = render_filename_template('{sub_title} - {title}', _program(sub_title=''),
                                       tag_cleanup=[], tz=UTC)
        self.assertEqual(out, 'The Big Game')

    def test_missing_times_render_empty(self):
        out = render_filename_template('{title}{start_time}',
                                       _program(start_time=None, stop_time=None),
                                       tag_cleanup=[], tz=UTC)
        self.assertEqual(out, 'The Big Game')


class TimezoneConversionTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-05: {date}/{start_time}/{end_time} used to format the
    stored naive-UTC datetimes directly, so a program airing at 8:00 PM ET on Aug 3 was
    named for Aug 4 UTC. render_filename_template must convert through `tz` first."""

    def test_utc_datetime_renders_in_the_given_timezone(self):
        # 8:00 PM ET on Aug 3 is 00:00 UTC on Aug 4 - the exact case from the bug report.
        start = datetime(2026, 8, 4, 0, 0)   # naive UTC
        stop = datetime(2026, 8, 4, 1, 0)
        out = render_filename_template(
            '{date} {start_time}-{end_time}',
            _program(start_time=start, stop_time=stop),
            tag_cleanup=[], tz=ZoneInfo('America/New_York'))
        self.assertEqual(out, '2026-08-03 2000-2100')

    def test_utc_tz_is_a_no_op(self):
        out = render_filename_template('{date} {start_time}-{end_time}', _program(),
                                       tag_cleanup=[], tz=UTC)
        self.assertEqual(out, '2026-07-17 1430-1530')

    def test_missing_tz_falls_back_to_the_display_timezone(self):
        """No explicit tz -> get_display_tz(), same fallback convention as tag_cleanup.

        Patches get_display_tz rather than relying on config.yaml (real or test-sandboxed):
        get_display_tz() is called from a bare `render_filename_template(...)` with no app
        context here, and even under one, make_test_app's overrides are not visible to a
        runtime load_config() call - it would read the real config.yaml (CLAUDE.md
        Testing section)."""
        with mock.patch('app.tz_utils.get_display_tz',
                        return_value=ZoneInfo('America/New_York')):
            out = render_filename_template(
                '{date} {start_time}',
                _program(start_time=datetime(2026, 8, 4, 0, 0)), tag_cleanup=[])
        self.assertEqual(out, '2026-08-03 2000')


class TagTokenTests(unittest.TestCase):
    """{tag:name} conditional insert + tag_cleanup - resolve against Tag/TagPattern rows."""

    def setUp(self):
        self.t = make_test_app()
        tag = Tag(name='4K')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern='UHD'))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_tag_token_inserts_name_when_pattern_matches(self):
        out = render_filename_template('{title} {tag:4K}',
                                       _program(title='Movie UHD Cut'), tag_cleanup=[])
        self.assertEqual(out, 'Movie UHD Cut 4K')

    def test_tag_token_empty_when_no_match_and_collapses(self):
        out = render_filename_template('{title} - {tag:4K} - {channel}',
                                       _program(title='Plain Movie'), tag_cleanup=[])
        self.assertEqual(out, 'Plain Movie - ESPN')

    def test_tag_cleanup_remove_strips_pattern_from_output(self):
        out = render_filename_template('{title}', _program(title='Movie UHD Cut'),
                                       tag_cleanup=[('4K', 'remove')])
        self.assertNotIn('UHD', out)

    def test_tag_cleanup_replace_swaps_pattern_for_tag_name(self):
        out = render_filename_template('{title}', _program(title='Movie UHD Cut'),
                                       tag_cleanup=[('4K', 'replace')])
        self.assertIn('4K', out)
        self.assertNotIn('UHD', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
