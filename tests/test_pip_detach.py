"""A deleted channel or group leaves every picture-in-picture channel that showed it
(dev/docs/BUGS.md 2026-11-25 @ 07:47:14 PM).

pip_channels.main_ref / inset_ref hold a channels.id or a channel_groups.id with no foreign key,
and neither id is AUTOINCREMENT, so a picture left naming a deleted one played whatever the next
sync or the next new group was given the id. Each delete path now unlinks the picture (its kind
stays, its ref goes) and refreshes the row's live plays after its commit.

No network, no real ffmpeg.
  python3 -m unittest tests.test_pip_detach
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import pip_channels as pip  # noqa: E402
from app.database import (  # noqa: E402
    PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP, PIP_SOURCE_SEARCH, PipChannel,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


def _row(name, main_kind, main_ref, inset_kind=PIP_SOURCE_SEARCH, inset_ref=None):
    row = PipChannel(name=name, main_kind=main_kind, main_ref=main_ref, inset_kind=inset_kind,
                     inset_ref=inset_ref, inset_query='news' if inset_kind == PIP_SOURCE_SEARCH else None)
    db.session.add(row)
    db.session.commit()
    return row.id


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.account = seed.make_account(name='Going')
        self.other = seed.make_account(name='Staying')
        db.session.commit()
        self.kept = seed.make_channel(self.other, stream_id=1, name='Stays')
        db.session.commit()
        self.refresh = mock.patch('app.output_feed.refresh_pip', return_value=(0, 0, 0)).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.t.cleanup()

    def _gone(self, row_id, role):
        db.session.expire_all()
        row = db.session.get(PipChannel, row_id)
        source = pip.source_of(row, role)
        pane = pip.resolve_source(source, {}, feed_id=0, label='a picture')
        described = pip.describe([row])[(row_id, role)]
        return row, pane, described

    def _missing(self, channel):
        now = datetime.utcnow()
        channel.last_seen_at = now - timedelta(days=30)
        channel.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()


class ChannelDeleteTests(_Base):
    def test_the_missing_channel_delete_unlinks_the_picture_and_a_reissued_id_is_not_played(self):
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        self._missing(ch)
        channel_id = ch.id
        row_id = _row('Two games', PIP_SOURCE_SEARCH, None, PIP_SOURCE_CHANNEL, channel_id)
        kept_id = _row('Other', PIP_SOURCE_CHANNEL, self.kept.id)

        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [channel_id]})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        reborn = seed.make_channel(self.other, stream_id=3, name='Someone else')
        db.session.commit()
        self.assertEqual(reborn.id, channel_id, 'SQLite re-issues the top id; the premise of the test')

        row, pane, described = self._gone(row_id, pip.ROLE_INSET)
        self.assertEqual(row.inset_kind, PIP_SOURCE_CHANNEL, 'the picture keeps its kind')
        self.assertIsNone(row.inset_ref)
        self.assertEqual(pane.state, pip.PANE_GONE)
        self.assertTrue(described.gone)
        self.assertNotIn('Someone else', described.title + described.short)
        self.assertEqual(db.session.get(PipChannel, kept_id).main_ref, self.kept.id)
        self.refresh.assert_called_once_with(row_id)

    def test_the_account_delete_unlinks_both_pictures(self):
        from app.routes.accounts import _delete_account_and_jobs
        a = seed.make_channel(self.account, stream_id=2, name='A')
        b = seed.make_channel(self.account, stream_id=3, name='B')
        db.session.commit()
        row_id = _row('Both', PIP_SOURCE_CHANNEL, a.id, PIP_SOURCE_CHANNEL, b.id)

        deleted, _name = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        db.session.expire_all()
        row = db.session.get(PipChannel, row_id)
        self.assertEqual((row.main_kind, row.main_ref, row.inset_kind, row.inset_ref),
                         (PIP_SOURCE_CHANNEL, None, PIP_SOURCE_CHANNEL, None))
        self.refresh.assert_called_once_with(row_id)

    def test_a_group_picture_with_the_same_number_is_left_alone(self):
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        self._missing(ch)
        channel_id = ch.id
        row_id = _row('Group', PIP_SOURCE_GROUP, channel_id)

        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [channel_id]})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(PipChannel, row_id).main_ref, channel_id)
        self.refresh.assert_not_called()


class GroupDeleteTests(_Base):
    def test_the_group_delete_unlinks_the_picture_and_a_reissued_id_is_not_played(self):
        group = seed.make_group(name='Feeds', members=[self.kept], in_guide=True)
        db.session.commit()
        group_id = group.id
        row_id = _row('Group', PIP_SOURCE_GROUP, group_id)
        channel_row = _row('Channel', PIP_SOURCE_CHANNEL, group_id)

        resp = self.t.client.post(f'/api/channel-groups/{group_id}/delete', json={})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        reborn = seed.make_group(name='Someone else', members=[self.kept], in_guide=True)
        db.session.commit()
        self.assertEqual(reborn.id, group_id, 'SQLite re-issues the top id; the premise of the test')

        row, pane, described = self._gone(row_id, pip.ROLE_MAIN)
        self.assertEqual(row.main_kind, PIP_SOURCE_GROUP)
        self.assertIsNone(row.main_ref)
        self.assertEqual(pane.state, pip.PANE_GONE)
        self.assertTrue(described.gone)
        self.assertNotIn('Someone else', described.title + described.short)
        self.assertEqual(db.session.get(PipChannel, channel_row).main_ref, group_id,
                         'a channel picture is not a group picture, whatever its number')
        self.refresh.assert_called_once_with(row_id)


class SaveTests(_Base):
    def test_a_removed_picture_sent_back_unchanged_is_refused_by_name(self):
        data = {'name': 'X', 'main': {'kind': PIP_SOURCE_CHANNEL, 'ref': None, 'query': ''},
                'inset': {'kind': PIP_SOURCE_SEARCH, 'ref': None, 'query': 'news'}}
        with self.assertRaises(pip.PipRefused) as ctx:
            pip.clean(data)
        self.assertEqual(ctx.exception.message, "The big picture's channel was removed. Choose another.")


if __name__ == '__main__':
    unittest.main()
