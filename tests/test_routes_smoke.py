"""Tier 2 route smoke sweep - the single highest-leverage regression net.

A large share of this codebase's worst shipped bugs were code paths that had never
executed even once before shipping: a `NameError: ET`, a bad `url_for()` target, a Jinja
`UndefinedError`, a template referencing a dict method as a key, arithmetic on a NULL
column. Every one of them is a 500 on first request. This test mechanically GETs every
GET-able route (parameterized ones against seeded ids, including a Recording in *every*
status and an all-NULL ChannelTest) and asserts the response is never a 500.

It guards the whole "never-executed-path" family (Fable defect Classes D and F), e.g.:
  * BUGS.md 2026-06-28 - url_for() referencing a non-existent endpoint name (500 on render)
  * BUGS.md 2026-06-28 - request.args.get('x', type=int) raising UndefinedError in a template
  * the all-NULL ChannelTest / arithmetic-on-nullable-column 500s
  * the ?dup_block=… / ?account_id=… query-param variants that previously 500'd

Runs against a throwaway temp SQLite DB seeded by seed_all() - never the live dvr.db.

    python3 -m unittest tests.test_routes_smoke
"""
import itertools
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import seed_all  # noqa: E402

# Endpoints deliberately skipped by the GET sweep.
#   * static - Flask's built-in file server, not app code.
#   * the three SSE endpoints - they return an infinite text/event-stream and would
#     block the test client forever (they are exercised by Tier 3, not here).
_SKIP_ENDPOINTS = {
    'static',
    'dashboard.stream_all',
    'dashboard.stream_recording',
    'logs.logs_stream',
}

# A 500 is the only hard failure. Everything else is a legitimate outcome of hitting a
# route with a dummy/missing id or without its required query params. 503 is included
# because recordings.live_thumbnail deliberately returns it for an IN_PROGRESS recording
# whose thumbnail hasn't been captured yet (a designed "not ready", not a crash).
_ALLOWED = {200, 302, 304, 400, 401, 403, 404, 503}

_PLACEHOLDER = re.compile(r'<[^>]+>')


def _build_url(rule, valuemap):
    """Substitute concrete values into a rule's <converter:name> placeholders."""
    def repl(m):
        token = m.group(0)[1:-1]  # strip the surrounding <>
        conv, _, name = token.partition(':')
        argname = name or conv
        return str(valuemap[argname])
    return _PLACEHOLDER.sub(repl, rule.rule)


class RouteSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app + one seed for the whole sweep - a per-test app would blow the <30s budget.
        cls.t = make_test_app()
        cls.seeded = seed_all()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _candidates(self, argname):
        """Seeded id list for a route argument; a dummy that 404s for unseeded args."""
        s = self.seeded
        if argname == 'recording_id':
            return [r.id for r in s.recordings.values()] + [s.group_recording.id]
        if argname == 'channel_id':
            return [s.guide_channel.id] + [m.id for m in s.group_members]
        if argname == 'group_id':
            return [s.group.id]
        if argname == 'account_id':
            return [s.account.id]
        # Unseeded args (job_id, profile_id, tag_id, alert_id, search_id, name,
        # filename, ...): a value that resolves to a real route but a missing row → 404.
        return [1]

    def _get_urls(self):
        """(url, endpoint) for every GET-able, non-skipped route × its seeded id combos."""
        urls = []
        for rule in self.t.app.url_map.iter_rules():
            if rule.endpoint in _SKIP_ENDPOINTS:
                continue
            if 'GET' not in rule.methods:
                continue
            argnames = list(rule.arguments)
            if not argnames:
                urls.append((rule.rule, rule.endpoint))
                continue
            value_lists = [self._candidates(a) for a in argnames]
            for combo in itertools.product(*value_lists):
                valuemap = dict(zip(argnames, combo))
                urls.append((_build_url(rule, valuemap), rule.endpoint))
        return urls

    def test_no_route_returns_500(self):
        failures = []
        for url, endpoint in self._get_urls():
            resp = self.t.client.get(url)
            if resp.status_code not in _ALLOWED:
                failures.append(f'{resp.status_code} {endpoint} {url}')
        self.assertEqual(failures, [], 'routes returned a disallowed status:\n' + '\n'.join(failures))

    def test_query_param_variants_do_not_500(self):
        """Query-param shapes that individually 500'd in the past (they exercise
        code the bare-path sweep never reaches: type=int coercion, dup-block banner,
        account filters, EPG/search windows)."""
        s = self.seeded
        now = datetime.utcnow()
        start = now.replace(microsecond=0).isoformat()
        end = (now + timedelta(hours=3)).replace(microsecond=0).isoformat()
        variants = [
            f'/channels?account_id={s.account.id}',
            f'/channels?dup_block={s.guide_channel.id}&dup_names=Some%20Channel',
            '/channels?page=1',
            '/api/channels/search?grain=airings&q=test&facets=',
            '/api/channels/search?grain=airings&q=',
            f'/api/guide/epg?start={start}&end={end}',
            f'/api/guide/epg?start={start}&end={end}&channel_id={s.guide_channel.id}',
            f'/api/channel-groups/suggest?group_id={s.group.id}',
            f'/channel-groups/{s.group.id}?timeline_page=1',
        ]
        failures = []
        for url in variants:
            resp = self.t.client.get(url)
            if resp.status_code not in _ALLOWED:
                failures.append(f'{resp.status_code} {url}')
        self.assertEqual(failures, [], 'query-param variants returned a disallowed status:\n' + '\n'.join(failures))


if __name__ == '__main__':
    unittest.main(verbosity=2)
