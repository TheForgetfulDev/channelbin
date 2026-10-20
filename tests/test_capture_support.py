"""The capture tier's footing (tests/capture/support.py), checked without a capture.

Guards dev/docs/BUGS.md 2026-10-19 (a loss at either end of a recording was invisible to six
of the eight capture scenarios): `CaptureCase.answer()` and `answer_joined()` hand the answer
key the recording's scheduled window unless the scenario passes one, so content lost before
the first connection or after the last segment stopped is counted. Without a window the
answer key's extent runs from the first frame held to the last (dev/changelog/1235).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import answer_key as ak  # noqa: E402
from tests.capture import support  # noqa: E402

WINDOW = ('2026-10-19T20:00:00', '2026-10-19T20:01:00')


class _Keeper:
    keep = '/kept'

    def __init__(self, kept):
        self.kept = kept

    def kept_copy(self, path):
        return self.kept.get(os.path.basename(path))


class _Rec:
    output_path = '/dvr/show.ts'


def _case(kept):
    """A CaptureCase with everything answer() reads stubbed, and no setUp run."""
    case = support.CaptureCase.__new__(support.CaptureCase)
    case.sim = object()
    case.keeper = _Keeper(kept)
    case.segments = lambda rid: []
    case.serving = lambda rid: ['cove:1']
    case.window = lambda rid: WINDOW
    case.fresh = lambda model, ident: _Rec()
    return case


class AnswerWindowTests(unittest.TestCase):
    def test_answer_judges_over_the_scheduled_window_by_default(self):
        case = _case({})
        with mock.patch.object(ak, 'check', return_value='answer') as check:
            self.assertEqual(case.answer(7), 'answer')
        self.assertEqual(check.call_args.kwargs['window'], WINDOW)
        self.assertEqual(check.call_args.kwargs['serving'], ['cove:1'])
        self.assertEqual(check.call_args.kwargs['min_gap'], support.MIN_GAP)

    def test_answer_joined_judges_over_the_scheduled_window_by_default(self):
        case = _case({'show.ts': '/kept/show.ts'})
        with mock.patch.object(ak, 'check', return_value='answer') as check:
            self.assertEqual(case.answer_joined(7), 'answer')
        self.assertEqual(check.call_args.args[1], ['/kept/show.ts'])
        self.assertEqual(check.call_args.kwargs['window'], WINDOW)

    def test_a_window_the_scenario_passes_is_kept(self):
        case = _case({})
        with mock.patch.object(ak, 'check', return_value='answer') as check:
            case.answer(7, window=None)
        self.assertIsNone(check.call_args.kwargs['window'])


if __name__ == '__main__':
    unittest.main()
