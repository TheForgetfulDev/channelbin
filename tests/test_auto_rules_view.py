"""The pure halves of the Rules pages (dev/changelog/1189): a search in words
(channel_search.describe_state), the `rule` action context's spelling on SearchState, the
day and "ago" phrases the pages print (tz_utils.day_label / ago), and the guide strip above
the list (auto_rules_view.guide_strip). The pages themselves are
tests/test_rules_page_conformance.py and tests/test_rule_page_conformance.py.

    python3 -m unittest tests.test_auto_rules_view
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.datastructures import MultiDict  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import auto_rules, auto_rules_view, db  # noqa: E402
from app.channel_search import (  # noqa: E402
    SearchState, describe_state, describe_value, rule_context_value,
)
from app.database import EPGEntry  # noqa: E402
from app.tz_utils import ago, day_label, format_clock  # noqa: E402

NY = ZoneInfo('America/New_York')


def state(qs):
    return SearchState.from_params(MultiDict(parse_qsl(qs, keep_blank_values=True)))


def lines(described):
    return [(ln['label'], ln['neg'], [(v['text'], v['gone']) for v in ln['values']])
            for ln in described['lines']]


class DescribeStateTests(unittest.TestCase):

    def test_text_and_where_it_is_searched(self):
        d = describe_state(state('grain=airings&q=%22Cup+Series%22+-Practice&in=epg-title'),
                           tz=NY, h24=False)
        self.assertEqual(lines(d), [
            ('Text', False, [('"Cup Series" -Practice', False)]),
            ('Searched in', False, [('Program title', False)])])
        self.assertEqual(d['summary'], '"Cup Series" -Practice in Program title')

    def test_the_default_scope_is_listed_but_not_repeated_in_the_summary(self):
        d = describe_state(state('grain=airings&q=cup'), tz=NY, h24=False)
        self.assertEqual(d['lines'][1]['values'][0]['text'],
                         'Program title, Episode / subtitle, Description')
        self.assertEqual(d['summary'], 'cup')

    def test_match_any_is_said(self):
        d = describe_state(state('grain=airings&q=cup+race&match=any'), tz=NY, h24=False)
        self.assertIn(('Match', False, [('Any of the words', False)]), lines(d))

    def test_every_filter_in_registry_order_with_includes_and_excludes(self):
        d = describe_state(state(
            'grain=airings&f.tag=live&x.group=CW&f.acct=3&f.chan=9&f.other=guide&f.health=good'
            '&f.when=next:7:days&f.dow=sat&f.dow=sun&f.tod=tod:18:00..&f.duration=dur:120..'),
            account_names={3: 'Main'}, channel_names={9: 'NBC East'}, tz=NY, h24=False)
        self.assertEqual(lines(d), [
            ('When', False, [('Next 7 days', False)]),
            ('Day of week', False, [('Saturday', False), ('Sunday', False)]),
            ('Time of day', False, [('6:00 PM or later', False)]),
            ('Program length', False, [('Longer than 2 hours', False)]),
            ('Tag', False, [('live', False)]),
            ('Account', False, [('Main', False)]),
            ('Health', False, [('Good', False)]),
            ('Channel group', True, [('CW', False)]),
            ('Status', False, [('In your guide', False)]),
            ('Channel', False, [('NBC East', False)])])
        self.assertEqual(d['summary'], 'next 7 days, Saturday or Sunday, 6:00 PM or later, '
                                       'longer than 2 hours, tag live, account Main, '
                                       'health Good, not channel group CW, in your guide, '
                                       'channel NBC East')

    def test_a_value_that_no_longer_exists_is_marked(self):
        d = describe_state(state('grain=airings&q=cup&f.tag=live&f.tag=gone'),
                           gone={'tag': {'gone'}}, tz=NY, h24=False)
        self.assertEqual(lines(d)[2], ('Tag', False, [('live', False), ('gone', True)]))

    def test_standing_options_that_differ_from_the_default(self):
        d = describe_state(state('grain=airings&q=cup&standing=showhidden&standing=grpdedup'
                                 '&standing=shownoepg&standing=showuntested'), tz=NY, h24=False)
        self.assertEqual(lines(d)[2:], [('Show hidden channels', False, [('On', False)])])
        self.assertTrue(d['summary'].endswith('show hidden channels on'))

    def test_a_search_with_nothing_in_it(self):
        d = describe_state(state('grain=airings'), tz=NY, h24=False)
        self.assertEqual(lines(d), [('Search', False, [('Every upcoming showing', False)])])

    def test_value_words(self):
        cases = [
            ('when', 'today', 'Today'), ('when', 'next:1:hours', 'Next 1 hour'),
            ('when', 'next:3:days', 'Next 3 days'), ('when', 'next:x', 'next:x'),
            ('when', 'custom:2026-10-18T14:00..', 'Sun Oct 18, 2:00 PM EDT to any time'),
            ('tod', 'tod:22:00..02:00', '10:00 PM to 2:00 AM'),
            ('tod', 'tod:..09:00', '9:00 AM or earlier'), ('tod', 'tod:18:00..18:00', '6:00 PM'),
            ('duration', 'dur:30..90', '30 minutes to 90 minutes'),
            ('duration', 'dur:..60', 'Shorter than 1 hour'),
            ('health', 'untested', 'Never tested'), ('group', '__any__', 'Any group'),
            ('acct', '5', '#5'), ('chan', 'x', '#x'), ('cat', 'Sports', 'Sports'),
        ]
        for key, value, want in cases:
            self.assertEqual(describe_value(key, value, tz=NY, h24=False), want, (key, value))
        self.assertEqual(describe_value('tod', 'tod:18:00..', tz=NY, h24=True), '18:00 or later')


class RuleContextTests(unittest.TestCase):
    """The third action context (DESIGN-channel-search.md §3): carried by the URL contract,
    never part of a rule's stored spelling."""

    def test_its_values(self):
        self.assertEqual(rule_context_value('new'), 'new')
        self.assertEqual(rule_context_value('7'), '7')
        self.assertEqual(rule_context_value(' 007 '), '7')
        for bad in ('0', '-3', 'x', '', None, '7x'):
            self.assertIsNone(rule_context_value(bad), bad)

    def test_it_round_trips_and_narrows_nothing(self):
        st = state('grain=airings&q=cup&rule=12')
        self.assertEqual(st.rule, '12')
        self.assertIn(('rule', '12'), st.to_params())
        self.assertEqual(state(urlencode(st.to_params())), st)
        self.assertEqual(st.filters, ())
        self.assertIsNone(state('grain=airings&rule=bogus').rule)

    def test_a_rule_never_stores_it(self):
        _state, params = auto_rules.clean_search('grain=airings&q=cup&rule=new&replace_rec=4')
        self.assertNotIn('rule', dict(parse_qsl(params)))
        self.assertNotIn('replace_rec', dict(parse_qsl(params)))
        self.assertIn('rule', auto_rules._PAGE_ONLY)

    def test_the_saved_search_identity_ignores_it(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'static', 'js', 'channel-search.js'), encoding='utf-8') as fh:
            js = fh.read()
        self.assertIn("const IDENTITY_SKIP = ['page', 'per_page', 'facets', 'add_to_group', "
                      "'replace_rec', 'rule'];", js)


