"""A guide-service account whose playlist carries a host that no longer resolves and a login
the provider stopped accepting, with both rows turned off, records through the host and login
the user added - every segment, from the first (dev/changelog/1670, DESIGN-account-providers.md
§13).

The simulator serves a panel whose own playlist is fine, so the sync lists its real host and
login first; the playlist is then moved onto a dead host and an expired login the way a
guide-service file carries them, and a second listing rewrites the playlist's rows in place,
switches untouched. The user adds the panel's real host and login and turns the playlist's two
rows off. Nothing about the capture path is mocked: the start takes a seat on the login that
is on, the launch renders its credentials into the URL, and the URL's host is the one the
handover wrote.

Two ways it would fail, both loud: a launch on the dead host is refused by the netguard before
ffmpeg starts (it is not the simulator's address), and a launch carrying the expired login is
answered 401 by the simulator, which the ledger shows. Two rules keep the expired login off
the wire and either one alone holds this green: an off login takes no seat
(account_links.login_seats_for_accounts) and a launch moves a seat off a login that is off
(connection_limits.reseat_before_relaunch); with both reverted the first segment goes out on
the expired login and is refused.
"""
import unittest

from app import account_links as links
from app import db
from app.database import AccountHost, Channel, Login, REC_STATUS_COMPLETED
from sim import answer_key as ak

from .support import CHANNEL_LOOP, CaptureCase

DEAD_HOST = 'cdn.oldreseller.invalid'
EXPIRED = ('k8x2expired', 'oldpw')

# One backend, one panel, one login with a seat: the reseller the user's own host and login
# come from.
PLAYLIST_ROWS_WORLD = {
    'seed': 31,
    'backends': [
        {'name': 'cove', 'channels': 8, 'content': CHANNEL_LOOP, 'burst_seconds': 4.0,
         'radio_share': 0.0, 'no_id_share': 0.0},
    ],
    'panels': [{'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'bare': 1.0}}],
    'logins': [{'backend': 'cove', 'username': 'coveuser', 'password': 'covepass1',
                'panels': ['cove-tv'], 'max_connections': 1}],
}


class PlaylistRowsOffScenario(CaptureCase):
    world_source = PLAYLIST_ROWS_WORLD
    loops = (CHANNEL_LOOP,)

    def _carry_the_guide_services_playlist(self, account_id):
        """Every channel's URLs as the guide-service file has them: the dead host and the
        expired login in front of the same stream id."""
        for ch in Channel.query.filter_by(account_id=account_id).all():
            url = f'http://{DEAD_HOST}/{EXPIRED[0]}/{EXPIRED[1]}/{ch.stream_id}'
            ch.raw_stream_url = ch.stream_url = url
        db.session.commit()

    def test_a_capture_with_the_playlists_rows_off_records_through_the_users_own(self):
        account_id = self.accounts['cove-tv']
        real_host = links.host_of(Channel.query.filter_by(account_id=account_id).first().stream_url)
        listed = Login.query.filter_by(playlist_account_id=account_id).one()
        self.assertEqual(listed.username, 'coveuser', 'the sync listed the playlist\'s login')

        self._carry_the_guide_services_playlist(account_id)
        facts = links.playlist_facts(account_id)
        changes = links.list_playlist_rows(account_id, facts['host'], facts['pair'])
        self.assertEqual({(c['what'], c['new']) for c in changes},
                         {('host', DEAD_HOST), ('login', EXPIRED[0])})

        links.add_host(account_id, real_host)
        links.add_login(account_id, 'tom', 'coveuser', 'covepass1', 1)
        db.session.expire_all()
        playlist_host = AccountHost.query.filter_by(account_id=account_id, from_playlist=True).one()
        playlist_login = Login.query.filter_by(playlist_account_id=account_id).one()
        links.set_host_enabled(account_id, playlist_host.id, False)
        links.set_login_enabled(account_id, playlist_login.id, False)

        ch = self.channel('cove-tv', 0)
        self.assertIn(real_host, ch.stream_url, 'the handover moved the URLs off the dead host')
        self.assertIn(f'/{EXPIRED[0]}/', ch.stream_url, 'the credentials are swapped at launch')
        rid = self.record(ch, seconds=20)
        rec = self.finish(rid, 90)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        streams = self.sim.ledger.entries(endpoint='stream')
        self.assertTrue(streams, 'nothing reached the simulator')
        self.assertEqual({r.login for r in streams}, {'coveuser'}, streams)
        self.assertEqual([r for r in streams if r.status not in ak.SERVED and r.status != 302], [],
                         'a stream request was refused - the expired login went out')
        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))


if __name__ == '__main__':
    unittest.main()
