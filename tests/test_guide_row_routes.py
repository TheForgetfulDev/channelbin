"""Tier 2 - the two JSON routes that give a channel its own TV Guide row and take it away.

`POST /api/guide/channels/<id>/add` shipped without the `CHANNEL_ADDED_TO_GUIDE` event its
form-path twin (`routes/channels.py::toggle_channel`) has always written, so a channel added
from the search page left no trace on its own timeline while the identical action taken from
the channel page did. Removal had no JSON route at all - the search page synthesized a form
POST to `/channels/<id>/toggle` and let the redirect reload the page, which is why there was
no bulk remove and why a single-row remove cost the selection.

Both are fixed in `dev/changelog/792`. The invariant that ties them together: whichever door
a guide row moves through, the channel's timeline says so, and the hide cache is recomputed
because a guide row defers a hide.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (Channel, ChannelEvent, ChannelGroup,  # noqa: E402
                          ChannelGroupMember, CHANNEL_ADDED_TO_GUIDE,
                          CHANNEL_REMOVED_FROM_GUIDE)

REMOVE_URL = '/api/guide/channels/remove'


class _GuideRowRoutes(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()
        self.a = seed.make_channel(self.acct, stream_id=1, name='ESPN', in_guide=True)
        self.b = seed.make_channel(self.acct, stream_id=2, name='TNT', in_guide=True)
        self.out = seed.make_channel(self.acct, stream_id=3, name='Not In Guide')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def events(self, channel, kind):
        return ChannelEvent.query.filter_by(channel_id=channel.id, event_type=kind).all()


class RemoveChannelsFromGuideTests(_GuideRowRoutes):
    def test_it_removes_every_named_channel_and_reports_which(self):
        resp = self.t.client.post(REMOVE_URL, json={'channel_ids': [self.a.id, self.b.id]})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertEqual(sorted(resp.get_json()['removed']), sorted([self.a.id, self.b.id]))
        self.assertFalse(db.session.get(Channel, self.a.id).in_guide)
        self.assertFalse(db.session.get(Channel, self.b.id).in_guide)

    def test_each_removal_is_on_the_channels_own_timeline(self):
        """A guide row leaving is exactly the kind of state change that has to reach a
        surface - the same event the form path has always written."""
        self.t.client.post(REMOVE_URL, json={'channel_ids': [self.a.id]})
        evs = self.events(self.a, CHANNEL_REMOVED_FROM_GUIDE)
        self.assertEqual(len(evs), 1)
        self.assertIn('Removed from guide', evs[0].detail)

    def test_a_channel_already_out_of_the_guide_is_a_no_op_not_an_error(self):
        """The client's button is an offer, never the gate: a stale row payload must not
        be able to fail a request the database says is already satisfied."""
        resp = self.t.client.post(REMOVE_URL, json={'channel_ids': [self.out.id]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['removed'], [])
        self.assertEqual(self.events(self.out, CHANNEL_REMOVED_FROM_GUIDE), [])

    def test_a_mixed_request_removes_only_what_was_in_the_guide(self):
        resp = self.t.client.post(REMOVE_URL,
                                  json={'channel_ids': [self.a.id, self.out.id]})
        self.assertEqual(resp.get_json()['removed'], [self.a.id])

    def test_group_membership_is_untouched(self):
        """This takes away a channel's OWN row and nothing wider. A member whose group
        holds a row still has its listings on screen afterwards."""
        group = seed.make_group(name='Sports', members=[self.a, self.b], in_guide=True)
        db.session.commit()
        gid = group.id
        self.t.client.post(REMOVE_URL, json={'channel_ids': [self.a.id, self.b.id]})
        db.session.expire_all()
        self.assertEqual(ChannelGroupMember.query.filter_by(group_id=gid).count(), 2)
        self.assertTrue(db.session.get(ChannelGroup, gid).in_guide)

    def test_an_empty_or_missing_selection_is_refused(self):
        for body in ({}, {'channel_ids': []}):
            resp = self.t.client.post(REMOVE_URL, json=body)
            self.assertEqual(resp.status_code, 400)
            self.assertIn('error', resp.get_json())

    def test_a_malformed_id_list_is_refused_rather_than_guessed_at(self):
        """`True` is an `int` in Python and would silently read as channel 1, which is why
        the shared parser rejects it - never a bare isinstance check."""
        for raw in ('not-a-list-of-ids', [True], [{'id': 1}]):
            resp = self.t.client.post(REMOVE_URL, json={'channel_ids': raw})
            self.assertEqual(resp.status_code, 400, raw)

    def test_an_unknown_id_is_skipped_rather_than_failing_the_request(self):
        resp = self.t.client.post(REMOVE_URL, json={'channel_ids': [self.a.id, 999999]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['removed'], [self.a.id])


class AddChannelToGuideEventTests(_GuideRowRoutes):
    def test_adding_through_the_json_route_lands_on_the_channels_timeline(self):
        """The form path at /channels/<id>/toggle has always written this event; the JSON
        route the search page uses did not, so the same action recorded itself or not
        depending on which page it was taken from."""
        resp = self.t.client.post(f'/api/guide/channels/{self.out.id}/add')
        self.assertEqual(resp.status_code, 200)
        evs = self.events(self.out, CHANNEL_ADDED_TO_GUIDE)
        self.assertEqual(len(evs), 1)
        self.assertIn('Added to guide', evs[0].detail)

    def test_adding_a_channel_that_is_already_in_the_guide_writes_nothing(self):
        """The route no-ops on an in-guide channel, so a second click must not stack a
        second event claiming it was added again."""
        self.t.client.post(f'/api/guide/channels/{self.a.id}/add')
        self.assertEqual(self.events(self.a, CHANNEL_ADDED_TO_GUIDE), [])


if __name__ == '__main__':
    unittest.main()
