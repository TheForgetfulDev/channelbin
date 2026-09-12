"""Both Accounts surfaces and their JSON API against DESIGN.md section 17.

Chunk 5b of the fableUI rollout. Part 1 (dev/changelog/455) added `/accounts/<id>` - a page
that did not exist before - and the JSON API behind it; part 2 (dev/changelog/456) converted
`/accounts` to list rows and deleted what the account page replaced. Each case here is a
decision from section 17 that a careless edit would quietly undo:

  * **The status-dependent primary action is rendered EXACTLY TWICE** (17.5 item 3, 17.6):
    once in the inline action bar you land on, once in the phone's sticky bottom bar. One
    copy means half of "both" went missing; three means something is keeping markup it
    should have dropped. It is one Jinja macro, so the two can never offer different
    things - which is the property these cases actually protect.
  * **The primary is enumerated per status** (17.3) - Sync now / Retry sync / Cancel sync /
    Sync for the first time - never a generic "Sync", and an unhandled status is loud
    rather than silently rendering the OK branch.
  * **The provider's error text must stay reachable** (17.6). It came OFF the list row in
    this redesign, so this page is the only place it appears; a banner that stopped
    rendering it would delete the answer to "why did this fail" rather than relocate it.
  * **An account's own URLs are masked in full** (17.4, DESIGN-secrets.md 4.2) - the path
    may BE the credential - and the stored password is never served back out, by any
    surface, in any form.
  * **An empty section still renders inside its own card, head and all** (17.3). A bare
    empty state where a card should be leaves the section unlabelled, which is unusable on
    a page whose sections the user reorders.
  * **An inherited setting says so** with `(global)` (17.3), so a per-account override is
    distinguishable from the app default at a glance.
  * **No action is silently dropped by the redesign** (17.6). This replaces the check
    dev/mockups/build31.py ran while the design was being drawn.
  * **The list row's subtractions stay subtracted** (17.1, 17.5): no provider error text
    (not even inside a tooltip attribute), no inline sync table, no tooltip on a badge. A
    subtraction has no visible symptom when it silently comes back, which is exactly why it
    needs a test. The sparkline is the one deliberate exception (17.5 item 6, corrected
    2026-08-13, dev/changelog/616): its bars carry a `[data-tip]` with per-sync counts, kept
    out of row-navigation the same way `/recordings`' `NO_NAV` excludes tooltip targets.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_accounts_page_conformance
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Account, AccountSyncLog, XtreamAccount  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The password only ever exists in the DB. If this string appears in any response body the
# account page or its API has started handing a provider credential back out.
SECRET_PASSWORD = 'pw-must-never-be-served'


def _account(name, status='OK', **kw):
    """seed.make_account() hardcodes status and both URLs, and every case here is about
    one of those - so they are set on the way back rather than passed in."""
    overrides = {k: kw.pop(k) for k in ('m3u_url', 'epg_url') if k in kw}
    acc = seed.make_account(name=name, **kw)
    acc.status = status
    for key, value in overrides.items():
        setattr(acc, key, value)
    db.session.flush()
    return acc


def _sync_log(account, *, status='SUCCESS', minutes_ago=60, channels=100, epg=1000,
              error=None, seconds=30, added=None, removed=None, malformed=None,
              duplicate=None):
    started = datetime.utcnow() - timedelta(minutes=minutes_ago)
    log = AccountSyncLog(
        account_id=account.id, started_at=started,
        completed_at=started + timedelta(seconds=seconds) if seconds is not None else None,
        status=status, channels_synced=channels, epg_entries_synced=epg,
        error_message=error, channels_added=added, channels_removed=removed,
        skipped_malformed_urls=malformed, skipped_duplicate_stream_ids=duplicate)
    db.session.add(log)
    db.session.flush()
    return log


class AccountPageTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _get(self, account):
        res = self.client.get(f'/accounts/{account.id}')
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    @staticmethod
    def _srow(details_html, label):
        """The single .srow div for a given label. A plain string split on the label is
        unreliable here - several rows' data-tip text starts by repeating its own label."""
        m = re.search(r'<div class="srow[^"]*"><span class="sk">' + re.escape(label)
                      + r'</span>.*?</div>', details_html, re.S)
        return m.group(0) if m else ''

    # ── The primary action: exactly two copies, and named per status ─────────

    def test_status_action_is_rendered_exactly_twice(self):
        """17.6: the inline bar and the sticky bar, no more and no fewer."""
        for status, label in (('OK', 'Sync now'), ('ERROR', 'Retry sync'),
                              ('SYNCING', 'Cancel sync'), ('UNSYNCED', 'Sync for the first time')):
            with self.subTest(status=status):
                acc = _account(f'Acct {status}', status)
                db.session.commit()
                html = self._get(acc)
                self.assertEqual(html.count(f'{label}</button>'), 2,
                                 f'{status}: the primary action must appear exactly twice')

    def test_primary_action_is_enumerated_never_generic(self):
        """17.3: each status names its own action, and no other status's."""
        cases = {
            'OK': ('Sync now', ['Retry sync', 'Cancel sync', 'Sync for the first time']),
            'ERROR': ('Retry sync', ['Cancel sync', 'Sync for the first time']),
            'SYNCING': ('Cancel sync', ['Retry sync', 'Sync for the first time']),
            'UNSYNCED': ('Sync for the first time', ['Retry sync', 'Cancel sync']),
        }
        for status, (expected, forbidden) in cases.items():
            with self.subTest(status=status):
                acc = _account(f'Enum {status}', status)
                db.session.commit()
                html = self._get(acc)
                self.assertIn(expected, html)
                for other in forbidden:
                    self.assertNotIn(f'{other}</button>', html,
                                     f'{status} must not offer {other}')

    def test_unknown_status_is_loud_rather_than_rendering_the_ok_branch(self):
        """A status nobody enumerated must not silently land in the OK branch and offer
        "Sync now" - the trailing branch says the page does not know what it is looking
        at, per CLAUDE.md's enumerated-status rule."""
        acc = _account('Weird', 'TELEPORTING')
        db.session.commit()
        html = self._get(acc)
        self.assertNotIn('Sync now</button>', html)
        self.assertIn('TELEPORTING', html)

    # ── Banners ─────────────────────────────────────────────────────────────

    def test_failed_sync_banner_carries_the_providers_own_error_text(self):
        """17.6: the error came off the list row, so this page is the only place it
        appears. Losing it here deletes the answer to "why did this fail"."""
        acc = _account('Broken', 'ERROR',
                                last_error='provider returned 403 Forbidden')
        _sync_log(acc, status='SUCCESS', minutes_ago=600)
        db.session.commit()
        html = self._get(acc)
        self.assertIn('provider returned 403 Forbidden', html)
        self.assertIn('The last sync failed', html)

    def test_no_error_banner_on_a_healthy_account(self):
        acc = _account('Fine', 'OK', last_error='stale text')
        db.session.commit()
        self.assertNotIn('The last sync failed', self._get(acc))

    def test_constructed_urls_banner_only_when_there_are_any(self):
        with_urls = _account('Constructed', 'OK',
                                      constructed_stream_url_count=42, channel_count=100)
        without = _account('Plain', 'OK', constructed_stream_url_count=0)
        db.session.commit()
        self.assertIn('stream URLs were built by ChannelBin', self._get(with_urls))
        self.assertNotIn('stream URLs were built by ChannelBin', self._get(without))

    def test_auto_sync_off_banner_only_for_a_synced_account(self):
        """A never-synced account's page already says so in its lead sentence and its
        action bar; a third copy of "nothing is arriving" is noise."""
        off = _account('Manual', 'OK', sync_enabled=False)
        never = _account('NewManual', 'UNSYNCED', sync_enabled=False)
        db.session.commit()
        self.assertIn('Automatic sync is off for this account', self._get(off))
        self.assertNotIn('Automatic sync is off for this account', self._get(never))

    # ── Sections ────────────────────────────────────────────────────────────

    def test_all_four_sections_render_and_are_reorderable(self):
        acc = _account('Sections', 'OK')
        db.session.commit()
        html = self._get(acc)
        for section in ('details', 'content', 'history', 'activity'):
            self.assertIn(f'data-section="{section}"', html)

    def test_an_empty_section_still_renders_its_card_head(self):
        """17.3: a bare empty state where a card should be leaves the section
        unlabelled, which is unidentifiable on a page whose sections the user reorders."""
        acc = _account('NoSyncs', 'UNSYNCED')
        db.session.commit()
        html = self._get(acc)
        history = html.split('data-section="history"')[1].split('data-section=')[0]
        self.assertIn('<h2>Sync history', history)
        self.assertIn('No syncs yet', history)
        activity = html.split('data-section="activity"')[1].split('</div>\n\n</div>')[0]
        self.assertIn('<h2>Activity', activity)
        self.assertIn('Nothing has happened yet', activity)

    def test_inherited_settings_are_marked_global_and_overrides_are_not(self):
        """17.3: "6h because this account says 6h" and "6h because Settings says 6h"
        behave differently the moment the global changes, so they must not look the same."""
        inherited = _account('Inherits', 'OK',
                                      sync_interval_hours=None, max_connections=None)
        db.session.commit()
        details = self._get(inherited).split('data-section="details"')[1].split('data-section=')[0]
        self.assertEqual(details.count('sv-inherit'), 3,
                         'normalization, interval and max connections all inherit here')

        override = _account('Overrides', 'OK',
                                     sync_interval_hours=2, max_connections=5,
                                     url_normalization='hls')
        db.session.commit()
        details = self._get(override).split('data-section="details"')[1].split('data-section=')[0]
        self.assertNotIn('sv-inherit', details)
        self.assertIn('every 2h', details)

    def test_provider_account_info_renders_for_xtream_only(self):
        """dev/changelog/534: the Xtream auth response's user_info fields (status, trial,
        expiry, connection entitlement, allowed formats) render only for an xtream account
        - an M3U account never authenticates this way and must not show the rows at all."""
        xt = XtreamAccount(name='Xt Provider Info', account_type='xtream',
                           base_url='http://x.test', username='u', password='p', status='OK',
                           provider_status='Active', provider_is_trial=False,
                           provider_max_connections=1, provider_active_connections=0,
                           provider_allowed_output_formats='ts,m3u8',
                           provider_stream_origin='http://cdn.x.test:80')
        db.session.add(xt)
        db.session.commit()
        details = self._get(xt).split('data-section="details"')[1].split('data-section=')[0]
        self.assertIn('Provider status', details)
        self.assertIn('Active', details)
        self.assertIn('Trial account', details)
        self.assertIn('Provider max connections', details)
        self.assertIn('Active connections (last sync)', details)
        self.assertIn('Allowed output formats', details)
        self.assertIn('ts, m3u8', details)
        self.assertIn('Declared stream origin', details)
        self.assertIn('http://cdn.x.test:80', details)

        m3u = _account('M3u No Provider Info')
        db.session.commit()
        details = self._get(m3u).split('data-section="details"')[1].split('data-section=')[0]
        self.assertNotIn('Provider status', details)
        self.assertNotIn('Declared stream origin', details)

    def test_provider_account_info_missing_fields_render_as_not_reported(self):
        """Different providers return different fields (dev/changelog/534) - a field the
        provider never sent must degrade to visible faint text, not disappear or crash."""
        xt = XtreamAccount(name='Xt No Provider Info', account_type='xtream',
                           base_url='http://x.test', username='u', password='p', status='OK')
        db.session.add(xt)
        db.session.commit()
        details = self._get(xt).split('data-section="details"')[1].split('data-section=')[0]
        self.assertIn('not reported', details)

    def test_provider_max_connections_mismatch_is_flagged(self):
        """The backlog ask: warn when the provider's reported cap disagrees with what
        ChannelBin is configured to enforce."""
        mismatched = XtreamAccount(name='Mismatched', account_type='xtream',
                                   base_url='http://x.test', username='u', password='p',
                                   status='OK', max_connections=1,
                                   provider_max_connections=5)
        db.session.add(mismatched)
        db.session.commit()
        details = self._get(mismatched).split('data-section="details"')[1].split('data-section=')[0]
        row = self._srow(details, 'Provider max connections')
        self.assertIn('warn', row)
        self.assertIn('configured: 1', row)

        matched = XtreamAccount(name='Matched', account_type='xtream',
                                base_url='http://x.test', username='u', password='p',
                                status='OK', max_connections=5,
                                provider_max_connections=5)
        db.session.add(matched)
        db.session.commit()
        details = self._get(matched).split('data-section="details"')[1].split('data-section=')[0]
        row = self._srow(details, 'Provider max connections')
        self.assertNotIn('warn', row)

    def test_a_zero_count_is_faint_text_rather_than_a_link_to_an_empty_page(self):
        """17.3: Content's numbers are jump-offs, but a zero has nothing to jump to."""
        acc = _account('Empty', 'UNSYNCED', channel_count=0,
                                epg_entry_count=0)
        db.session.commit()
        content = self._get(acc).split('data-section="content"')[1].split('data-section=')[0]
        self.assertNotIn('sv-link', content)

    def test_content_channels_row_names_how_many_are_hidden(self):
        """dev/docs/DESIGN-channel-hiding.md §11 "Counts": hidden_channel_count is rendered
        beside channel_count everywhere the latter already is, including the Content card's
        Channels row - and only when there is anything to name."""
        acc = _account('SomeHidden', 'OK', channel_count=100, hidden_channel_count=40)
        db.session.commit()
        content = self._get(acc).split('data-section="content"')[1].split('data-section=')[0]
        row = self._srow(content, 'Channels')
        self.assertIn('40', row)
        self.assertIn('hidden', row)

        none_hidden = _account('NoneHidden', 'OK', channel_count=100, hidden_channel_count=0)
        db.session.commit()
        content = self._get(none_hidden).split('data-section="content"')[1].split('data-section=')[0]
        row = self._srow(content, 'Channels')
        self.assertNotIn('hidden', row)

    def test_activity_shows_added_removed_breakdown_with_links(self):
        """dev/changelog/480: a SUCCESS/PARTIAL sync with tracked counts shows "N added,
        M removed" in the Activity card, each a deep link into /channels pre-filtered to
        this account and the matching lifecycle state."""
        acc = _account('Breakdown', 'OK')
        _sync_log(acc, added=3, removed=2)
        db.session.commit()
        html = self._get(acc)
        activity = html.split('data-section="activity"')[1].split('</div>\n\n</div>')[0]
        self.assertIn('3 added', activity)
        self.assertIn('2 removed', activity)
        self.assertIn("f.other=new", activity)
        self.assertIn("f.other=removed", activity)
        self.assertIn(f'f.acct={acc.id}', activity)

    def test_activity_breakdown_zero_counts_are_faint_not_links(self):
        """Same zero-has-nothing-to-jump-to convention as the Content card's counts."""
        acc = _account('BreakdownZero', 'OK')
        _sync_log(acc, added=0, removed=0)
        db.session.commit()
        activity = self._get(acc).split('data-section="activity"')[1].split('</div>\n\n</div>')[0]
        self.assertIn('0 added', activity)
        self.assertIn('0 removed', activity)
        self.assertNotIn('f.other=new', activity)
        self.assertNotIn('f.other=removed', activity)

    def test_activity_omits_breakdown_for_pre_migration_null_rows(self):
        """Product Principle 1: a sync from before this feature shipped must read as
        "not tracked", never render a false "0 added, 0 removed"."""
        acc = _account('BreakdownUntracked', 'OK')
        _sync_log(acc)  # added/removed default to None - the pre-migration shape
        db.session.commit()
        activity = self._get(acc).split('data-section="activity"')[1].split('</div>\n\n</div>')[0]
        self.assertNotIn('added', activity)
        self.assertNotIn('removed', activity)

    def _content(self, account):
        return self._get(account).split('data-section="content"')[1].split('data-section=')[0]

    def _activity(self, account):
        return self._get(account).split('data-section="activity"')[1].split('</div>\n\n</div>')[0]

    def test_content_shows_the_last_finished_syncs_skip_counts(self):
        """dev/changelog/926: the counts come from the newest SUCCESS or PARTIAL sync. A newer
        failed sync records none, so reading it would blank the rows every time a sync fails."""
        acc = _account('Skips', 'OK')
        _sync_log(acc, status='PARTIAL', minutes_ago=120, malformed=1250, duplicate=0)
        _sync_log(acc, status='ERROR', minutes_ago=30, error='provider returned 403')
        db.session.commit()
        content = self._content(acc)
        malformed = self._srow(content, 'Malformed URLs skipped (last sync)')
        self.assertIn('>1,250<', malformed)
        self.assertNotIn('faint', malformed)
        duplicate = self._srow(content, 'Duplicate stream IDs skipped (last sync)')
        self.assertIn('>0<', duplicate)
        self.assertIn('faint', duplicate, 'a zero is faint, the same as every other Content zero')

    def test_content_skip_counts_say_not_tracked_rather_than_zero(self):
        """Product Principle 1: a sync from before the columns, with no alert to recover its
        count from, must never render as a false 0."""
        acc = _account('SkipsUntracked', 'OK')
        _sync_log(acc)
        db.session.commit()
        content = self._content(acc)
        for label in ('Malformed URLs skipped (last sync)',
                      'Duplicate stream IDs skipped (last sync)'):
            row = self._srow(content, label)
            self.assertIn('Not tracked', row, label)
            self.assertNotIn('>0<', row, label)

    def test_content_skip_counts_are_a_dash_when_no_sync_has_finished(self):
        acc = _account('NeverFinished', 'ERROR')
        _sync_log(acc, status='ERROR', error='provider returned 403')
        db.session.commit()
        row = self._srow(self._content(acc), 'Malformed URLs skipped (last sync)')
        self.assertIn('>-<', row)
        self.assertNotIn('Not tracked', row, 'there is no sync to be untracked')

    def test_activity_names_skip_counts_only_when_something_was_skipped(self):
        acc = _account('SkipsActivity', 'OK')
        _sync_log(acc, malformed=1250, duplicate=332)
        db.session.commit()
        activity = self._activity(acc)
        self.assertIn('1,250 skipped as malformed URLs', activity)
        self.assertIn('332 skipped as duplicate stream IDs', activity)

        quiet = _account('SkipsQuiet', 'OK')
        _sync_log(quiet, minutes_ago=90, malformed=0, duplicate=0)
        _sync_log(quiet, minutes_ago=30)
        db.session.commit()
        self.assertNotIn('skipped', self._activity(quiet),
                         'a zero or an untracked sync reads as the plain line')

    def test_sync_history_shows_the_last_ten_and_offers_the_rest(self):
        acc = _account('Busy', 'OK')
        for i in range(14):
            _sync_log(acc, minutes_ago=i * 60)
        db.session.commit()
        html = self._get(acc)
        self.assertIn('All 14 syncs', html)
        # The rows themselves are rendered by account-detail.js from this blob, which is
        # the ONE shape both the first paint and the expand-in-place fetch read.
        blob = re.search(r'\n  logs: (\[.*?\]),\n', html, re.S)
        self.assertIsNotNone(blob, 'the history renderer needs its data blob')
        self.assertEqual(blob.group(1).count('"started_at"'), 10)

    def test_no_expand_control_when_everything_already_fits(self):
        acc = _account('Quiet', 'OK')
        _sync_log(acc)
        db.session.commit()
        self.assertNotIn('acct-hist-more', self._get(acc))

    # ── Secrets ─────────────────────────────────────────────────────────────

    def test_account_urls_are_masked_in_full(self):
        """17.4: for a path-token provider the path IS the credential, and no heuristic
        can tell which providers those are - so the whole path goes."""
        acc = _account('Tokened',
                       m3u_url='http://provider.test/abc123secret/list.m3u',
                       epg_url='http://provider.test/abc123secret/epg.xml')
        db.session.commit()
        html = self._get(acc)
        self.assertNotIn('abc123secret', html)
        self.assertIn('http://provider.test/***', html)

    def test_the_stored_password_never_reaches_the_page_or_the_api(self):
        acc = XtreamAccount(name='Xt', account_type='xtream', base_url='http://x.test',
                            username='someone', password=SECRET_PASSWORD, status='OK')
        db.session.add(acc)
        db.session.commit()
        self.assertNotIn(SECRET_PASSWORD, self._get(acc))
        api = self.client.get(f'/api/accounts/{acc.id}')
        self.assertEqual(api.status_code, 200)
        self.assertNotIn(SECRET_PASSWORD, api.get_data(as_text=True))
        self.assertTrue(api.get_json()['account']['has_password'],
                        'the modal still needs to know a password EXISTS, just not what it is')

    def test_the_password_eyeball_is_local_and_adds_no_reveal_endpoint(self):
        """17.4: this is deliberately narrower than 3.16's config-secret reveal, which
        serves a stored value back. A provider password is not ours to hand out again.

        Both surfaces that render a password field are checked, because the danger is a
        second copy quietly gaining a path: `data-secret-path` is the ONE thing that makes
        util.js fetch a stored value, and there is no endpoint that would answer for an
        account."""
        modal = open(os.path.join(REPO, 'static', 'js', 'account-modal.js')).read()
        # The Add form's eyeball is rendered by the shared macro, so the check has to be on
        # what the macro actually emits - the macro's OTHER mode does add a path.
        form_html = self.client.get('/accounts/new').get_data(as_text=True)
        for name, src in (('account-modal.js', modal), ('/accounts/new', form_html)):
            with self.subTest(source=name):
                self.assertNotIn('data-secret-path=', src)
                self.assertNotIn('/api/settings/reveal', src)
        self.assertIn('secret-reveal-btn', form_html, 'the Add form still offers the eyeball')
        # Both go through the one shared local wirer rather than a hand-rolled copy each.
        self.assertIn('initLocalReveal', modal)
        util = open(os.path.join(REPO, 'static', 'js', 'util.js')).read()
        self.assertIn("input.type = revealed ? 'password' : 'text'", util)

    # ── The action inventory ────────────────────────────────────────────────

    def test_no_action_is_silently_dropped_by_the_redesign(self):
        """17.6, replacing the check dev/mockups/build31.py performs.

        Every mutating thing /accounts could do before the conversion must still have a
        home. Both surfaces now exist - the account page and the converted list page - and
        every action on each goes through the JSON API, so an `accounts.` endpoint still
        being named from the list template is either a new feature that needs a decision or
        a form POST that should have gone with the conversion."""
        known = {
            'new_account': '+ Add Account, the one account action that stays a page (17.1)',
            'account_detail': 'the row itself, and its name, link to the account page',
        }
        src = open(os.path.join(REPO, 'templates', 'accounts.html')).read()
        today = set(re.findall(r"url_for\('accounts\.(\w+)'", src))
        missing = today - set(known)
        self.assertFalse(missing,
                         f'these /accounts actions have no home in the new design: {sorted(missing)}')

    def test_the_retired_routes_are_gone_not_merely_unlinked(self):
        """17.1 retired account_logs and 17.6 retired the form-POST actions; editing became
        a modal. An unlinked route is still a URL, and `edit_account` in particular would
        have served a form whose Save button 405s once its POST half went - so all of them
        are deleted, and this is what says so."""
        from app.routes import accounts as accounts_routes
        for gone in ('account_logs', 'edit_account', 'delete_account', 'sync_account_now',
                     'cancel_sync_account', 'dump_account_data', 'sync_from_dump'):
            with self.subTest(route=gone):
                self.assertFalse(hasattr(accounts_routes, gone),
                                 f'{gone} was retired by section 17 and must not come back')
        # The shared helpers they used to call stay - the JSON routes call the same ones.
        for kept in ('_start_sync', '_cancel_sync', '_delete_account_and_jobs',
                     '_dump_account', '_start_sync_from_dump'):
            self.assertTrue(hasattr(accounts_routes, kept), f'{kept} is still the one implementation')

    def test_the_retired_urls_no_longer_resolve(self):
        acc = _account('Retired', 'OK')
        db.session.commit()
        for path, method in ((f'/accounts/{acc.id}/logs', 'get'),
                             (f'/accounts/{acc.id}/edit', 'get'),
                             (f'/accounts/{acc.id}/delete', 'post')):
            with self.subTest(path=path):
                res = getattr(self.client, method)(path)
                self.assertIn(res.status_code, (404, 405), f'{path} still serves something')


