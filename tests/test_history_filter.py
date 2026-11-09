"""The shared history-filter chips (app/history_filter.py, dev/changelog/1474): one chip per
kind of entry with its count, All always first, a zero-count chip hidden, the active chip
keeping only its entries - on the recording's Event log, the channel's and the group's
Activity timelines, and the rule page that had the first row.

The bucket table for the recording Event log is held complete here: every event type the
recording page already classifies (its `ev_cls` map) and every type the app writes has a
chip, so a new event type cannot land in All only by being forgotten.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_history_filter
"""
import glob
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, history_filter as hf  # noqa: E402
from app.database import (CHANNEL_GROUPED, CONVERSION_DONE, DIAGNOSTICS, EVENT_LABELS,  # noqa: E402
                          GROUP_GUIDE_ADDED, STALL_DETECTED, ChannelEvent, ChannelGroupEvent,
                          RecordingEvent)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHIP = re.compile(r'<a class="chip([^"]*)" data-hist="(\w+)" href="([^"]+)">(.*?)</a>', re.S)


def chips(html, card):
    """[(key, text, active, href)] for the chips inside the card whose id is `card`."""
    part = re.split(r'<div class="card[" ]', html.split(f'id="{card}"', 1)[1], 1)[0]
    return [(k, re.sub(r'<[^>]+>', ' ', t).split(), 'active' in cls, h)
            for cls, k, h, t in CHIP.findall(part)]


class ApplyTests(unittest.TestCase):
    FILTERS = (('all', 'All'), ('a', 'Apples'), ('b', 'Bananas'), ('c', 'Cherries'))

    def test_an_unknown_key_is_all(self):
        self.assertEqual(hf.pick('nonsense', self.FILTERS), 'all')
        self.assertEqual(hf.pick(None, self.FILTERS), 'all')
        self.assertEqual(hf.pick('b', self.FILTERS), 'b')

    def test_counts_hide_empty_chips_and_keep_the_active_bucket(self):
        entries = ['a1', 'a2', 'b1', 'x']   # 'x' is in no bucket: All shows it, no chip does
        out, kept = hf.apply(entries, lambda e: e[0] if e[0] in 'abc' else None, self.FILTERS,
                             'b', lambda k: f'/p?hist={k}')
        self.assertEqual([(c['key'], c['count'], c['active']) for c in out],
                         [('all', 4, False), ('a', 2, False), ('b', 1, True)])
        self.assertEqual(out[1]['href'], '/p?hist=a')
        self.assertEqual(kept, ['b1'])

    def test_all_keeps_everything_including_unbucketed_entries(self):
        out, kept = hf.apply(['a1', 'x'], lambda e: None, self.FILTERS, 'all', lambda k: k)
        self.assertEqual(kept, ['a1', 'x'])
        self.assertEqual([c['key'] for c in out], ['all'])
        self.assertTrue(out[0]['active'])


class BucketTableTests(unittest.TestCase):
    def test_every_recording_event_the_page_classifies_has_a_chip(self):
        src = open(os.path.join(REPO, 'templates', 'recording_detail.html')).read()
        block = re.search(r"\{%\s*set\s+ev_cls\s*=\s*\{(.*?)\}\s*%\}", src, re.S).group(1)
        classified = set(re.findall(r"'([A-Z_]+)':", block))
        self.assertGreater(len(classified), 50)
        self.assertEqual(sorted(classified - set(hf.RECORDING_BUCKETS)), [])

    def test_every_recording_event_the_app_writes_has_a_chip(self):
        written = set()
        for path in glob.glob(os.path.join(REPO, 'app', '*.py')):
            text = open(path).read()
            written |= set(re.findall(r"add_recording_event\(\s*[^,]+,\s*([A-Z_]{4,})", text))
            written |= set(re.findall(r"RecordingEvent\([^)]*?event_type\s*=\s*([A-Z_]{4,})",
                                      text, re.S))
        self.assertGreater(len(written), 30)
        self.assertEqual(sorted(written - set(hf.RECORDING_BUCKETS)), [])

    def test_the_tables_name_real_event_types_and_real_chips(self):
        self.assertEqual(sorted(set(hf.RECORDING_BUCKETS) - set(EVENT_LABELS)), [])
        keys = {k for k, _ in hf.RECORDING_FILTERS}
        self.assertEqual(sorted(set(hf.RECORDING_BUCKETS.values()) - keys), [])
        tl_keys = {k for k, _ in hf.TIMELINE_FILTERS}
        self.assertEqual(sorted(set(hf.TIMELINE_KIND_BUCKETS.values()) - tl_keys), [])


class RecordingPageTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='COMPLETED')
        for et in (STALL_DETECTED, STALL_DETECTED, CONVERSION_DONE, DIAGNOSTICS):
            db.session.add(RecordingEvent(recording_id=self.rec.id, event_type=et,
                                          detail=f'{et} happened'))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def get(self, qs=''):
        resp = self.t.client.get(f'/recordings/{self.rec.id}{qs}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_chips_count_each_kind_and_all_is_active(self):
        html = self.get()
        got = [(k, ' '.join(t), a) for k, t, a, _h in chips(html, 'panel-eventlog')]
        self.assertEqual(got, [('all', 'All 4', True), ('stream', 'Stream trouble 2', False),
                               ('file', 'File 1', False), ('tech', 'Technical detail 1', False)])
        self.assertEqual(len(re.findall(r'<li class="ev ', html)), 4)

    def test_a_chip_narrows_the_log_and_the_heading_still_counts_everything(self):
        html = self.get('?hist=stream')
        self.assertEqual(len(re.findall(r'<li class="ev ', html)), 2)
        self.assertIn('Event log <span class="cnt">4</span>', html)
        self.assertIn(('stream', ['Stream', 'trouble', '2'], True, f'/recordings/{self.rec.id}?hist=stream'),
                      chips(html, 'panel-eventlog'))

    def test_an_unknown_chip_is_all(self):
        html = self.get('?hist=nonsense')
        self.assertEqual(len(re.findall(r'<li class="ev ', html)), 4)


class ChannelPageTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account()
        self.ch = seed.make_channel(acc, name='Filtered Channel')
        seed.make_channel_test(self.ch, status='COMPLETED')
        seed.make_channel_test(self.ch, status='COMPLETED')
        seed.make_recording(status='COMPLETED', channel_id=self.ch.id)
        db.session.add(ChannelEvent(channel_id=self.ch.id, event_type=CHANNEL_GROUPED,
                                    detail='grouped'))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def get(self, qs=''):
        resp = self.t.client.get(f'/channels/{self.ch.id}{qs}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_chips_count_tests_recordings_and_changes(self):
        got = [(k, ' '.join(t), a) for k, t, a, _h in chips(self.get(), 'cd-timeline')]
        self.assertEqual(got, [('all', 'All 4', True), ('tests', 'Health tests 2', False),
                               ('recordings', 'Recordings 1', False), ('changes', 'Changes 1', False)])

    def test_a_chip_narrows_the_timeline_and_the_page_size_carries_it(self):
        html = self.get('?hist=tests&timeline_per_page=10')
        self.assertEqual(html.count('ch-timeline-row'), 2)
        hrefs = {k: h for k, _t, _a, h in chips(html, 'cd-timeline')}
        self.assertEqual(hrefs['recordings'],
                         f'/channels/{self.ch.id}?timeline_per_page=10&amp;hist=recordings')
        self.assertIn("'&amp;hist=tests'", html)   # the per-page select keeps the filter


class GroupPageTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account()
        self.ch = seed.make_channel(acc, name='Member')
        self.group = seed.make_group(name='Filtered Group', members=[self.ch])
        seed.make_recording(status='COMPLETED', group_id=self.group.id, channel_id=self.ch.id)
        db.session.add(ChannelGroupEvent(group_id=self.group.id, event_type=GROUP_GUIDE_ADDED,
                                         detail='added', timestamp=datetime.utcnow() - timedelta(minutes=5)))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_the_chips_count_and_a_chip_narrows(self):
        html = self.t.client.get(f'/channel-groups/{self.group.id}').get_data(as_text=True)
        got = [(k, ' '.join(t), a) for k, t, a, _h in chips(html, 'gd-activity')]
        # The group's own creation is a group event too, so Changes carries at least one.
        self.assertEqual(got[0], ('all', f'All {html.count("ch-timeline-row")}', True))
        self.assertEqual(got[1][:2], ('recordings', 'Recordings 1'))
        self.assertEqual(got[2][0], 'changes')
        narrowed = self.t.client.get(f'/channel-groups/{self.group.id}?hist=recordings').get_data(as_text=True)
        self.assertEqual(narrowed.count('ch-timeline-row'), 1)
        self.assertIn('<span>Recording</span>', narrowed.split('id="gd-activity"', 1)[1])


if __name__ == '__main__':
    unittest.main()
