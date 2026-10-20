"""One pair of disk-fullness thresholds for every surface that colors it
(dev/docs/BUGS.md 2026-10-20, dev/changelog/1244).

The dashboard's Disk free tile and the Maintenance page's meter judged a disk at 75/90
while the readiness row judged it at 85/95, so a disk 92% full was drawn red by the meter
and amber by the readiness row beside it on the same page, and the free space was stated
two ways ("412.3 GB free" against "0.4 TB free"). routes/system.py now owns the two
numbers beside its one statvfs, the storage-details payload carries the judgment, and
readiness prints the free space through fmt_bytes like everyone else.

  python3 -m unittest tests.test_disk_level
"""
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from app import readiness
from app.routes.system import DISK_BAD_PCT, DISK_WARN_PCT, disk_level, disk_used_pct
from tests.support.app import make_test_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class LevelTests(unittest.TestCase):
    def test_the_pair_and_the_three_levels(self):
        self.assertEqual((DISK_WARN_PCT, DISK_BAD_PCT), (75, 90))
        self.assertEqual(disk_level(10), 'ok')
        self.assertEqual(disk_level(74), 'ok')
        self.assertEqual(disk_level(75), 'warn')
        self.assertEqual(disk_level(89), 'warn')
        self.assertEqual(disk_level(90), 'bad')
        self.assertIsNone(disk_level(None))

    def test_percent_used_is_whole_and_none_for_no_answer(self):
        self.assertEqual(disk_used_pct(1000, 80), 92)
        self.assertIsNone(disk_used_pct(None, None))
        self.assertIsNone(disk_used_pct(0, 0))


class ReadinessAgreesTests(unittest.TestCase):
    def _storage(self, total, free):
        ctx = SimpleNamespace(dvr_dir='/dvr', disk_total=total, disk_free=free)
        return readiness.CHECKS_BY_ID['storage_space'].run(ctx)

    def test_a_92_percent_disk_is_a_problem_on_the_readiness_row_too(self):
        """The meter drew this red while the readiness row said ATTENTION."""
        self.assertEqual(self._storage(1000, 80).status, readiness.PROBLEM)

    def test_a_80_percent_disk_needs_attention_on_both(self):
        self.assertEqual(self._storage(1000, 200).status, readiness.ATTENTION)
        self.assertEqual(self._storage(1000, 300).status, readiness.READY)

    def test_the_free_space_is_the_canonical_byte_string(self):
        found = self._storage(4 * 2 ** 40, 412 * 2 ** 30).found
        self.assertIn('412.0 GB free', found)
        self.assertNotIn('TB free', found)
        self.assertNotIn('def _tb(', _read('app/readiness.py'))

    def test_no_surface_carries_its_own_numbers(self):
        for rel in ('app/readiness.py', 'app/routes/dashboard.py', 'static/js/maintenance.js'):
            src = _read(rel)
            self.assertIsNone(re.search(r'>=?\s*(75|85|90|95)\b', src), f'{rel} judges the disk itself')


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_storage_details_carries_the_judgment_for_the_meter(self):
        with mock.patch('app.routes.system._disk_bytes', return_value=(1000, 80)):
            payload = self.t.client.get('/api/system/storage-details').get_json()
        self.assertEqual(payload['disk_used_pct'], 92)
        self.assertEqual(payload['disk_level'], 'bad')

    def test_an_unmounted_disk_sends_no_level(self):
        with mock.patch('app.routes.system._disk_bytes', return_value=(None, None)):
            payload = self.t.client.get('/api/system/storage-details').get_json()
        self.assertIsNone(payload['disk_used_pct'])
        self.assertIsNone(payload['disk_level'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
