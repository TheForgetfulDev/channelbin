"""Tier 2 - a recording does not change format mid-run.

Guards what `dev/changelog/754` built for `dev/docs/DESIGN-channel-groups-model.md` §5.1
(DECIDED 12). Segments are concatenated into one file, so a failover onto a member of a
different resolution or frame rate produces a single output whose format changes partway
through. Before this, nothing stopped it and nothing said it had happened.

**The measurement is what shaped these assertions.** Run on this machine against the real
`app/concatenator.py` command: the `.ts` concat, the mp4 stream-copy and the mp4 re-encode
all exit 0 on a 720p30 + 1080p60 pair, and this app's own damage detector reports zero
gaps - so a mixed-format recording is not flagged damaged and nothing anywhere errors. The
entire cost is silence: the container advertises only the first segment's format. That is
why the pin **filters and never refuses**, and why the disclosure is the load-bearing half.

The rules with teeth, each a separate way to get this wrong:

  * The pin is the earliest **probed** segment, not literally segment 1 - an unprobed
    segment contributes no data to the concatenated file either.
  * **Unknown is never "different"**: no probe means no pin, and an untested candidate
    survives the filter. The opposite call makes a never-tested member unselectable.
  * **Zero pin-matching candidates is an override, never a skip** - principle 2 spent on a
    format rule buys an incomplete recording to avoid damage ffmpeg absorbs.
  * The pin composes with the group's format lock rather than replacing it, and holds
    **even when the group manages no format at all**.
  * The change is announced off the segment's own **probe**, not off the failover
    decision - the same member's feed can drift with no failover at all.
  * Every divergent segment logs its own event; only the first also alerts.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test, make_recording)
from app import db  # noqa: E402
from app.channel_groups import segment_format_key, format_key_from  # noqa: E402
from app.recorder import recording_format_pin, _pin_eligible_members  # noqa: E402
from app.database import (RecordingSegment, RecordingEvent, Alert,  # noqa: E402
                          RECORDING_FORMAT_CHANGED, GROUP_FORMAT_MANUAL,
                          GROUP_FORMAT_UNMANAGED)

HD = ('1920x1080', 60)
SD = ('1280x720', 30)


class _PinCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _rec(self):
        rec = make_recording(status='IN_PROGRESS')
        db.session.commit()
        return rec

    def _segment(self, rec, seg_num, key):
        """A segment on `rec`. `key` None leaves it unprobed."""
        seg = RecordingSegment(
            recording_id=rec.id, segment_number=seg_num,
            file_path=f'/tmp/pin_{rec.id}_{seg_num}.ts',
            started_at=datetime.utcnow())
        if key is not None:
            seg.probe_resolution, seg.probe_fps = key[0], float(key[1])
        db.session.add(seg)
        db.session.commit()
        return seg

    def _member(self, name, key, score=80):
        ch = make_channel(self.acct, name=name)
        ch.health_score = score
        if key is not None:
            make_channel_test(ch, all_null=False, status='COMPLETED', connected=True,
                              resolution=key[0], fps=float(key[1]), bitrate_kbps=5000)
        db.session.commit()
        return ch

    def _latest(self, channels):
        from app.routes.channel_tests import _latest_tests_by_channel
        return _latest_tests_by_channel([ch.id for ch in channels])


# ── The pin itself ────────────────────────────────────────────────────────────

class FormatPinTests(_PinCase):
    def test_pin_is_the_format_of_the_first_probed_segment(self):
        rec = self._rec()
        self._segment(rec, 1, SD)
        self._segment(rec, 2, HD)
        self.assertEqual(recording_format_pin(rec.id), SD)

    def test_a_recording_with_no_segments_has_no_pin(self):
        rec = self._rec()
        self.assertIsNone(recording_format_pin(rec.id))

    def test_an_unprobed_segment_leaves_the_recording_unpinned(self):
        """No probe means unknown, and unknown must never read as a constraint."""
        rec = self._rec()
        self._segment(rec, 1, None)
        self.assertIsNone(recording_format_pin(rec.id))

    def test_pin_skips_an_unprobed_earlier_segment(self):
        """A segment that never carried enough data to probe carries none into the
        concatenated file either, so the output opens as the first segment that did."""
        rec = self._rec()
        self._segment(rec, 1, None)
        self._segment(rec, 2, HD)
        self.assertEqual(recording_format_pin(rec.id), HD)

    def test_pin_is_scoped_to_its_own_recording(self):
        rec_a, rec_b = self._rec(), self._rec()
        self._segment(rec_a, 1, SD)
        self._segment(rec_b, 1, HD)
        self.assertEqual(recording_format_pin(rec_a.id), SD)
        self.assertEqual(recording_format_pin(rec_b.id), HD)

    def test_segment_format_key_rounds_fps_like_format_key(self):
        """59.94 and 60 are the same format - one rounding rule, or two callers disagree
        about whether a failover changed anything."""
        rec = self._rec()
        seg = self._segment(rec, 1, None)
        seg.probe_resolution, seg.probe_fps = '1920x1080', 59.94
        db.session.commit()
        self.assertEqual(segment_format_key(seg), HD)
        self.assertEqual(segment_format_key(seg), format_key_from('1920x1080', 60))

    def test_segment_format_key_is_none_when_a_half_is_missing(self):
        rec = self._rec()
        seg = self._segment(rec, 1, None)
        seg.probe_resolution = '1920x1080'      # fps never read
        db.session.commit()
        self.assertIsNone(segment_format_key(seg))


# ── The filter ────────────────────────────────────────────────────────────────

class PinFilterTests(_PinCase):
    def test_pin_drops_the_candidates_that_do_not_match(self):
        a, b, c = (self._member('A', SD), self._member('B', HD), self._member('C', SD))
        members, override = _pin_eligible_members([a, b, c], SD, self._latest([a, b, c]))
        self.assertEqual([ch.name for ch in members], ['A', 'C'])
        self.assertFalse(override)

    def test_no_pin_filters_nothing(self):
        a, b = self._member('A', SD), self._member('B', HD)
        members, override = _pin_eligible_members([a, b], None, self._latest([a, b]))
        self.assertEqual([ch.name for ch in members], ['A', 'B'])
        self.assertFalse(override)

    def test_an_untested_candidate_survives_the_pin(self):
        """Unknown is not proven-different - the same call format_eligible_members makes."""
        a, b = self._member('A', SD), self._member('B', None)
        members, override = _pin_eligible_members([a, b], SD, self._latest([a, b]))
        self.assertEqual([ch.name for ch in members], ['A', 'B'])
        self.assertFalse(override)

    def test_zero_matching_candidates_is_an_override_not_an_empty_list(self):
        """Principle 2: a live capture is never abandoned over a format rule. Measured -
        the concat and both conversion branches absorb a mid-file format change."""
        a, b = self._member('A', HD), self._member('B', HD)
        members, override = _pin_eligible_members([a, b], SD, self._latest([a, b]))
        self.assertEqual([ch.name for ch in members], ['A', 'B'])
        self.assertTrue(override)

    def test_an_empty_candidate_list_is_not_an_override(self):
        """Nothing to fail over TO is the caller's abort path, not a format override."""
        members, override = _pin_eligible_members([], SD, {})
        self.assertEqual(members, [])
        self.assertFalse(override)


