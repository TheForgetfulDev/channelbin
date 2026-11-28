"""A playlist that comes back with every channel under a new id, over real HTTP
(dev/docs/DESIGN-channel-successors.md sections 11 and 12, dev/changelog/1794).

The `renumber` fault keeps each channel's name and the id inside its stream URL and gives it a
new playlist id - what a playlist-rewriting service did to two prod accounts when it expired
(dev/changelog/1788). ChannelBin's unmodified `_do_sync` then makes a new row for every
channel and leaves the group members on rows no sync updates. These tests run that sync and
then the replacement: every stranded member is proposed, accepting moves it into the old one's
place with its switches, and the variant that moves the URL id too proposes nothing.

No network: the simulator listens on loopback and netguard allows in-process sockets there.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import channel_successors as successors  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (ChannelGroup, ChannelGroupEvent, ChannelGroupMember,  # noqa: E402
                          ChannelReplacement, GROUP_MEMBER_REPLACED, REPLACE_MATCH_STREAM_ID)
from tests.support import seed  # noqa: E402
from tests.support.sim_sync import SimSyncCase  # noqa: E402


class RenumberTests(SimSyncCase):
    def _grouped(self, aid, n=6):
        """A synced account and one recording group of `n` of its channels, the third one's
        Recording switch off so a moved switch is visible."""
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        picks = [c for c in self.channels(aid) if c.provider_stream_id is not None][:n]
        group = seed.make_group(name='Sports', members=picks, disabled={picks[2].id})
        db.session.commit()
        return group.id, [c.id for c in picks]

    def _members(self, gid):
        return {m.channel_id: (m.position, m.recording_enabled, m.test_enabled)
                for m in ChannelGroupMember.query.filter_by(group_id=gid).all()}

    def test_a_renumbered_playlist_strands_members_and_each_is_proposed_and_moved(self):
        """Section 12, first bullet: a sync through the fault strands the group's members, the
        review proposes each one on the same account, and accepting puts each new channel at
        the old one's position with the old one's switches, the recording-enabled count
        unchanged, with an event for each."""
        aid = self.add_xtream()
        gid, old_ids = self._grouped(aid)
        before = self._members(gid)
        count_before = sum(1 for p in before.values() if p[1])

        self.sim.inject('renumber', 'north-tv')
        self.assertEqual(self.sync(aid).status, 'SUCCESS')

        stranded = successors.stranded_channels()
        self.assertEqual(sorted(c.id for c in stranded), sorted(old_ids))
        props = successors.proposals(stranded)
        for old in stranded:
            rows = successors.for_target(props[old.id], old, 'own')
            self.assertEqual(len(rows), 1, old.name)
            new, kind = rows[0]
            self.assertEqual(kind, REPLACE_MATCH_STREAM_ID)
            self.assertNotEqual(new.id, old.id)
            self.assertEqual((new.name, new.provider_stream_id),
                             (old.name, old.provider_stream_id))

        pairs = successors.single_match_pairs(props, stranded, 'own')
        done, skipped = successors.accept_many(pairs, successors.CARRY_PIECES, cfg=load_config())
        db.session.expire_all()
        self.assertEqual((len(done), skipped), (len(old_ids), []))

        after = self._members(gid)
        new_of = {r['old_id']: r['new_id'] for r in done}
        self.assertEqual(after, {new_of[old]: place for old, place in before.items()})
        self.assertEqual(sum(1 for p in after.values() if p[1]), count_before)
        self.assertEqual(ChannelGroupEvent.query.filter_by(
            group_id=gid, event_type=GROUP_MEMBER_REPLACED).count(), len(old_ids))
        self.assertEqual(ChannelReplacement.query.count(), len(old_ids))
        self.assertEqual(successors.stranded_channels(), [])
        self.assertIsNotNone(db.session.get(ChannelGroup, gid))

    def test_a_renumber_that_moves_the_url_id_too_proposes_nothing(self):
        """The fault's `url_id: new` variant: nothing on the account still names the stream,
        so each stranded member is listed with no replacement rather than guessed at."""
        aid = self.add_xtream()
        _gid, old_ids = self._grouped(aid, n=3)
        self.sim.inject('renumber', 'north-tv', url_id='new')
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        stranded = successors.stranded_channels()
        self.assertEqual(sorted(c.id for c in stranded), sorted(old_ids))
        props = successors.proposals(stranded)
        self.assertTrue(all(props[c.id] == [] for c in stranded))
        self.assertEqual(successors.single_match_pairs(props, stranded, 'own'), [])

    def test_an_m3u_panel_renumbered_through_its_cuid(self):
        """On an M3U panel the playlist's id is its CUID; a renumber there strands members
        the same way and proposes the same rows by provider stream id."""
        aid = self.add_m3u()
        _gid, old_ids = self._grouped(aid, n=3)
        self.sim.inject('renumber', 'vale-m3u')
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        stranded = successors.stranded_channels()
        self.assertEqual(sorted(c.id for c in stranded), sorted(old_ids))
        props = successors.proposals(stranded)
        self.assertEqual(len(successors.single_match_pairs(props, stranded, 'own')), 3)


if __name__ == '__main__':
    unittest.main()
