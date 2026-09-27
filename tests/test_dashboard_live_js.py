"""Tier 0 - the Dashboard's live badge, driven in a real DOM by the frames app/ publishes.

Guards dev/docs/BUGS.md 2026-09-18 "The Dashboard's live badge ignored most of the frames
that change a recording's status". Invariants:

  (a) Any SSE frame whose `status` is a known Recording status, and differs from what the
      row shows, relabels the badge with that status's shared label and asks for exactly one
      re-render of the recording regions, never a page reload - whatever the event is called
      (capture end, cancel, dead-stream failure). dev/changelog/1143.
  (b) A conversion that parks badges WAITING from its yield frame, and drops back to its
      phase label on the resume frame.
  (c) A frame that repeats the row's current status (every CONVERSION_PROGRESS tick) neither
      relabels nor re-renders, and does not clear WAITING. Nor does the same change arriving
      again after a swap that still showed the old status.
  (d) A frame without a status never relabels the badge - in particular never with the
      event's own name.
  (e) Every publish in app/ carries `status`, except the named set of events that never move
      a recording's status (the static half, so a new publish must decide).

tests/support/dashboard_live.mjs replays each scenario's frames against the markup the
dashboard route really rendered; every assertion lives here.

  python3 -m unittest tests.test_dashboard_live_js
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    CAPTURE_COMPLETE, CONVERSION_RESUMED, CONVERSION_YIELDED, RECORDING_ABORTED,
    RECORDING_FAILED, RECORDING_FAILED_DEAD_STREAM, RESTART_ATTEMPTED,
    REC_STATUS_ABORTED, REC_STATUS_CONCATENATING, REC_STATUS_CONVERTING, REC_STATUS_FAILED,
    REC_STATUS_IN_PROGRESS,
)
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'dashboard_live.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None
_IDS = {}


def _frame(rid, event, data):
    return {'recording_id': rid, 'event': event, 'data': data}


def _scenarios(ids):
    live, conv, parked = ids['live'], ids['conv'], ids['parked']
    progress = {'status': REC_STATUS_CONVERTING, 'pct': 12.5, 'out_size': 1024,
                'eta_seconds': 60}
    return {
        'capture_complete': {'watch': [live], 'frames': [
            _frame(live, CAPTURE_COMPLETE, {'status': REC_STATUS_CONCATENATING})]},
        'aborted': {'watch': [live], 'frames': [
            _frame(live, RECORDING_ABORTED, {'status': REC_STATUS_ABORTED})]},
        'dead_stream': {'watch': [live], 'frames': [
            _frame(live, RECORDING_FAILED_DEAD_STREAM,
                   {'status': REC_STATUS_FAILED, 'retry_attempts': 3})]},
        'yield': {'watch': [conv], 'frames': [
            _frame(conv, CONVERSION_YIELDED, {'status': REC_STATUS_CONVERTING, 'waiting': True})]},
        'resume': {'watch': [parked], 'frames': [
            _frame(parked, CONVERSION_RESUMED,
                   {'status': REC_STATUS_CONVERTING, 'waiting': False})]},
        'progress_repeats': {'watch': [conv, parked], 'frames': [
            _frame(rid, 'CONVERSION_PROGRESS', progress)
            for _ in range(5) for rid in (conv, parked)]},
        'no_status': {'watch': [live], 'frames': [
            _frame(live, RESTART_ATTEMPTED, {'attempt': 1}),
            _frame(live, RECORDING_FAILED, {'consecutive_failures': 5, 'max_failures': 5})]},
        # The server answers the re-render with the page as it was, i.e. the frame beat its
        # own commit - so the swap puts the old status back under every repeat that follows.
        'stale_swap': {'watch': [live], 'servePage': True, 'gapMs': 20, 'frames': [
            _frame(live, CAPTURE_COMPLETE, {'status': REC_STATUS_CONCATENATING})
            for _ in range(4)]},
        'stats_repeat': {'watch': [live], 'frames': [
            _frame(live, 'STATS_SNAPSHOT', {'status': REC_STATUS_IN_PROGRESS,
                                            'elapsed_seconds': 10, 'remaining_seconds': 50,
                                            'total_bytes': 2048})
            for _ in range(3)]},
    }


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='dashboard_live_js_')
    try:
        with t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            _IDS['live'] = make_recording(status=REC_STATUS_IN_PROGRESS, name='Live',
                                          channel_id=ch.id).id
            _IDS['conv'] = make_recording(status=REC_STATUS_CONVERTING, name='Converting',
                                          channel_id=ch.id).id
            _IDS['parked'] = make_recording(status=REC_STATUS_CONVERTING, name='Parked',
                                            channel_id=ch.id,
                                            postprocess_waiting_since=datetime.utcnow()).id
            db.session.commit()
        html = t.app.test_client().get('/').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(html)
        with open(os.path.join(tmp, 'scenarios.json'), 'w', encoding='utf-8') as f:
            json.dump(_scenarios(_IDS), f)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries the shared cases only')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def row(self, key):
        return self.obs['rows'][str(_IDS[key])]


class RenderedStateTests(unittest.TestCase):
    """The server-rendered starting point the scenarios below move away from."""

    @unittest.skipIf(shutil.which('node') is None, 'node not installed')
    @unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
    def test_the_parked_row_renders_waiting_and_the_others_their_phase(self):
        obs = _observe()['progress_repeats']['rows']
        self.assertEqual(obs[str(_IDS['parked'])]['text'], 'WAITING')
        self.assertEqual(obs[str(_IDS['conv'])]['text'], 'CONVERTING')


class CaptureCompleteTests(_Base):
    SCENARIO = 'capture_complete'

    def test_the_row_relabels_to_joining(self):
        self.assertEqual(self.row('live')['text'], 'JOINING')
        self.assertEqual(self.row('live')['cls'], 'badge badge-concatenating')
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)


class AbortedTests(_Base):
    SCENARIO = 'aborted'

    def test_the_row_relabels_to_cancelled(self):
        self.assertEqual(self.row('live')['text'], 'CANCELLED')
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)


class DeadStreamTests(_Base):
    SCENARIO = 'dead_stream'

    def test_the_row_relabels_to_failed(self):
        self.assertEqual(self.row('live')['text'], 'FAILED')
        self.assertEqual(self.row('live')['cls'], 'badge badge-failed')
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)


class YieldTests(_Base):
    SCENARIO = 'yield'

    def test_a_parked_conversion_badges_waiting(self):
        self.assertEqual(self.row('conv')['text'], 'WAITING')
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)


class ResumeTests(_Base):
    SCENARIO = 'resume'

    def test_a_resumed_conversion_badges_converting(self):
        self.assertEqual(self.row('parked')['text'], 'CONVERTING')
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)


class ProgressRepeatTests(_Base):
    SCENARIO = 'progress_repeats'

    def test_repeated_progress_neither_relabels_nor_rerenders(self):
        self.assertEqual(self.obs['refreshes'], 0)
        self.assertEqual(self.row('conv')['text'], 'CONVERTING')

    def test_progress_does_not_clear_waiting(self):
        self.assertEqual(self.row('parked')['text'], 'WAITING')


class StaleSwapTests(_Base):
    SCENARIO = 'stale_swap'

    def test_one_change_asks_for_one_rerender_even_when_the_server_lags(self):
        """A frame can arrive before its commit is readable, so the swapped-in row still
        shows the old status and every repeated frame differs from it again. Asking once
        per change keeps that from re-rendering the page once a second; the nav poll's
        recording signature is what catches the server up (dev/changelog/1143)."""
        self.assertEqual(self.obs['refreshes'], 1)
        self.assertEqual(self.obs['reloads'], 0)

    def test_the_badge_still_says_what_sse_said(self):
        self.assertEqual(self.row('live')['text'], 'JOINING')


class StatsRepeatTests(_Base):
    SCENARIO = 'stats_repeat'

    def test_a_snapshot_repeating_the_status_does_nothing(self):
        self.assertEqual(self.obs['refreshes'], 0)
        self.assertEqual(self.row('live')['text'], 'RECORDING')


class NoStatusTests(_Base):
    SCENARIO = 'no_status'

    def test_a_frame_without_a_status_never_relabels(self):
        self.assertEqual(self.row('live')['text'], 'RECORDING')
        self.assertEqual(self.row('live')['status'], REC_STATUS_IN_PROGRESS)
        self.assertEqual(self.obs['refreshes'], 0)


# Events that never move a Recording.status, so their frames carry no `status`: cell
# updates and in-run incidents. Anything published outside this set must say which status
# the row is now in, or the Dashboard cannot follow it.
_NO_STATUS_EVENTS = {
    'FAST_DELIVERY_DETECTED', 'STALL_DETECTED', 'RESTART_ATTEMPTED', 'RESTART_SUCCEEDED',
    'RESTART_FAILED', 'CAPTURE_PACING_ENABLED', 'SEGMENT_STARTED', 'RECORDING_HANDOFF',
    'GROUP_FAILOVER',
}


def _publish_calls():
    app_dir = os.path.join(REPO, 'app')
    for root, _dirs, files in os.walk(app_dir):
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'publish'
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'ev'):
                    yield os.path.relpath(path, REPO), node


def _event_name(node):
    arg = node.args[1]
    if isinstance(arg, ast.Constant):
        return arg.value
    if isinstance(arg, ast.Name):
        return arg.id
    return None


class EveryStatusMovingPublishCarriesStatusTests(unittest.TestCase):
    def test_the_scan_finds_the_publish_sites(self):
        self.assertGreaterEqual(len(list(_publish_calls())), 30)

    def test_every_publish_outside_the_no_status_set_carries_status(self):
        missing = []
        for path, node in _publish_calls():
            event = _event_name(node)
            if event in _NO_STATUS_EVENTS:
                continue
            payload = node.args[2] if len(node.args) > 2 else None
            keys = ([k.value for k in payload.keys if isinstance(k, ast.Constant)]
                    if isinstance(payload, ast.Dict) else [])
            if 'status' not in keys:
                missing.append(f'{path}:{node.lineno} {event}')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
