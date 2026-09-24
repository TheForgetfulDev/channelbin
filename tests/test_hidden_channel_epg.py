"""Hidden channels carry no EPG - the import filter, the purge, and the rebased guard.

The largest single payoff of the hiding feature (dev/changelog/781): a channel nobody can
see costs nothing to store guide data for, so none is stored. Three pieces have to hold
together, and each fails silently on its own:

  - `import_source` builds `channel_map` from VISIBLE channels only, so a <programme> for a
    hidden channel becomes no rows. `_count_projected_epg_entries` is handed the same map,
    so the collapse guard's count pass follows for free - if it did not, the guard would
    compare a whole-account projection against a visible-only baseline and refuse every
    import after a large hide.
  - `channel_hiding.recompute()` deletes the entries of whatever is hidden, in its own
    transaction. Without it the payoff waits for the next sync and the row counts on screen
    describe a database that does not exist.
  - The collapse guard's baseline is counted LIVE over visible channels rather than read
    from the cached `account.epg_entry_count`. That cached total is what the last sync
    produced for EVERY channel, so after hiding a third of an account it reads a legitimate
    drop as a provider returning garbage - refusing the import and freezing that account's
    guide with an alert blaming the provider for what the user asked for.

The un-hide half is disclosure, not restoration: nothing here fetches the entries back, so
`hide_state()` reports the gap and the channel page says when it closes.

No network: XMLTV is built in-process as bytes, never fetched. Runs against a throwaway temp
SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_hidden_channel_epg
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import channel_hiding  # noqa: E402
from app.accounts import import_source, _visible_source_baseline  # noqa: E402
from app.database import Channel, EPGEntry  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402


def _xmltv(entries):
    """entries: iterable of (epg_channel_id, offset_minutes, duration_minutes), one
    <programme> each, well inside the default 3-day import window."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for chid, offset_min, dur_min in entries:
        start = now + timedelta(minutes=offset_min)
        stop = start + timedelta(minutes=dur_min)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{chid}">'
            f'<title>Show</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class _EpgHidingTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self.acct = seed.make_account(name='Alpha')
        make_epg_source(self.acct)

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _channel(self, name, **kw):
        return seed.make_channel(self.acct, name=name, **kw)

    def _hide(self, channel, value=True):
        channel_hiding.set_hidden_override(channel, value)
        channel_hiding.recompute([channel.id])
        db.session.commit()

    def _entries(self, channel):
        return EPGEntry.query.filter_by(channel_id=channel.id).count()

    def _cfg(self, threshold_pct=20):
        return {'sync': {'epg_collapse_threshold_percent': threshold_pct}}


class ImportSkipsHiddenChannelsTests(_EpgHidingTestCase):
    """The one WHERE clause that is the whole saving."""

    def test_a_hidden_channel_gets_no_entries_from_an_import(self):
        visible = self._channel('Visible')
        hidden = self._channel('Hidden')
        self._hide(hidden)

        xml = _xmltv([(visible.epg_channel_id, 60, 30),
                      (hidden.epg_channel_id, 60, 30)])
        synced, reason = import_source(make_epg_source(self.acct), xml, epg_days=3, cfg=self._cfg())

        self.assertIsNone(reason)
        self.assertEqual(synced, 1, 'only the visible channel may produce a row')
        self.assertEqual(self._entries(visible), 1)
        self.assertEqual(self._entries(hidden), 0)

    def test_a_deferred_hide_still_imports(self):
        """Deferred means the channel is still offered, so it still needs its guide data.

        `hidden_deferred` is set while a TV Guide row or a group membership keeps a matched
        channel visible. Reading it as "hidden" here would empty the guide row of the very
        channel that is keeping itself visible.
        """
        ch = self._channel('In The Guide', in_guide=True)
        self._hide(ch)
        db.session.expire_all()
        ch = db.session.get(Channel, ch.id)
        self.assertFalse(ch.hidden)
        self.assertTrue(ch.hidden_deferred)

        synced, reason = import_source(
            make_epg_source(self.acct), _xmltv([(ch.epg_channel_id, 60, 30)]), epg_days=3, cfg=self._cfg())

        self.assertIsNone(reason)
        self.assertEqual(synced, 1)
        self.assertEqual(self._entries(ch), 1)

    def test_an_account_with_every_channel_hidden_imports_nothing(self):
        ch = self._channel('Only One')
        self._hide(ch)

        synced, reason = import_source(
            make_epg_source(self.acct), _xmltv([(ch.epg_channel_id, 60, 30)]), epg_days=3, cfg=self._cfg())

        self.assertEqual(synced, 0)
        self.assertIsNone(reason, 'nothing to import is not a degradation')
        self.assertEqual(self._entries(ch), 0)

    def test_the_delete_still_clears_a_hidden_channel_left_holding_entries(self):
        """The import's delete is deliberately WIDER than its insert.

        A channel hidden by some path that never purged would otherwise keep its entries
        forever behind a filter that only stops re-creating them.
        """
        visible = self._channel('Visible')
        stranded = self._channel('Stranded')
        seed.make_epg_entry(stranded, offset_minutes=60)
        db.session.commit()
        # Hidden WITHOUT a recompute, i.e. exactly the state a missed purge would leave.
        stranded.hidden = True   # hidden-cache-write-ok: simulating a stale cache on purpose
        db.session.commit()
        self.assertEqual(self._entries(stranded), 1)

        import_source(make_epg_source(self.acct), _xmltv([(visible.epg_channel_id, 60, 30)]),
                      epg_days=3, cfg=self._cfg(threshold_pct=0))

        self.assertEqual(self._entries(stranded), 0)