class TimePhraseTests(unittest.TestCase):

    def test_day_label(self):
        now = datetime(2026, 10, 16, 1, 0)       # Thu Oct 15, 9:00 PM EDT
        self.assertEqual(day_label(datetime(2026, 10, 16, 2, 0), now, NY), 'Today')
        self.assertEqual(day_label(datetime(2026, 10, 16, 15, 0), now, NY), 'Tomorrow')
        self.assertEqual(day_label(datetime(2026, 10, 15, 3, 0), now, NY), 'Yesterday')
        self.assertEqual(day_label(datetime(2026, 10, 18, 18, 0), now, NY), 'Sun Oct 18')
        self.assertEqual(day_label(datetime(2027, 1, 3, 18, 0), now, NY), 'Jan 3, 2027')

    def test_ago(self):
        now = datetime(2026, 10, 16, 1, 0)
        self.assertEqual(ago(None, now), 'never')
        self.assertEqual(ago(None, now, none_value='Never'), 'Never')
        self.assertEqual(ago(now - timedelta(seconds=20), now), 'just now')
        self.assertEqual(ago(now - timedelta(minutes=1), now), '1 minute ago')
        self.assertEqual(ago(now - timedelta(minutes=45), now), '45 minutes ago')
        self.assertEqual(ago(now - timedelta(hours=1), now), '1 hour ago')
        self.assertEqual(ago(now - timedelta(hours=30), now), '30 hours ago')
        self.assertEqual(ago(now - timedelta(days=1), now), '24 hours ago')
        self.assertEqual(ago(now - timedelta(days=3), now), '3 days ago')

    def test_format_clock_takes_the_format_from_its_caller(self):
        self.assertEqual(format_clock(18, 5, h24=True), '18:05')
        self.assertEqual(format_clock(18, 5, h24=False), '6:05 PM')
        self.assertEqual(format_clock(0, 0, h24=False), '12:00 AM')


class GuideStripTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.now = datetime(2026, 10, 16, 1, 0)

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def strip(self):
        return auto_rules_view.guide_strip(self.now, NY, False)

    def listing(self, ch, days):
        start = self.now + timedelta(days=days)
        db.session.add(EPGEntry(channel_id=ch.id, title='Race', start_time=start,
                                stop_time=start + timedelta(hours=1)))
        db.session.commit()

    def test_no_accounts(self):
        s = self.strip()
        self.assertEqual(s['tone'], 'warn')
        self.assertIn('no accounts yet', s['title'])

    def test_an_empty_guide_says_what_would_refresh_it(self):
        seed.make_account(name='A', sync_enabled=False)
        db.session.commit()
        s = self.strip()
        self.assertEqual(s['tone'], 'warn')
        self.assertEqual(s['title'], 'The guide is empty, so no rule can schedule anything.')
        self.assertIn('automatic sync is off on the only account', s['sub'])

    def test_a_short_guide_with_nothing_to_refresh_it(self):
        acc = seed.make_account(name='A', sync_enabled=False)
        self.listing(seed.make_channel(acc), 0.5)
        s = self.strip()
        self.assertEqual(s['tone'], 'warn')
        self.assertIn('The guide ends ', s['title'])
        self.assertIn('and nothing will refresh it', s['title'])

    def test_a_healthy_guide_names_the_next_refresh(self):
        acc = seed.make_account(name='A', sync_enabled=True,
                                next_sync_at=self.now + timedelta(hours=5))
        seed.make_account(name='B', sync_enabled=False)
        self.listing(seed.make_channel(acc), 3)
        s = self.strip()
        self.assertEqual(s['tone'], 'muted')
        self.assertIn('The guide reaches ', s['title'])
        self.assertIn('; the next refresh is Fri Oct 16, 2:00 AM EDT.', s['title'])

    def test_a_healthy_guide_nothing_will_refresh(self):
        acc = seed.make_account(name='A', sync_enabled=False)
        self.listing(seed.make_channel(acc), 3)
        s = self.strip()
        self.assertEqual(s['tone'], 'muted')
        self.assertIn('automatic sync is off on the only account, so nothing will refresh it.',
                      s['title'])


if __name__ == '__main__':
    unittest.main()