# ── The pin at the failover site ──────────────────────────────────────────────

class FailoverPinTests(_PinCase):
    def _group_recording(self, formats, strategy=GROUP_FORMAT_MANUAL,
                         locked=None, scores=None):
        scores = scores or [90 - 10 * i for i in range(len(formats))]
        chans = [self._member(f'Feed {i}', key, score=scores[i])
                 for i, key in enumerate(formats)]
        grp = make_group(name='FS1', members=chans, in_guide=True,
                         format_strategy=strategy)
        if locked is not None:
            grp.format_resolution, grp.format_fps = locked[0], locked[1]
        db.session.commit()
        rec = make_recording(status='IN_PROGRESS', group_id=grp.id,
                             channel_id=chans[0].id)
        db.session.commit()
        return grp, chans, rec

    def _failover(self, rec, chans):
        """Drive the real failover with its ffmpeg-adjacent work stubbed - this asserts
        on WHICH member was chosen, not on process handling."""
        from unittest.mock import patch
        from app import recorder
        state = recorder.RecordingState()
        with patch.object(recorder, 'get_state', return_value=state), \
             patch.object(recorder, '_busy_channel_ids', return_value=set()), \
             patch('app.health_score.apply_failover_health_observation'):
            ok = recorder.failover_group_member(self.t.app, rec.id, 'dead stream')
        db.session.expire_all()
        return ok, db.session.get(type(rec), rec.id)

    def test_failover_prefers_a_member_matching_the_pin(self):
        """The whole point: the higher-scored member is the wrong FORMAT, so the pin
        must send the failover to the lower-scored one that matches."""
        grp, chans, rec = self._group_recording([SD, HD, SD], scores=[90, 85, 60])
        self._segment(rec, 1, SD)
        ok, rec = self._failover(rec, chans)
        self.assertTrue(ok)
        self.assertEqual(rec.channel_id, chans[2].id)

    def test_without_a_pin_failover_takes_the_best_ranked_member(self):
        """Same group, no probed segment - the pin must not be inventing a constraint."""
        grp, chans, rec = self._group_recording([SD, HD, SD], scores=[90, 85, 60])
        self._segment(rec, 1, None)
        ok, rec = self._failover(rec, chans)
        self.assertTrue(ok)
        self.assertEqual(rec.channel_id, chans[1].id)

    def test_the_pin_holds_when_the_group_manages_no_format(self):
        """The lock is the group's standing rule; the pin is this run's. An unmanaged
        group filters nothing at the lock layer and must still be pinned."""
        grp, chans, rec = self._group_recording(
            [SD, HD, SD], strategy=GROUP_FORMAT_UNMANAGED, scores=[90, 85, 60])
        self._segment(rec, 1, SD)
        ok, rec = self._failover(rec, chans)
        self.assertTrue(ok)
        self.assertEqual(rec.channel_id, chans[2].id)

    def test_no_pin_match_fails_over_anyway_and_says_so(self):
        """Zero survivors is an override: the recording continues, and the GROUP_FAILOVER
        event carries the reason rather than leaving a silent format change."""
        grp, chans, rec = self._group_recording([SD, HD, HD], scores=[90, 85, 60])
        self._segment(rec, 1, SD)
        ok, rec = self._failover(rec, chans)
        self.assertTrue(ok)
        self.assertEqual(rec.channel_id, chans[1].id)
        ev = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type='GROUP_FAILOVER').one()
        self.assertIn('1280x720 @ 30', ev.detail)
        self.assertIn('changes format part-way through', ev.detail)
        self.assertTrue(ev.extra_data and '"pin_override": true' in ev.extra_data.lower())

    def test_a_member_that_already_failed_cannot_suppress_the_override(self):
        """dev/docs/BUGS.md 2026-08-19: both format filters judge "zero survivors" over
        the candidate list they are handed, so a dead member still counted as a survivor -
        the override never fired and the recording aborted with a working member left.
        Here the group is locked to SD, its only SD member is the one that just died, and
        the recording must continue on the HD member rather than give up."""
        grp, chans, rec = self._group_recording(
            [SD, HD], locked=SD, scores=[90, 60])
        self._segment(rec, 1, None)            # unprobed: the LOCK layer, not the pin
        ok, rec = self._failover(rec, chans)
        self.assertTrue(ok)
        self.assertEqual(rec.channel_id, chans[1].id)

    def test_a_clean_failover_says_nothing_about_format(self):
        """Server-rendered quiet case: no override note when the pin was honored."""
        grp, chans, rec = self._group_recording([SD, HD, SD], scores=[90, 85, 60])
        self._segment(rec, 1, SD)
        ok, rec = self._failover(rec, chans)
        ev = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type='GROUP_FAILOVER').one()
        self.assertNotIn('changes format part-way through', ev.detail)