class PurgeOnHideTests(_EpgHidingTestCase):
    """Hiding deletes the entries, in the same transaction that sets the column."""

    def test_hiding_a_channel_deletes_its_epg(self):
        ch = self._channel('Goes Away')
        seed.make_epg_entry(ch, offset_minutes=30)
        seed.make_epg_entry(ch, offset_minutes=-600)   # already ended - goes too
        db.session.commit()
        self.assertEqual(self._entries(ch), 2)

        self._hide(ch)

        self.assertEqual(self._entries(ch), 0)

    def test_a_visible_channel_keeps_its_epg_through_a_recompute(self):
        ch = self._channel('Stays')
        seed.make_epg_entry(ch)
        db.session.commit()

        channel_hiding.recompute()
        db.session.commit()

        self.assertEqual(self._entries(ch), 1)

    def test_a_deferred_hide_keeps_its_epg(self):
        ch = self._channel('Protected', in_guide=True)
        seed.make_epg_entry(ch)
        db.session.commit()

        self._hide(ch)

        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, ch.id).hidden_deferred)
        self.assertEqual(self._entries(ch), 1,
                         'a channel still on screen must keep the data that fills it')

    def test_the_purge_fires_when_protection_clears_rather_than_at_the_hide(self):
        """The transition that matters is not always the one the user performed.

        A deferred hide lands later, when the guide row goes away - and the delete has to
        travel with it, which is why it rides in recompute() rather than in the hide door.
        """
        ch = self._channel('Deferred Then Real', in_guide=True)
        seed.make_epg_entry(ch)
        db.session.commit()
        self._hide(ch)
        self.assertEqual(self._entries(ch), 1)

        ch = db.session.get(Channel, ch.id)
        ch.in_guide = False
        channel_hiding.recompute([ch.id])
        db.session.commit()

        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, ch.id).hidden)
        self.assertEqual(self._entries(ch), 0)

    def test_the_purge_is_scoped_and_leaves_other_accounts_alone(self):
        other = seed.make_account(name='Beta')
        theirs = seed.make_channel(other, name='Theirs')
        theirs.hidden = True   # hidden-cache-write-ok: out of this pass's scope on purpose
        seed.make_epg_entry(theirs)
        mine = self._channel('Mine')
        seed.make_epg_entry(mine)
        db.session.commit()

        channel_hiding.recompute(account_id=self.acct.id)
        db.session.commit()

        self.assertEqual(self._entries(theirs), 1,
                         'a scoped pass must not delete outside its scope')

    def test_running_the_purge_twice_changes_nothing(self):
        """A recompute from the top, not a decrement - a retry re-runs the whole closure."""
        ch = self._channel('Idempotent')
        seed.make_epg_entry(ch)
        db.session.commit()
        self._hide(ch)

        self.assertEqual(channel_hiding.purge_hidden_epg(), 0)

    def test_un_hiding_does_not_bring_the_entries_back(self):
        """Decided, not an oversight: the gap is disclosed rather than closed by a fetch."""
        ch = self._channel('Back Again')
        seed.make_epg_entry(ch)
        db.session.commit()
        self._hide(ch)

        self._hide(ch, value=None)

        db.session.expire_all()
        self.assertFalse(db.session.get(Channel, ch.id).hidden)
        self.assertEqual(self._entries(ch), 0)


