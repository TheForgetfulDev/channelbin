"""A recording and an IPTV output play of the same feed judge it with the same live checks
(CLAUDE.md, "Recordings and plays judge a stream the same way"; dev/changelog/1799).

LIVE_CHECKS is the list of what can be wrong with a feed while it is being read, the way the
recording's watchdog ends a segment for it, and the way the relay leaves a channel for it. The
tests read both files and fail when either reader learns a way to give up on a feed that has no
row here - so a check added to one side is either added to the other in the same change, or
given a row saying in plain words why the play has none. dev/docs/DESIGN-output-feed.md keeps the
same table for a person to read.

The guard is about the live checks only. What a recording can judge after the fact, from the
finished file (the join's fast-delivery findings, the format probe), has no live counterpart by
nature and is not listed.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.database as database  # noqa: E402
import app.output_feed as feed  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (what is wrong with the feed, the recording's segment exit_reason or excluded_reason,
#  the play's REASON_* name, or None with why the play has no counterpart yet)
LIVE_CHECKS = (
    ('nothing arrives for watchdog.stall_timeout_seconds',
     'STALL_KILLED', 'REASON_NO_DATA', None),
    ('ffmpeg exits on its own',
     'PROCESS_EXITED', 'REASON_FFMPEG_EXITED', None),
    ('a new connection sends nothing in time',
     'RESTART_NO_DATA', 'REASON_CONNECT_TIMEOUT', None),
    ('too little of the program arrives to keep up (a trickle)',
     database.SEGMENT_EXIT_SLOW_DELIVERY, 'REASON_TOO_SLOW', None),
    ('the program arrives faster than real time (a frozen or replaying feed)',
     'FAST_DELIVERY_KILLED', None,
     'not built for the IPTV output yet - tracked in the backlog'),
    ('the video stops while the sound keeps flowing',
     database.SEGMENT_EXIT_VIDEO_STOPPED, None,
     'not built for the IPTV output yet - tracked in the backlog'),
    ('the provider sends its "channel offline" clip in place of the channel',
     database.SEGMENT_EXCLUDED_PLACEHOLDER, None,
     'not built for the IPTV output yet - tracked in the backlog'),
)

# Segment ends the watchdog writes that are not a verdict on the feed: a person moved the
# recording, or a failover abandoned the segment it was leaving (its verdict is written by the
# check that moved it).
NOT_A_FEED_VERDICT = {
    'ERROR': 'a failover abandoned the segment it was leaving',
    'CHANNEL_SWITCHED': 'a person switched the recording to another channel',
    'ACCOUNT_BLOCKED': 'a person blocked the account the segment was on',
}

# Both spellings: `exit_reason = X` and `exit_reason=X` as a keyword argument.
_EXIT_ASSIGN = re.compile(r"\b(?:exit_reason|excluded_reason)\s*=\s*('?)([A-Z][A-Z_]+)\1")
_MEMBER_DIED = re.compile(r'_member_died\(session, (REASON_[A-Z_]+)')


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class StreamCheckParityTests(unittest.TestCase):

    def test_every_way_the_watchdog_gives_up_on_a_feed_has_a_row(self):
        found = set()
        for quoted, name in _EXIT_ASSIGN.findall(_read('app/watchdog.py')):
            found.add(name if quoted else getattr(database, name))
        found -= set(NOT_A_FEED_VERDICT)
        listed = {row[1] for row in LIVE_CHECKS}
        self.assertEqual(found - listed, set(),
                         'a live check on recordings with no row in LIVE_CHECKS: add its IPTV '
                         'output counterpart in the same change, or a row saying why there is none')
        self.assertEqual(listed - found, set(), 'a row names a segment end the watchdog never writes')

    def test_every_way_a_play_leaves_a_channel_has_a_row(self):
        found = set(_MEMBER_DIED.findall(_read('app/output_feed.py')))
        listed = {row[2] for row in LIVE_CHECKS if row[2] is not None}
        self.assertEqual(found - listed, set(),
                         'a play leaves a channel for a reason with no row in LIVE_CHECKS: add the '
                         'recording\'s counterpart in the same change')
        self.assertEqual(listed - found, set(), 'a row names a reason the relay never leaves by')
        for name in listed:
            self.assertIn(getattr(feed, name), feed.REASON_TEXT, name)

    def test_a_row_without_a_play_reason_says_why(self):
        for what, _rec, play, why in LIVE_CHECKS:
            self.assertTrue(play or why, f'"{what}" needs its IPTV output reason or why it has none')
            self.assertFalse(play and why, f'"{what}" has a play reason, so it needs no excuse')


if __name__ == '__main__':
    unittest.main()
