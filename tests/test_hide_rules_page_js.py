"""Tier 0 - the Hide Channels page's Account pills, driven in a real DOM.

Picking an account used to list only the rules written for that account, each showing its
total across every account, so with every rule written for all accounts there was no way to
see what a rule hides on one of them. An account pill now lists every rule that reaches the
account - its own and the all-accounts ones - with Matches counted on the picked accounts
alone, and the summary tiles follow; "All" keeps listing the rules written for every account
and never combines with an account pill (dev/changelog/1678). All of it is client-side
rendering, so the server markup cannot show it.

tests/support/hide_rules_page.mjs runs the shipped util.js and hide-rules.js against the page
the Flask app really rendered and reports; every assertion lives here.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_hide_rules_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import channel_hiding, db  # noqa: E402
from app.database import (  # noqa: E402
    ChannelHideRule, HIDE_TARGET_CATEGORY_EXACT, HIDE_TARGET_CATEGORY_GLOB,
    HIDE_TARGET_NAME_GLOB)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'hide_rules_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _seed():
    """Three accounts and four rules:

    - `AR|*`, every account: Alpha 2 hidden, Beta 1 kept visible (in the guide).
    - `US| *` (category), every account: Gamma 1 hidden.
    - `UK| News` (exact), Alpha only: Alpha 1 hidden.
    - `ZZ*`, Gamma only, never counted by account (a pass that has not run since it was saved).
    """
    ChannelHideRule.query.delete()
    alpha = seed.make_account(name='Alpha')
    beta = seed.make_account(name='Beta')
    gamma = seed.make_account(name='Gamma')
    seed.make_channel(alpha, name='AR| One', category_name='AR| News')
    seed.make_channel(alpha, name='AR| Two', category_name='AR| News')
    seed.make_channel(alpha, name='BBC', category_name='UK| News')
    seed.make_channel(beta, name='AR| Three', category_name='AR| News', in_guide=True)
    seed.make_channel(gamma, name='CNN', category_name='US| News')
    rules = [ChannelHideRule(target=HIDE_TARGET_NAME_GLOB, pattern='AR|*'),
             ChannelHideRule(target=HIDE_TARGET_CATEGORY_GLOB, pattern='US| *'),
             ChannelHideRule(target=HIDE_TARGET_CATEGORY_EXACT, pattern='UK| News',
                             account_id=alpha.id),
             ChannelHideRule(target=HIDE_TARGET_NAME_GLOB, pattern='ZZ*', account_id=gamma.id)]
    db.session.add_all(rules)
    db.session.flush()
    channel_hiding.recompute()
    channel_hiding.refresh_rule_stats()
    rules[3].account_counts = None
    db.session.commit()
    return {'accounts': {'Alpha': alpha.id, 'Beta': beta.id, 'Gamma': gamma.id},
            'rules': {r.pattern: r.id for r in rules}}


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='hide_rules_page_js_')
    try:
        with t.app.app_context():
            ids = _seed()
        resp = t.app.test_client().get('/channels/hide-rules')
        if resp.status_code != 200:
            raise AssertionError(f'/channels/hide-rules answered {resp.status_code}')
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(resp.get_data(as_text=True))
        with open(os.path.join(tmp, 'ids.json'), 'w', encoding='utf-8') as f:
            json.dump(ids, f)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class AccountPillTests(unittest.TestCase):

    def _view(self, name):
        got = _observe()[name]
        self.assertNotIn('error', got, got.get('error'))
        self.assertEqual(got['errors'], [], name)
        return got

    def _matches(self, view):
        return {r['pattern']: (r['matches'], r['kept']) for r in view['rows']}

    def test_with_no_pill_every_rule_shows_its_total(self):
        v = self._view('boot')
        self.assertEqual(self._matches(v), {
            'AR|*': ('3', '+1 kept visible'), 'US| *': ('1', None),
            'UK| News': ('1', None), 'ZZ*': ('0', None)})

    def test_the_tiles_open_on_what_the_server_rendered(self):
        """Server-rendered first paint equals the nothing-picked state JS draws."""
        v = self._view('boot')
        self.assertEqual(v['tiles'], v['tiles_served'])
        self.assertEqual((v['tiles']['hidden'], v['tiles']['deferred'], v['tiles']['offered'],
                          v['tiles']['rules_on'], v['tiles']['rules']), ('4', '1', '1', '4', '4'))

    def test_an_account_pill_shows_the_all_accounts_rules_counted_on_that_account(self):
        v = self._view('one_account')
        self.assertEqual(v['pills'], ['Beta'])
        self.assertEqual(self._matches(v), {'AR|*': ('1', '+1 kept visible'),
                                            'US| *': ('0', None)})
        tip = next(r['tip'] for r in v['rows'] if r['pattern'] == 'AR|*')
        self.assertEqual(tip, '1 of 1 channels on Beta')

    def test_an_account_pill_rescopes_the_tiles(self):
        t = self._view('one_account')['tiles']
        self.assertEqual((t['hidden'], t['deferred'], t['offered'], t['rules_on'], t['rules']),
                         ('0', '1', '1', '2', '2'))
        self.assertTrue(t['deferred_warn'])
        self.assertEqual((t['hidden_sub'], t['offered_sub']),
                         ('channels on Beta', 'channels on Beta'))

    def test_two_accounts_are_summed_and_split_in_the_tip(self):
        v = self._view('two_accounts')
        self.assertEqual(v['pills'], ['Beta', 'Gamma'])
        m = self._matches(v)
        self.assertEqual(m['AR|*'], ('1', '+1 kept visible'))
        self.assertEqual(m['US| *'], ('1', None))
        tip = next(r['tip'] for r in v['rows'] if r['pattern'] == 'AR|*')
        self.assertEqual(tip, '1 of 2 channels on Beta and Gamma: Beta 1, Gamma 0')
        t = v['tiles']
        self.assertEqual((t['hidden'], t['deferred'], t['offered']), ('1', '1', '1'))
        self.assertEqual(t['hidden_sub'], 'channels on Beta and Gamma')

    def test_a_rule_scoped_to_one_picked_account_counts_only_there(self):
        v = self._view('account_after_all')
        self.assertEqual(self._matches(v), {'AR|*': ('2', None), 'US| *': ('0', None),
                                            'UK| News': ('1', None)})
        tip = next(r['tip'] for r in v['rows'] if r['pattern'] == 'UK| News')
        self.assertEqual(tip, '1 of 3 channels on Alpha')

    def test_a_rule_never_counted_by_account_says_so_rather_than_zero(self):
        v = self._view('two_accounts')
        row = next(r for r in v['rows'] if r['pattern'] == 'ZZ*')
        self.assertEqual(row['matches'], '-')
        self.assertIn('Not counted by account yet', row['tip'])

    def test_all_lists_the_all_accounts_rules_at_their_totals_and_drops_the_account(self):
        v = self._view('all_after_account')
        self.assertEqual(v['pills'], ['All'])
        self.assertEqual(self._matches(v), {'AR|*': ('3', '+1 kept visible'),
                                            'US| *': ('1', None)})
        t = v['tiles']
        self.assertEqual((t['hidden'], t['offered'], t['hidden_sub']),
                         ('4', '1', 'channels on every account'))

    def test_an_account_pill_drops_all(self):
        self.assertEqual(self._view('account_after_all')['pills'], ['Alpha'])

    def test_clearing_the_pill_puts_the_totals_back(self):
        v = self._view('cleared')
        boot = self._view('boot')
        self.assertEqual(v['pills'], [])
        self.assertEqual(v['rows'], boot['rows'])
        self.assertEqual(v['tiles'], boot['tiles'])

    def test_matches_sorts_on_the_scoped_count(self):
        v = self._view('sorted_by_matches')
        self.assertEqual(v['asc'], ['US| *', 'UK| News', 'AR|*'])
        self.assertEqual(v['desc'], ['AR|*', 'UK| News', 'US| *'])


if __name__ == '__main__':
    unittest.main()