class CollapseGuardBaselineTests(_EpgHidingTestCase):
    """The guard compares like with like, or it freezes a guide for doing as it was told."""

    def test_the_baseline_counts_only_visible_channels(self):
        visible = self._channel('Visible')
        hidden = self._channel('Hidden')
        for _ in range(4):
            seed.make_epg_entry(visible)
        seed.make_epg_entry(hidden)
        db.session.commit()
        # Hidden without a purge, so the rows are really there to be excluded.
        hidden.hidden = True   # hidden-cache-write-ok: proving the COUNT filters, not the purge
        db.session.commit()

        self.assertEqual(_visible_source_baseline(make_epg_source(self.acct).id), 4)

    def test_the_cached_column_no_longer_arms_the_guard(self):
        """The whole rebase in one assertion: a stale high count cannot refuse an import."""
        ch = self._channel('Only One')
        self.acct.epg_entry_count = 100_000
        db.session.commit()

        synced, reason = import_source(
            make_epg_source(self.acct), _xmltv([(ch.epg_channel_id, 60, 30)]), epg_days=3, cfg=self._cfg())

        self.assertIsNone(reason)
        self.assertEqual(synced, 1)

    def test_hiding_most_of_an_account_does_not_look_like_a_collapse(self):
        """The case this exists for: hide 4 of 5 channels, then sync.

        The old baseline was the previous sync's whole-account total, so the next import -
        which now projects only the surviving channel's entries - fell under the 20%
        threshold and was refused, alerting the user that their provider had returned
        garbage.
        """
        keep = self._channel('Keeper')
        hidden = [self._channel(f'Gone {i}') for i in range(9)]
        for ch in [keep] + hidden:
            for _ in range(5):
                seed.make_epg_entry(ch)
        db.session.commit()
        # What the last sync really produced across all ten channels. Under the old rule
        # this armed the guard at 10 required against a projection of 5.
        self.acct.epg_entry_count = 50
        db.session.commit()
        for ch in hidden:
            self._hide(ch)

        xml = _xmltv([(keep.epg_channel_id, 60 + i * 30, 30) for i in range(5)])
        synced, reason = import_source(make_epg_source(self.acct), xml, epg_days=3, cfg=self._cfg())

        self.assertIsNone(reason, f'the guard refused a legitimate import: {reason}')
        self.assertEqual(synced, 5)

    def test_a_real_collapse_is_still_refused(self):
        """The rebase must not become a way through the guard."""
        ch = self._channel('Busy')
        for _ in range(100):
            seed.make_epg_entry(ch)
        db.session.commit()

        synced, reason = import_source(
            make_epg_source(self.acct), _xmltv([(ch.epg_channel_id, 60, 30)]), epg_days=3, cfg=self._cfg())

        self.assertEqual(synced, 0)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith('import refused:'))
        self.assertEqual(self._entries(ch), 100, 'a refusal keeps the old EPG untouched')


class UnHideDisclosureTests(_EpgHidingTestCase):
    """Un-hiding says what it cannot do."""

    def test_hide_state_reports_the_gap_only_for_a_visible_channel(self):
        ch = self._channel('Reported')
        self.assertTrue(channel_hiding.hide_state(ch, epg_gap=True)['epg_gap'])
        self.assertFalse(channel_hiding.hide_state(ch)['epg_gap'])

    def test_the_single_door_reports_the_gap_on_un_hide(self):
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        ch = self._channel('Round Trip')
        seed.make_epg_entry(ch)
        db.session.commit()
        client = self.t.app.test_client()

        client.post(f'/channels/{ch.id}/hide', json={'override': True})
        body = client.post(f'/channels/{ch.id}/hide', json={'override': None}).get_json()

        self.assertFalse(body['hide']['hidden'])
        self.assertTrue(body['hide']['epg_gap'],
                        'un-hiding leaves an empty guide and has to say so')

    def test_a_channel_that_never_had_epg_data_is_not_reported_as_a_gap(self):
        """No epg_channel_id means no guide data was ever coming - a different fact."""
        ch = self._channel('No EPG Id')
        ch.epg_channel_id = None
        db.session.commit()
        client = self.t.app.test_client()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

        body = client.post(f'/channels/{ch.id}/hide', json={'override': False}).get_json()

        self.assertFalse(body['hide']['epg_gap'])

    def test_the_bulk_door_counts_the_gap(self):
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        with_epg = self._channel('Had Data')
        without = self._channel('Never Had Data')
        seed.make_epg_entry(with_epg)
        db.session.commit()
        client = self.t.app.test_client()
        ids = [with_epg.id, without.id]

        client.post('/api/channels/hide', json={'channel_ids': ids, 'override': True})
        body = client.post('/api/channels/hide',
                           json={'channel_ids': ids, 'override': None}).get_json()

        self.assertEqual(body['matched'], 2)
        self.assertEqual(body['epg_gap'], 2,
                         'both are visible again with no entries stored for either')


if __name__ == '__main__':
    unittest.main()