# ── The disclosure ────────────────────────────────────────────────────────────

class FormatChangeDisclosureTests(_PinCase):
    def _watchdog(self, rec):
        from app.watchdog import WatchdogThread
        from app import recorder
        return WatchdogThread(rec.id, recorder.RecordingState(), self.t.app)

    def test_a_divergent_segment_logs_the_change_on_the_recording(self):
        rec = self._rec()
        self._segment(rec, 1, SD)
        seg2 = self._segment(rec, 2, HD)
        self._watchdog(rec)._check_format_pin(seg2.id, 2)
        ev = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_FORMAT_CHANGED).one()
        self.assertIn('1920x1080 @ 60', ev.detail)
        self.assertIn('1280x720 @ 30', ev.detail)

    def test_the_segment_that_set_the_pin_is_not_a_change(self):
        rec = self._rec()
        seg1 = self._segment(rec, 1, SD)
        self._watchdog(rec)._check_format_pin(seg1.id, 1)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_FORMAT_CHANGED).count(), 0)

    def test_a_matching_later_segment_is_not_a_change(self):
        rec = self._rec()
        self._segment(rec, 1, SD)
        seg2 = self._segment(rec, 2, SD)
        self._watchdog(rec)._check_format_pin(seg2.id, 2)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_FORMAT_CHANGED).count(), 0)

    def test_the_change_is_measured_not_inferred_from_a_failover(self):
        """The same member drifting under us produces no failover at all, and is exactly
        the case a selection-time check structurally cannot see."""
        rec = self._rec()
        self._segment(rec, 1, SD)
        seg2 = self._segment(rec, 2, HD)
        seg2.channel_id = rec.channel_id      # no member change; the feed itself moved
        db.session.commit()
        self._watchdog(rec)._check_format_pin(seg2.id, 2)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_FORMAT_CHANGED).count(), 1)

    def test_every_divergent_segment_logs_its_own_event_and_none_alert(self):
        """A flapping feed is several facts about one file, each its own event on the
        recording - and no alert at all since dev/changelog/928: a mixed-format file is
        disclosed where the file is, not in the alert counter."""
        rec = self._rec()
        self._segment(rec, 1, SD)
        seg2 = self._segment(rec, 2, HD)
        seg3 = self._segment(rec, 3, HD)
        wd = self._watchdog(rec)
        wd._check_format_pin(seg2.id, 2)
        wd._check_format_pin(seg3.id, 3)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_FORMAT_CHANGED).count(), 2)
        self.assertEqual(Alert.query.filter_by(
            alert_type=RECORDING_FORMAT_CHANGED).count(), 0)

    def test_the_check_never_harms_the_capture_it_describes(self):
        """A diagnostic must never be able to break the recording it is diagnosing
        (CLAUDE.md Product Principles). A broken pin read must be swallowed, not raised
        into the watchdog's poll loop."""
        from unittest.mock import patch
        rec = self._rec()
        self._segment(rec, 1, SD)
        seg2 = self._segment(rec, 2, HD)
        with patch('app.recorder.recording_format_pin', side_effect=RuntimeError('boom')):
            self._watchdog(rec)._check_format_pin(seg2.id, 2)   # must not raise


if __name__ == '__main__':
    unittest.main()
