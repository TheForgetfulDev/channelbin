"""The small Python formatters that had grown a second spelling, now one each in
app/fmt_utils.py (dev/changelog/1244): the "a, b and c" joiner, the plural word and its
count-carrying form, and the ISO-or-None datetime.

Each had two homes written days apart - routes/providers._names beside
connection_limits._join_labels, routes/channels._plural beside readiness._plural, and
routes/ha._iso beside fourteen inline `x.isoformat() if x else None` spellings. These hold
the one home's behavior and that the old homes are gone.

  python3 -m unittest tests.test_fmt_utils_shared
"""
import os
import re
import unittest
from datetime import datetime

from app.fmt_utils import counted, iso_or_none, join_labels, plural

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class JoinLabelsTests(unittest.TestCase):
    def test_zero_one_two_and_three(self):
        self.assertEqual(join_labels([]), '')
        self.assertEqual(join_labels(['a']), 'a')
        self.assertEqual(join_labels(['a', 'b']), 'a and b')
        self.assertEqual(join_labels(['a', 'b', 'c']), 'a, b and c')

    def test_repeats_are_dropped_and_order_kept(self):
        """connection_limits dedupes two holders of one kind into one label; the providers
        route never repeated, so the dedupe is harmless there."""
        self.assertEqual(join_labels(['a recording', 'a recording', 'a preview']),
                         'a recording and a preview')

    def test_the_two_old_spellings_are_gone(self):
        self.assertNotIn('def _join_labels', _read('app/connection_limits.py'))
        providers = _read('app/routes/providers.py')
        self.assertNotIn("' and ' + listed[-1]", providers)
        self.assertIn('join_labels(', providers)


class PluralTests(unittest.TestCase):
    def test_plural_word(self):
        self.assertEqual(plural(1, 'folder'), 'folder')
        self.assertEqual(plural(2, 'folder'), 'folders')
        self.assertEqual(plural(0, 'folder'), 'folders')
        self.assertEqual(plural(1, 'is', 'are'), 'is')
        self.assertEqual(plural(3, 'is', 'are'), 'are')

    def test_counted_carries_the_grouped_number(self):
        self.assertEqual(counted(1, 'program'), '1 program')
        self.assertEqual(counted(1234, 'program'), '1,234 programs')
        self.assertEqual(counted(2, 'has', 'have'), '2 have')

    def test_no_module_defines_its_own_plural_any_more(self):
        for rel in ('app/readiness.py', 'app/routes/channels.py'):
            self.assertNotIn('def _plural(', _read(rel), rel)


class IsoOrNoneTests(unittest.TestCase):
    def test_datetime_and_none(self):
        self.assertEqual(iso_or_none(datetime(2026, 10, 20, 9, 5, 1)), '2026-10-20T09:05:01')
        self.assertIsNone(iso_or_none(None))

    def test_the_inline_spelling_is_gone_from_the_payload_builders(self):
        """The two documented variants (channel_search_rows drops microseconds,
        health_score accepts a non-datetime) stay and are marked at the definition;
        everything else uses the one helper."""
        pattern = re.compile(r'(\w[\w.]*)\.isoformat\(\) if \1 else None')
        bad = []
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, 'app')):
            for name in files:
                if not name.endswith('.py') or name == 'fmt_utils.py':
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                for m in pattern.finditer(_read(rel)):
                    bad.append(f'{rel}: {m.group(0)}')
        self.assertEqual(bad, [], 'use fmt_utils.iso_or_none()')
        self.assertIn('duplicated from app/fmt_utils.py::iso_or_none',
                      _read('app/channel_search_rows.py'))
        self.assertIn('duplicated from app/fmt_utils.py::iso_or_none',
                      _read('app/health_score.py'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
