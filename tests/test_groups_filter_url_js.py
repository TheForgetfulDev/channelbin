"""Tier 0 - the Groups tab's filter chips riding the URL, "is not" included, driven in a real
DOM (dev/changelog/1207).

The Groups tab persists the shared filter bar to its query string so a reload or a bookmark
keeps the filter: `f=<k>:<v>` for "is", and `x=<k>:<v>` for "is not" - the channel search's own
`f.`/`x.` split. A URL written before "is not" existed carries only `f=` and must keep meaning
what it meant. tests/support/groups_filter_url.mjs runs the shipped scripts against the page
the Flask app really rendered and reports; every assertion lives here.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_groups_filter_url_js
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
from app import db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'groups_filter_url.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='groups_filter_url_js_')
    try:
        with t.app.app_context():
            acct = seed.make_account(name='Acct')
            alpha = seed.make_channel(acct, name='Alpha Feed')
            beta = seed.make_channel(acct, name='Beta Feed')
            seed.make_group(name='Pair', members=[alpha], in_guide=True)
            seed.make_group(name='Other', members=[beta], in_guide=False)
            db.session.commit()
        resp = t.app.test_client().get('/channel-groups')
        if resp.status_code != 200:
            raise AssertionError(f'/channel-groups answered {resp.status_code}')
        with open(os.path.join(tmp, 'groups.html'), 'w', encoding='utf-8') as f:
            f.write(resp.get_data(as_text=True))
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
class GroupsFilterUrlTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_harness_ran_without_an_error(self):
        self.assertNotIn('threw', self.obs)
        for k in ('is_url', 'not_url', 'picked', 'removed'):
            self.assertEqual(self.obs[k]['errors'], [], k)

    # Every database also carries the app's own "TV Guide health check", which is not a
    # guide row - so it lands on the "not in the guide" side with Other.
    NOT_IN_GUIDE = ['other', 'tv guide health check']

    def test_an_f_parameter_still_reads_as_is(self):
        self.assertEqual(self.obs['is_url']['shown'], ['pair'])
        self.assertEqual(self.obs['is_url']['chips'], ['Guide row is Present'])

    def test_an_x_parameter_reads_as_is_not(self):
        self.assertEqual(sorted(self.obs['not_url']['shown']), self.NOT_IN_GUIDE)
        self.assertEqual(self.obs['not_url']['chips'], ['Guide row is not Present'])
        self.assertEqual(self.obs['not_url']['search'], '?x=guide:in')

    def test_choosing_is_not_writes_x_and_removing_it_clears_the_url(self):
        self.assertEqual(self.obs['picked']['search'], '?x=guide:in')
        self.assertEqual(sorted(self.obs['picked']['shown']), self.NOT_IN_GUIDE)
        self.assertEqual(self.obs['removed']['search'], '')
        self.assertEqual(sorted(self.obs['removed']['shown']), ['other', 'pair', 'tv guide health check'])


if __name__ == '__main__':
    unittest.main()
