"""A digit int() refuses never reaches int().

Guards dev/docs/BUGS.md 2026-11-16 (isdigit), after the first case on 2026-11-15:
str.isdigit() is true of a superscript two and int() raises on it, so a request value
tested with one and converted with the other ended in a 500. str.isdecimal() is true only
of what int() reads.

  python3 -m unittest tests.test_isdecimal_guard
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app.database import Recording  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPERSCRIPT_TWO = '²'


class WholeTreeTests(unittest.TestCase):
    def test_the_premise(self):
        self.assertTrue(SUPERSCRIPT_TWO.isdigit())
        self.assertFalse(SUPERSCRIPT_TWO.isdecimal())
        with self.assertRaises(ValueError):
            int(SUPERSCRIPT_TWO)
        # Every character isdecimal() accepts is one int() reads, other scripts included.
        self.assertEqual(3, int('٣'))
        self.assertTrue('٣'.isdecimal())

    def test_app_and_the_simulator_never_test_a_number_with_isdigit(self):
        pattern = re.compile(r'\.isdigit\(')
        bad = []
        for tree in ('app', 'sim'):
            for dirpath, _dirs, files in os.walk(os.path.join(ROOT, tree)):
                for name in files:
                    if not name.endswith('.py'):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding='utf-8') as fh:
                        for number, line in enumerate(fh, 1):
                            if pattern.search(line):
                                bad.append(f'{os.path.relpath(path, ROOT)}:{number}')
        self.assertEqual([], bad, 'use str.isdecimal(): isdigit() is also true of digits '
                                  'int() raises on')


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)

    def test_a_superscript_id_on_a_new_recording_is_ignored_not_a_500(self):
        start = datetime.utcnow() + timedelta(hours=2)
        stop = start + timedelta(hours=1)
        with mock.patch('app.routes.recordings.schedule_recording'):
            resp = self.t.client.post('/recordings/new-json', data={
                'name': 'superscript', 'url': 'http://example.test/live/1',
                'channel_id': SUPERSCRIPT_TWO, 'group_id': SUPERSCRIPT_TWO,
                'profile_id': SUPERSCRIPT_TWO, 'replace_recording_id': SUPERSCRIPT_TWO,
                'start_time': start.strftime('%Y-%m-%dT%H:%M'),
                'start_time_utc': start.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
                'stop_time_utc': stop.strftime('%Y-%m-%dT%H:%M:%S.000Z')})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True)[:300])
        rec = Recording.query.filter_by(name='superscript').one()
        self.assertEqual((None, None, None), (rec.channel_id, rec.group_id, rec.profile_id))


if __name__ == '__main__':
    unittest.main()