class AccountsListRowTests(unittest.TestCase):
    """The converted /accounts list against section 17.1/17.2/17.5 (dev/changelog/456).

    The row's job is to say which account this is, whether it is healthy and how big it is.
    Each case below is a decision that a careless edit would quietly undo, and the two that
    matter most are subtractions - what came OFF the row when the account page took it over.
    A subtraction has no visible symptom when it silently comes back.
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _rows(self):
        """(whole page, [one row's markup each]).

        Every "must not appear in a row" case below needs the ROW, not the page - a slice
        that ran to the end of the document would find the chrome's own tooltips and pass
        forever. The row div is the only element at two-space indentation inside the list,
        so its own close is where each slice ends."""
        html = self.client.get('/accounts').get_data(as_text=True)
        self.assertIn('acct-rows', html)
        body = html.split('<div class="acct-rows">')[1]
        rows = []
        for part in body.split('<div class="arow ')[1:]:
            end = part.index('\n  </div>')
            rows.append('<div class="arow ' + part[:end])
        return html, rows

    def test_every_row_carries_exactly_one_enumerated_status_class(self):
        """17.2: the edge means sync status, and every status is explicit. A row with two
        of them, or none, is a row whose edge color is whatever the cascade decides."""
        for status, expected in (('OK', 'st-ok'), ('SYNCING', 'st-sync'),
                                 ('ERROR', 'st-bad'), ('UNSYNCED', 'st-none')):
            _account(f'Row {status}', status)
        db.session.commit()
        _, rows = self._rows()
        self.assertEqual(len(rows), 4)
        seen = []
        for row in rows:
            head = row.split('>')[0]
            found = [c for c in ('st-ok', 'st-sync', 'st-bad', 'st-none') if c in head]
            self.assertEqual(len(found), 1, f'exactly one status class per row, got {found}')
            seen.append(found[0])
        self.assertEqual(seen, ['st-ok', 'st-sync', 'st-bad', 'st-none'])

    def test_an_unknown_status_gets_the_neutral_edge_and_prints_its_raw_value(self):
        """No trailing branch quietly renders as healthy: the edge says "nothing known" and
        the badge names the value, so a status nobody enumerated is visible rather than
        disguised as OK (CLAUDE.md, states are enumerated)."""
        _account('Weird', 'TELEPORTING')
        db.session.commit()
        _, rows = self._rows()
        self.assertIn('st-none', rows[0].split('>')[0])
        self.assertNotIn('st-ok', rows[0])
        self.assertIn('TELEPORTING', rows[0])

    def test_the_row_carries_all_five_numbers(self):
        """17.1: channels, in guide, EPG and next sync as labelled cells, last sync on the
        meta line. "Mostly just a list" was about the seven loose buttons and the inline
        sync table, never about dropping the numbers that survived that cut."""
        acc = _account('Numbers', 'OK', channel_count=1234, epg_entry_count=98765,
                       last_sync_at=datetime.utcnow() - timedelta(minutes=90))
        seed.make_channel(account=acc, name='In The Guide', in_guide=True)
        _sync_log(acc, minutes_ago=90)
        db.session.commit()
        _, rows = self._rows()
        row = rows[0]
        for label in ('Channels', 'In guide', 'EPG', 'Next sync'):
            self.assertIn(f'<span class="a-k">{label}</span>', row)
        self.assertEqual(row.count('class="a-num"'), 4)
        self.assertIn('1,234', row)
        self.assertIn('98,765', row)
        self.assertIn('Synced ', row, 'the fifth number is last sync, on the meta line')

    def test_the_channels_number_names_how_many_are_hidden(self):
        """dev/docs/DESIGN-channel-hiding.md §11 "Counts": hidden_channel_count is rendered
        beside channel_count everywhere the latter already is. It goes inside the existing
        Channels cell, not a fifth .a-num - the row already asserts exactly four above.

        And on a line of its own (dev/docs/BUGS.md 2026-09-11 06:39). As a parenthetical it
        shared the number's single nowrap line in a fixed-width column, which clipped it
        mid-number - "57,024 (12,5…" - so it said neither how many nor what they were."""
        _account('Hidden', 'OK', channel_count=1234, hidden_channel_count=9441)
        db.session.commit()
        _, rows = self._rows()
        row = rows[0]
        self.assertEqual(row.count('class="a-num"'), 4)
        cell = row.split('<span class="a-k">Channels</span>')[1].split('class="a-num"')[0]
        self.assertIn('<span class="a-note">9,441 hidden</span>', cell)
        self.assertNotIn('(9,441', row)
        css = open(os.path.join(REPO, 'static', 'css', 'style.css')).read()
        rule = re.search(r'\.arow \.a-num \.a-note \{(.*?)\}', css, re.S)
        self.assertIsNotNone(rule, 'the note needs its own rule')
        self.assertIn('display: block', rule.group(1),
                      'an inline note shares the number\'s one clipped line again')

    def test_no_hidden_note_when_nothing_is_hidden(self):
        """No empty note line under a number when nothing is hidden."""
        _account('NotHidden', 'OK', channel_count=1234, hidden_channel_count=0)
        db.session.commit()
        _, rows = self._rows()
        self.assertNotIn('a-note', rows[0])

    def test_the_header_total_names_how_many_are_hidden(self):
        """The header's channel total is the sum of the column beneath it - the hidden
        total beside it follows the same rule rather than a separate query."""
        _account('A', 'OK', channel_count=100, hidden_channel_count=40)
        _account('B', 'OK', channel_count=50, hidden_channel_count=5)
        db.session.commit()
        html, _ = self._rows()
        head = html.split('class="sub"')[1].split('</span>')[0]
        self.assertIn('150', head)
        self.assertIn('45', head)
        self.assertIn('hidden', head)

    def test_the_sparkline_always_draws_five_bars(self):
        """17.1: the last five syncs. A shorter history pads with the "nothing" bar rather
        than drawing a shorter line - a 2-bar and a 5-bar sparkline side by side read as a
        measurement of something, and it would not be a measurement of anything."""
        busy = _account('Busy', 'OK')
        for i in range(8):
            _sync_log(busy, minutes_ago=i * 60)
        _account('Fresh', 'UNSYNCED')
        db.session.commit()
        _, rows = self._rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            spark = row.split('<span class="spark"')[1].split('</span>')[0]
            self.assertEqual(spark.count('<i '), 5, 'five bars, always')

    def test_badges_carry_no_tooltip_but_the_sparkline_does(self):
        """17.5 item 6, corrected 2026-08-13 (dev/changelog/616). The row is itself the tap
        target that opens the account, and a full 40px badge can't also be a tooltip trigger
        without the two fighting over the same touch - so badges stay tip=false and the
        account page explains them. The sparkline's ~5px bars are the one exception: paired
        with [data-tip], which accounts.js's NO_NAV excludes from row-navigation the same way
        /recordings' NO_NAV (templates/index.html) already does."""
        acc = _account('Tipless', 'ERROR', last_error='provider returned 403',
                       constructed_stream_url_count=9, sync_enabled=False)
        _sync_log(acc, status='ERROR', minutes_ago=30, error='provider returned 403')
        db.session.commit()
        _, rows = self._rows()
        row = rows[0]
        badges = row.split('<span class="a-badges">')[1].split('</span>')[0]
        status_badge = row.split('<span class="a-status">')[1].split('</span>')[0]
        for attr in ('data-tip', 'tip-plain', 'title='):
            self.assertNotIn(attr, badges, f'{attr} must not appear on a badge')
            self.assertNotIn(attr, status_badge, f'{attr} must not appear on the status badge')
        self.assertIn('data-tip', row, 'the sparkline bar must carry a tooltip')

    def test_the_sparkline_tooltip_names_skip_counts_like_the_activity_feed(self):
        """sync_detail promises the tooltip and the account page's Activity line never say
        different things about one sync's numbers (dev/changelog/926)."""
        acc = _account('ListSkips', 'OK')
        _sync_log(acc, malformed=1250, duplicate=332)
        db.session.commit()
        _, rows = self._rows()
        self.assertIn('1,250 skipped as malformed URLs', rows[0])
        self.assertIn('332 skipped as duplicate stream IDs', rows[0])

    def test_the_provider_error_text_is_not_on_the_row(self):
        """17.1: the status badge and the colored edge carry "this is broken"; the text
        that says WHY lives on the account page, which is the page this row opens."""
        acc = _account('Broken', 'ERROR', last_error='provider returned 403 Forbidden')
        _sync_log(acc, status='ERROR', minutes_ago=20, error='provider returned 403 Forbidden')
        db.session.commit()
        html, rows = self._rows()
        self.assertNotIn('403 Forbidden', html)
        self.assertNotIn('Recent syncs', html, 'the inline sync table is retired (17.1)')

    def test_the_kebab_offers_exactly_the_settled_action_list(self):
        """17.5 item 2. Re-normalize URLs is not offered here; everything else the
        old card's seven loose buttons did has a home here or on the account page."""
        _account('Kebab', 'OK')
        db.session.commit()
        _, rows = self._rows()
        row = rows[0]
        self.assertEqual(set(re.findall(r'data-act="([\w-]+)"', row)),
                         {'sync', 'force-epg', 'settings', 'delete'})
        self.assertIn('>Browse channels<', row)
        self.assertNotIn('Re-normalize', row)

    def test_a_syncing_row_offers_cancel_instead_of_sync_never_both(self):
        """One primary at a time: offering Sync now beside Cancel sync asks the user to
        pick between an action and its own undo."""
        _account('Running', 'SYNCING')
        db.session.commit()
        _, rows = self._rows()
        self.assertIn('data-act="cancel-sync"', rows[0])
        self.assertNotIn('data-act="sync"', rows[0])

    def test_the_grid_tracks_are_one_shared_rule_of_fixed_lengths_and_fr(self):
        """CLAUDE.md's list-grid rule (dev/changelog/390): the header and each row are
        SEPARATE grid containers, so a content-sized track sizes itself per row and slides
        every header label off its column. jsdom computes no layout, so this is asserted on
        the stylesheet text - it is the only place the defect is visible without a browser.

        minmax() of a fixed length and a fixed length or fr is allowed (dev/changelog/921):
        both ends resolve against the container's width alone, never a cell's content."""
        css = open(os.path.join(REPO, 'static', 'css', 'style.css')).read()
        rule = re.search(r'\.acct-head, \.acct-rows \.arow \{(.*?)\}', css, re.S)
        self.assertIsNotNone(rule, 'the head and the rows must share ONE track definition')
        track_list = re.search(r'grid-template-columns:([^;]+);', rule.group(1)).group(1)
        tracks = re.findall(r'minmax\([^)]*\)|\S+', track_list)
        self.assertEqual(len(tracks), 7)
        length = r'(?:0|\d+(?:\.\d+)?(?:rem|px|em))'
        flex = r'\d+(?:\.\d+)?fr'
        allowed = rf'^(?:{length}|{flex}|minmax\(\s*{length}\s*,\s*(?:{length}|{flex})\s*\))$'
        for track in tracks:
            self.assertRegex(track, allowed,
                             f'{track} is content-sized - it will desync the header')


class AccountSyncSignatureTests(unittest.TestCase):
    """`accounts.sync_signature()`, the value /accounts compares against to know its rows
    have gone stale (dev/docs/BUGS.md 2026-09-11 06:38). The list was load-once, so a sync
    that finished while it was open read SYNCING until a manual reload. The browser half -
    that a changed signature re-renders the list - is tests/test_accounts_page_js.py; these
    are the server half's promises."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _nav_sig(self):
        return self.client.get('/api/nav-status').get_json()['account_sync']

    def _page_sig(self):
        html = self.client.get('/accounts').get_data(as_text=True)
        return re.search(r'id="acct-live" data-sync-sig="([^"]*)"', html).group(1)

    def test_the_page_and_the_poll_agree_when_nothing_changed(self):
        acc = _account('Quiet', 'OK')
        _sync_log(acc)
        db.session.commit()
        self.assertEqual(self._page_sig(), self._nav_sig())

    def test_the_empty_state_carries_a_signature_too(self):
        """The empty state is inside the live region, so a first account can replace it."""
        self.assertEqual(self._page_sig(), self._nav_sig())

    def test_it_moves_when_a_sync_starts(self):
        acc = _account('Starts', 'OK')
        db.session.commit()
        before = self._page_sig()
        acc.status = 'SYNCING'
        _sync_log(acc, status='IN_PROGRESS', seconds=None)
        db.session.commit()
        self.assertNotEqual(self._nav_sig(), before)

    def test_it_moves_when_a_sync_finishes(self):
        acc = _account('Finishes', 'SYNCING')
        log = _sync_log(acc, status='IN_PROGRESS', seconds=None)
        db.session.commit()
        before = self._page_sig()
        acc.status = 'OK'
        log.status = 'SUCCESS'
        db.session.commit()
        self.assertNotEqual(self._nav_sig(), before)

    def test_it_moves_for_a_sync_that_started_and_failed_between_two_polls(self):
        """Seen from two polls, this sync never happened: the account read ERROR before it
        and reads ERROR after, and it was never SYNCING at a poll. The new sync-log row is
        the only trace, so a signature built from the syncing set alone misses it."""
        acc = _account('Blink', 'ERROR')
        _sync_log(acc, status='ERROR', minutes_ago=90, error='401')
        db.session.commit()
        before = self._page_sig()
        _sync_log(acc, status='ERROR', minutes_ago=0, error='401')
        db.session.commit()
        self.assertNotEqual(self._nav_sig(), before)


class AccountApiTests(unittest.TestCase):
    """The API is the enforcement half. The modal runs the same rules for presentation
    only, so every rule is asserted here against the endpoint."""

    def setUp(self):
        self.t = make_test_app()
        # The API is CSRF-protected app-wide (CLAUDE.md), which a browser satisfies from
        # base.html's meta tag and util.js's fetch wrapper. These cases are about the rules
        # the endpoint enforces, not about the token.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.acc = _account('Api', 'OK')
        db.session.commit()
        self.acc_id = self.acc.id

    def tearDown(self):
        self.t.cleanup()

    def test_save_rejects_an_empty_name(self):
        res = self.client.post(f'/api/accounts/{self.acc_id}',
                               json={'name': '  ', 'account_type': 'm3u',
                                     'm3u_url': 'http://x.test/a.m3u'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Name is required', res.get_json()['error'])

    def test_save_rejects_a_non_numeric_max_connections(self):
        res = self.client.post(f'/api/accounts/{self.acc_id}',
                               json={'name': 'Api', 'account_type': 'm3u',
                                     'm3u_url': 'http://x.test/a.m3u',
                                     'max_connections': 'lots'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('positive whole number', res.get_json()['error'])

    def test_save_accepts_a_json_integer_where_the_form_sends_a_string(self):
        """The form page and the modal go through one validator, so it has to read both
        a werkzeug MultiDict (everything a string) and a JSON body (real ints)."""
        res = self.client.post(f'/api/accounts/{self.acc_id}',
                               json={'name': 'Api', 'account_type': 'm3u',
                                     'm3u_url': 'http://x.test/a.m3u',
                                     'max_connections': 4, 'sync_interval_hours': 2})
        self.assertEqual(res.status_code, 200)
        db.session.expire_all()
        acc = db.session.get(Account, self.acc_id)
        self.assertEqual(acc.max_connections, 4)
        self.assertEqual(acc.sync_interval_hours, 2)

    def test_a_blank_password_keeps_the_stored_one(self):
        acc = XtreamAccount(name='Keep', account_type='xtream', base_url='http://x.test',
                            username='u', password=SECRET_PASSWORD, status='OK')
        db.session.add(acc)
        db.session.commit()
        res = self.client.post(f'/api/accounts/{acc.id}',
                               json={'name': 'Keep', 'account_type': 'xtream',
                                     'base_url': 'http://x.test', 'username': 'u',
                                     'password': ''})
        self.assertEqual(res.status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, acc.id).password, SECRET_PASSWORD)

    def test_blank_interval_and_connections_mean_inherit_not_zero(self):
        self.client.post(f'/api/accounts/{self.acc_id}',
                         json={'name': 'Api', 'account_type': 'm3u',
                               'm3u_url': 'http://x.test/a.m3u',
                               'sync_interval_hours': '', 'max_connections': ''})
        db.session.expire_all()
        acc = db.session.get(Account, self.acc_id)
        self.assertIsNone(acc.sync_interval_hours)
        self.assertIsNone(acc.max_connections)

    def test_sync_on_an_already_syncing_account_is_refused_not_queued(self):
        self.acc.status = 'SYNCING'
        db.session.commit()
        res = self.client.post(f'/api/accounts/{self.acc_id}/sync', json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn('already syncing', res.get_json()['error'])

    def test_missing_account_is_a_404_with_the_standard_envelope(self):
        for path, method in (('/api/accounts/99999', 'get'),
                             ('/api/accounts/99999/sync', 'post'),
                             ('/api/accounts/99999/syncs', 'get')):
            with self.subTest(path=path):
                res = getattr(self.client, method)(path, json={})
                self.assertEqual(res.status_code, 404)
                self.assertIn('error', res.get_json())

    def test_the_debug_only_routes_are_gated_server_side(self):
        """Enforcement lives server-side: the same flag that hides the buttons must also
        refuse the route, or the gate is decoration."""
        for path in (f'/api/accounts/{self.acc_id}/dump',
                     f'/api/accounts/{self.acc_id}/sync-from-dump'):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 404)

    def test_the_syncs_endpoint_returns_every_run_in_the_history_shape(self):
        for i in range(12):
            _sync_log(self.acc, minutes_ago=i * 30)
        db.session.commit()
        res = self.client.get(f'/api/accounts/{self.acc_id}/syncs')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['total'], 12)
        self.assertEqual(len(data['logs']), 12)
        first = data['logs'][0]
        # Exactly the keys the page's own blob carries - one shape, or the first paint and
        # the expand disagree about what a run looked like.
        self.assertEqual(set(first), {'id', 'started_at', 'completed_at', 'status',
                                      'channels', 'epg', 'duration_seconds', 'error'})

    def test_delete_removes_the_account(self):
        res = self.client.delete(f'/api/accounts/{self.acc_id}')
        self.assertEqual(res.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.acc_id))


class CommitRetryTests(unittest.TestCase):
    """CHARACTERIZATION TEST, not a regression guard - it passes against both shapes.

    `new_account` used to carry `@retry_on_locked()` on the whole view, which is the shape
    CLAUDE.md's commit rule warns about; it now retries only the insert tail. That change is
    rule compliance, NOT a bug fix, and this test is what established the difference: a
    whole-view retry duplicates a row only when the function has already committed something
    before the failing commit, and `new_account` has exactly one commit as its last DB
    action, so the retry's rollback discards the pending insert and no duplicate is possible.

    It is kept because it pins the property down the moment a second commit is ever added to
    that view - at which point the whole-view shape WOULD duplicate, and this case starts
    failing."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_a_locked_commit_does_not_create_two_accounts(self):
        real_commit = db.session.commit
        calls = {'n': 0}

        def flaky_commit():
            calls['n'] += 1
            if calls['n'] == 1:
                raise OperationalError('INSERT INTO accounts', {}, Exception('database is locked'))
            return real_commit()

        with mock.patch.object(db.session, 'commit', flaky_commit):
            res = self.client.post('/accounts/new', data={
                'name': 'Retried', 'account_type': 'm3u',
                'm3u_url': 'http://retry.test/list.m3u', 'color': '#58a6ff',
            })
        self.assertEqual(res.status_code, 302)
        self.assertEqual(Account.query.filter_by(name='Retried').count(), 1,
                         'a retried commit must not leave a duplicate account row')


class TimeUntilOverdueTests(unittest.TestCase):
    """DESIGN.md section 5: a scheduled time that has already passed is named as OVERDUE,
    never as "now". Rendering a stale timestamp as "now" is a claim rather than a reading -
    one real account's next sync read "now" indefinitely because nothing had rescheduled
    it (dev/docs/BUGS.md 2026-08-04). The filter is shared, so this is visible on every
    page that uses it."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _filter(self):
        return self.t.app.jinja_env.filters['time_until']

    def test_a_past_time_is_named_as_overdue(self):
        f = self._filter()
        self.assertEqual(f(datetime.utcnow() - timedelta(hours=8)), 'overdue by 8h')
        self.assertEqual(f(datetime.utcnow() - timedelta(minutes=20)), 'overdue by 20m')
        self.assertEqual(f(datetime.utcnow() - timedelta(days=2, hours=3)), 'overdue by 2d 3h')

    def test_the_first_minute_past_is_just_overdue(self):
        """"overdue by 3s" reads as a fault when it is the clock ticking past the mark."""
        self.assertEqual(self._filter()(datetime.utcnow() - timedelta(seconds=3)), 'overdue')

    def test_no_past_time_renders_as_now(self):
        f = self._filter()
        for delta in (timedelta(seconds=1), timedelta(hours=5), timedelta(days=30)):
            self.assertNotEqual(f(datetime.utcnow() - delta), 'now')

    def test_a_future_time_is_unchanged(self):
        f = self._filter()
        self.assertTrue(f(datetime.utcnow() + timedelta(hours=3)).startswith('in '))
        self.assertEqual(f(None), '?')


if __name__ == '__main__':
    unittest.main()
