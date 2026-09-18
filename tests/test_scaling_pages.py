"""Page-level scaling guards for the per-row-I/O defect class (BUGS.md 2026-07-15 10:34
`/guide` 11s, BUGS.md 2026-07-20 01:28 `/` 3s - the class nicknamed "whack-a-mole").

Wall-clock "does 100 rows take ~10x as long as 10" tests are flaky on a loaded machine, so
these count the two I/O currencies that actually caused both incidents, deterministically:
  * SQL statements executed during the request (catches N+1 ORM query patterns), and
  * real config.yaml disk parses via the app.config._parse_config_file seam (catches
    per-row load_config(), whether from route code or Jinja template filters).
Both counts must be identical for a small vs. a large seed - a page whose I/O scales with
row count fails with the ratio in the message.

Complements tests/test_scaling.py (guide API endpoints, load_config *call* count) and
tests/test_config_cache.py (the load_config mtime cache itself).

When adding a new list/grid page, add a case here: a `_seed_<page>(n)` function plus a
`test_<page>_*` method pairing it with the page's path. `PageCaseCoverageTests` at the
bottom is what makes that mandatory rather than aspirational - a new page route with
neither a case nor an allowlist entry fails there.
"""
import contextlib
import os
import sys
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
import app.channel_search_rows as rows_mod  # noqa: E402
import app.scheduler as scheduler_mod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import unfolded_query  # noqa: E402
from app import db  # noqa: E402
from app.database import (Account, AccountSyncLog, Alert, Channel, ChannelEvent,  # noqa: E402
                          ChannelGroup, ChannelHideRule, HealthCheckProfile,
                          IgnoredAlertPattern, OnDemandTestJob, Recording, RecordingEvent,
                          RecordingProfile, RecordingSegment, Tag, TagPattern,
                          CHANNEL_ADDED_TO_GUIDE, HIDE_TARGET_NAME_GLOB,
                          RECORDING_HANDOFF, REC_STATUS_SCHEDULED)


def _seed_recordings(n):
    base = datetime.utcnow() + timedelta(days=1)
    for i in range(n):
        seed.make_recording(status='COMPLETED', name=f'rec_{i}', with_segment=True,
                            start_time=base + timedelta(hours=i),
                            stop_time=base + timedelta(hours=i, minutes=30))
    db.session.commit()


def _seed_groups(n):
    """n channel-kind groups (2 members each) - exercises the Groups tab (groups
    unification 4/4, changelog/238), which batches ChannelGroupMember/OnDemandTestJob
    lookups across all groups instead of the per-group `g.memberships`/`g.test_jobs`
    lazy-relationship access that would otherwise be an N+1 per row."""
    acc = seed.make_account()
    for i in range(n):
        ch1 = seed.make_channel(acc, name=f'Group {i} Member A')
        ch2 = seed.make_channel(acc, name=f'Group {i} Member B')
        seed.make_group(name=f'Group {i}', members=[ch1, ch2])
    db.session.commit()


def _seed_pairs(n):
    """n LINKED PAIRS - a channel-kind group with a health check attached to it, which is
    the Linked pairs section of the Groups tab (changelog/323). Seeded separately from
    _seed_groups because a pair takes the merged-row path (per-check status chip, results
    span, per-check kebab entries) that an unpaired channel group never reaches."""
    acc = seed.make_account()
    for i in range(n):
        ch1 = seed.make_channel(acc, name=f'Pair {i} Member A')
        ch2 = seed.make_channel(acc, name=f'Pair {i} Member B')
        grp = seed.make_group(name=f'Pair {i}', members=[ch1, ch2])
        db.session.add(OnDemandTestJob(name=f'Pair {i} check', group_id=grp.id,
                                       status='COMPLETED'))
    db.session.commit()


def _seed_group_members(n):
    """ONE channel-kind group with n members, each carrying a test result - the unified
    group/health-check detail page (changelog/273). Its per-member work (latest test,
    disabled reason, format outlier, monitored-by-a-check, duplicate title) and its
    Activity timeline must all be batched, not queried per member."""
    acc = seed.make_account()
    channels = [seed.make_channel(acc, name=f'Member {i}') for i in range(n)]
    for ch in channels:
        seed.make_channel_test(ch, all_null=False, status='COMPLETED',
                               resolution='1920x1080', fps=60.0, bitrate_kbps=4200.0)
    seed.make_group(name='Scaling Group', members=channels)
    db.session.commit()


def _seed_check_members(n):
    """The same page entered by its health check instead of its group, with n channels
    under test - the facet that also renders Status/Frames/Drops/Screenshot columns and
    the per-run result tally."""
    acc = seed.make_account()
    channels = [seed.make_channel(acc, name=f'Checked {i}') for i in range(n)]
    job = seed.make_test_job(name='Scaling Check', channels=channels, status='COMPLETED')
    for ch in channels:
        seed.make_channel_test(ch, all_null=False, status='COMPLETED', job_id=job.id,
                               resolution='1280x720', fps=30.0, bitrate_kbps=2100.0)
    db.session.commit()


def _seed_channel_history(n):
    """ONE channel carrying n tests, n scored recordings and n channel events - the three
    row-scaling lists on the revamped channel detail page (dev/changelog/348): Test
    History, Recording Observations and the Activity Timeline. The timeline builder loads
    every test/recording/event for the channel regardless of the page size, so a lazy
    relationship touched while rendering a row would be an N+1 here even though each list
    is paginated."""
    acc = seed.make_account()
    ch = seed.make_channel(acc, name='Scaling Channel', in_guide=True)
    base = datetime.utcnow() - timedelta(days=2)
    for i in range(n):
        seed.make_channel_test(ch, all_null=False, status='COMPLETED',
                               resolution='1920x1080', fps=60.0, bitrate_kbps=4200.0,
                               drop_count=0, quality_score=90)
        seed.make_recording(status='COMPLETED', name=f'ch_rec_{i}', channel_id=ch.id,
                            start_time=base + timedelta(hours=i),
                            stop_time=base + timedelta(hours=i, minutes=30),
                            completed_at=base + timedelta(hours=i, minutes=30),
                            health_quality_score=88)
        db.session.add(ChannelEvent(channel_id=ch.id, event_type=CHANNEL_ADDED_TO_GUIDE,
                                    detail=f'seed event {i}'))
    db.session.commit()


def _scaling_channel_id():
    return Channel.query.filter_by(name='Scaling Channel').one().id


def _scaling_group_id():
    return ChannelGroup.query.filter_by(name='Scaling Group').one().id


def _scaling_job_id():
    return OnDemandTestJob.query.filter_by(name='Scaling Check').one().id


def _seed_converting(n):
    """n CONVERTING recordings, each carrying a progress snapshot - guards the dashboard
    bg-task line's _converting_detail(), which must read only the row's own fields and add
    no per-row query (conversion-monitor feature, changelog 276)."""
    base = datetime.utcnow() + timedelta(days=1)
    for i in range(n):
        seed.make_recording(status='CONVERTING', name=f'conv_{i}',
                            start_time=base + timedelta(hours=i),
                            stop_time=base + timedelta(hours=i, minutes=30),
                            conversion_progress_pct=42.0, conversion_out_size=1234567,
                            conversion_eta_seconds=90, conversion_attempts=1,
                            conversion_updated_at=datetime.utcnow())
    db.session.commit()


def _seed_alerts(n):
    """n unread alerts - the Alert center renders one row each, and every row runs
    the `local_time` template filter, which is the exact shape that made `/` take
    3 seconds (BUGS.md 2026-07-20 01:28). Added with the page's conversion
    (dev/changelog/446)."""
    base = datetime.utcnow() - timedelta(hours=1)
    for i in range(n):
        db.session.add(Alert(alert_type='SYNC_FAILED', severity='ERROR',
                             title=f'Sync failed for account {i}',
                             body='provider returned 502\nretrying in 15 minutes',
                             source=f'account:{i}:sync',
                             created_at=base + timedelta(seconds=i)))
    db.session.commit()


def _seed_guide_channels(n):
    acc = seed.make_account()
    for i in range(n):
        ch = seed.make_channel(acc, name=f'Channel {i}', in_guide=True)
        db.session.add(seed.EPGEntry(
            channel_id=ch.id, title='Show',
            start_time=datetime.utcnow(),
            stop_time=datetime.utcnow() + timedelta(hours=1)))
    db.session.commit()


def _seed_readiness_shape(n):
    """n accounts, n guide groups with a recording-enabled member each, and n guide channels
    with a program - the three things the Readiness check counts over.

    Its checks look at accounts, at guide groups' memberships, at which recording members
    have ever been health checked, and at the EPG. Every one of those is a place a per-row
    query would go unnoticed, because the card renders a fixed eleven capability rows
    whatever the numbers behind them are (dev/changelog/950).
    """
    for i in range(n):
        acc = seed.make_account(name=f'Provider {i}')
        member = seed.make_channel(acc, name=f'Member {i}')
        grp = seed.make_group(name=f'Guide group {i}', members=[member])
        grp.in_guide = True
        for m in grp.memberships:
            m.recording_enabled = True
        ch = seed.make_channel(acc, name=f'Guide channel {i}', in_guide=True)
        db.session.add(seed.EPGEntry(
            channel_id=ch.id, title='Show',
            start_time=datetime.utcnow(),
            stop_time=datetime.utcnow() + timedelta(hours=1)))
    db.session.commit()


def _seed_search_channels(n):
    """n channels with a program each, plus the three things the channel search's row
    payload enriches beyond the channel row itself (dev/changelog/398): a duplicate cluster,
    a group membership and a tag that matches by pattern.

    The cluster and the group are a FIXED size at both seed sizes on purpose - they exist so
    those enrichment queries run at all, and anything that grows with n would be measuring
    the seed rather than the endpoint. Every enrichment must be one query for the whole page:
    the search re-runs on every keystroke, so a per-row lookup here is a page-size worth of
    queries per character typed."""
    acc = seed.make_account(name='Scaling Account')
    now = datetime.utcnow()
    channels = []
    for i in range(n):
        ch = seed.make_channel(acc, name=f'Channel {i}', category_name='Sports',
                               health_score=50.0 + (i % 40), last_seen_at=now)
        db.session.add(seed.EPGEntry(
            channel_id=ch.id, title='Wembley Show', sub_title='Part One',
            description='Liverpool play at Wembley',
            start_time=now - timedelta(minutes=5),
            stop_time=now + timedelta(hours=1)))
        channels.append(ch)
    for ch in channels[:3]:
        ch.stream_url = 'http://example.test/live/shared/cluster/1'
        ch.is_duplicate_stream_url = True
    seed.make_group(name='Scaling Search Group', members=channels[:2])
    tag = Tag(name='scaling-tag')
    db.session.add(tag)
    db.session.flush()
    db.session.add(TagPattern(tag_id=tag.id, pattern='Wembley'))
    db.session.commit()


def _seed_search_groups(n):
    """n channel GROUPS, each with two members - the row kind dev/changelog/811 added.

    The groups are what scales here, which is the whole point: a group row carries a member
    count, a recording-enabled count, a health score, the member it would record from and
    what that member is airing, and `DESIGN-group-search-rows.md` §5.2's fourth rule is that
    NONE of those may be resolved per row. Resolved per row they are five lookups per group
    per keystroke, which is exactly the defect class this file exists for.

    Two members each rather than one, so `pick_best_member` actually has a choice to rank
    and the format lock has something to filter - a one-member group would let a per-group
    ranking query hide behind a trivial answer.

    THE NAMES INTERLEAVE THE TWO KINDS ON PURPOSE. The default sort is the name, so naming
    every group after every channel would put page 1 entirely in one kind at the large seed
    and both kinds at the small one - and the query counts would then differ because the
    page held different ROW KINDS, which is a property of the seed rather than of the
    endpoint. `Item 007 Feed 0/1` sorts immediately before `Item 007 Group` at every size.
    """
    acc = seed.make_account(name='Scaling Group Account')
    now = datetime.utcnow()
    for i in range(n):
        members = []
        for j in range(2):
            ch = seed.make_channel(acc, name=f'Item {i:03d} Feed {j}', category_name='Sports',
                                   health_score=50.0 + j, last_seen_at=now)
            db.session.add(seed.EPGEntry(
                channel_id=ch.id, title='Wembley Show', sub_title='Part One',
                description='Liverpool play at Wembley',
                start_time=now - timedelta(minutes=5),
                stop_time=now + timedelta(hours=1)))
            members.append(ch)
        seed.make_group(name=f'Item {i:03d} Group', members=members)
    db.session.commit()


def _seed_tags(n):
    """n tags, each carrying two match patterns - the Tags list (dev/changelog/448). The
    patterns cell renders per row off `tag.patterns`, which is a lazy relationship: without
    the route's selectinload that is one extra SELECT per tag. The `usage` column is the
    other per-row hazard here and must stay one config read plus one profiles query, however
    many tags exist."""
    for i in range(n):
        tag = Tag(name=f'scaling-tag-{i}', color='#58a6ff')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern=f'MARKER{i}'))
        db.session.add(TagPattern(tag_id=tag.id, pattern=f'marker-alt-{i}'))
    db.session.commit()


def _seed_hide_rules(n):
    """n global name-pattern rules - the Hide Rules page (dev/changelog/780). The page's
    own row rendering is client-side (hide-rules.js, from the embedded rules_json), so what
    can scale here is the route's own query count: the rules list (one .all()) plus the
    three Channel aggregate COUNTs it renders into the summary tiles, none of which grow
    with the number of rules."""
    for i in range(n):
        db.session.add(ChannelHideRule(target=HIDE_TARGET_NAME_GLOB, pattern=f'SCALE{i}*',
                                       account_id=None, enabled=True))
    db.session.commit()


def _seed_accounts(n):
    """n accounts, each with five sync-log rows - the /accounts list (dev/changelog/455).

    The list page fetched the last five sync runs with a query PER ACCOUNT, which is a real
    N+1 and the identical defect the Tags page had (dev/docs/BUGS.md 2026-08-04 @ 05:58:52
    AM ET). Five each rather than one because the fix is a windowed ranking query: a single
    log per account would pass just as well against a `LIMIT 5` in a loop."""
    for i in range(n):
        acc = seed.make_account(name=f'Scaling Account {i}')
        for run in range(5):
            db.session.add(AccountSyncLog(
                account_id=acc.id,
                started_at=datetime.utcnow() - timedelta(hours=run + 1),
                completed_at=datetime.utcnow() - timedelta(hours=run + 1) + timedelta(seconds=20),
                status='SUCCESS', channels_synced=10, epg_entries_synced=100))
    db.session.commit()


def _seed_account_history(n):
    """One account with n sync runs and n channels - the account page (dev/changelog/455).

    The page shows only the last ten runs, so what this actually guards is everything
    AROUND that cap: the counts, the recordings tally and the effective-settings resolution
    must each stay one query and one config read however much history the account has."""
    acc = (Account.query.filter_by(name='Scaling Account Detail').first()
           or seed.make_account(name='Scaling Account Detail'))
    for i in range(n):
        seed.make_channel(acc, name=f'Scaling Channel {i}', in_guide=(i % 2 == 0))
        db.session.add(AccountSyncLog(
            account_id=acc.id,
            started_at=datetime.utcnow() - timedelta(minutes=i + 1),
            completed_at=datetime.utcnow() - timedelta(minutes=i + 1) + timedelta(seconds=5),
            status='SUCCESS', channels_synced=i, epg_entries_synced=i * 10))
    db.session.commit()


def _scaling_account_id():
    return Account.query.filter_by(name='Scaling Account Detail').one().id


def _seed_ignored_patterns(n):
    """n alert suppression rules - the Ignored alerts page renders one row each, and every
    row runs the `local_time` filter over `created_at`, which is the template-filter shape
    that made `/` take 3 seconds (dev/docs/BUGS.md 2026-07-20 01:28)."""
    base = datetime.utcnow() - timedelta(days=1)
    for i in range(n):
        db.session.add(IgnoredAlertPattern(
            alert_type='SYNC_FAILED', title_pattern=f'Sync failed for account # ({i})',
            example_title=f'Sync failed for account 4 ({i})',
            created_at=base + timedelta(seconds=i)))
    db.session.commit()


def _seed_recording_profiles(n):
    """n recording profiles, half of them actually used - the Profiles list renders a
    Recordings and a Channels usage count per row, and both must stay one grouped query for
    the whole page rather than a count per profile."""
    acc = seed.make_account(name='Profile Scaling Account')
    base = datetime.utcnow() - timedelta(days=1)
    for i in range(n):
        p = RecordingProfile(name=f'Scaling Profile {i}', pre_padding_minutes=i % 5)
        db.session.add(p)
        db.session.flush()
        seed.make_channel(acc, name=f'Profile Channel {i}', default_profile_id=p.id)
        seed.make_recording(status='COMPLETED', name=f'profile_rec_{i}', profile_id=p.id,
                            started_at=base + timedelta(minutes=i),
                            start_time=base + timedelta(minutes=i),
                            stop_time=base + timedelta(minutes=i + 30))
    db.session.commit()


def _seed_health_check_profiles(n):
    """n health-check profiles, each attached to a check - same shape as the recording
    profiles above: the "used by N health checks" column is one grouped query for the page,
    and the inherited-default cells resolve from one config read however many rows there
    are."""
    for i in range(n):
        p = HealthCheckProfile(name=f'Scaling Check Profile {i}',
                               test_duration_seconds=30 + i)
        db.session.add(p)
        db.session.flush()
        db.session.add(OnDemandTestJob(name=f'Check {i}', profile_id=p.id,
                                       status='COMPLETED'))
    db.session.commit()


def _seed_recording_history(n):
    """ONE recording carrying n segments and n events - the recording detail page's two
    row-scaling tables (Segments, Event log). Neither is paginated, so a lazy relationship
    touched while rendering a row is an N+1 straight away: the Segments table renders each
    segment's channel and account, which `_segment_channels()` batch-fetches with a
    joinedload precisely so the Channel column costs nothing per row."""
    acc = seed.make_account(name='Segment Scaling Account')
    base = datetime.utcnow() - timedelta(hours=4)
    rec = seed.make_recording(status='COMPLETED', name='Scaling Recording',
                              start_time=base, stop_time=base + timedelta(hours=2),
                              completed_at=base + timedelta(hours=2))
    for i in range(n):
        ch = seed.make_channel(acc, name=f'Segment Channel {i}')
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=i + 1, channel_id=ch.id,
            file_path=f'/tmp/scaling_{rec.id}_seg_{i:03d}.ts',
            started_at=base + timedelta(minutes=i),
            ended_at=base + timedelta(minutes=i + 1),
            exit_reason='STALL_DETECTED', bytes_recorded=1024 * (i + 1)))
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type=RECORDING_HANDOFF,
            detail=f'failover to member {i}', segment_number=i + 1))
    db.session.commit()


def _scaling_recording_id():
    return Recording.query.filter_by(name='Scaling Recording').one().id


def _seed_scheduled_recordings(n):
    """n SCHEDULED recordings, each with the start/stop APScheduler pair the real scheduler
    creates - the Scheduled Jobs page's largest row family.

    Deliberately NOT seeding accounts: `_job_duration_info()` runs one estimate query per
    account sync job, which scales with account count rather than row count, and a real
    install has a handful of accounts against potentially hundreds of scheduled recordings.
    The recording rows are the ones that actually grow, and they are what this measures.
    """
    base = datetime.utcnow() + timedelta(days=1)
    for i in range(n):
        seed.make_recording(status=REC_STATUS_SCHEDULED, name=f'sched_{i}',
                            start_time=base + timedelta(hours=i),
                            stop_time=base + timedelta(hours=i, minutes=30))
    db.session.commit()


class _FakeJob:
    """The three attributes `_build_job_list()` reads off an APScheduler job for a
    recording row. A real BackgroundScheduler is not used here because its jobstore writes
    to disk and its next_run_time depends on wall clock - neither of which the per-row DB
    lookups under test care about."""

    def __init__(self, job_id, next_run_time):
        self.id = job_id
        self.next_run_time = next_run_time
        self.trigger = types.SimpleNamespace()


def _fake_scheduler_for_scheduled_recordings():
    jobs = []
    for rec in Recording.query.filter_by(status=REC_STATUS_SCHEDULED).all():
        jobs.append(_FakeJob(f'start_{rec.id}', rec.start_time))
        jobs.append(_FakeJob(f'stop_{rec.id}', rec.stop_time))
    return types.SimpleNamespace(get_jobs=lambda: jobs)


def _seed_search_channels_indexed(n):
    """The same rows with the FTS indexes built, so the endpoint is measured on the indexed
    path too - the engine answers the same question three interchangeable ways and only one
    of them is exercised by the LIKE-fallback case above."""
    from app.search_index import rebuild_search_indexes
    _seed_search_channels(n)
    rebuild_search_indexes('scaling test')


class PageScalingTests(unittest.TestCase):

    SMALL, LARGE = 8, 80

    def _measure(self, seed_fn, n_rows, path, prepare=None):
        """`path` is either a literal URL or a callable run after seeding, for pages whose
        URL carries the seeded row's own id. Hard-coding an id here is a trap: the app
        auto-creates the "TV Guide Channels" system group at startup, so it - not the
        seeded group - owns /channel-groups/1, and the page then renders identically at
        both sizes and the guard passes without measuring anything.

        `prepare` is an optional zero-argument callable returning a context manager, run
        after seeding and entered around the measured request only - for a page whose rows
        come from somewhere other than the database (the scheduler's job list). Building
        that stand-in queries the seeded rows, so it has to happen before the counter opens
        or the setup lands in the count it is setting up for.
        """
        t = make_test_app()
        try:
            t.sandbox_output_dirs()
            seed_fn(n_rows)
            url = path() if callable(path) else path
            ctx = prepare() if prepare is not None else None
            # Reset the config cache so each measurement pays exactly one warm-up parse;
            # a per-row parse path would then show up as a count difference.
            config_mod._yaml_cache = None
            with ctx or contextlib.nullcontext():
                with IOCounter(all_engines()) as counter:
                    resp = t.client.get(url)
            self.assertEqual(resp.status_code, 200,
                             f'{url} returned {resp.status_code}, not 200')
            counter.url = url
            return counter
        finally:
            t.cleanup()

    def _assert_row_independent(self, seed_fn, path, prepare=None):
        small = self._measure(seed_fn, self.SMALL, path, prepare)
        large = self._measure(seed_fn, self.LARGE, path, prepare)
        path = small.url
        self.assertEqual(
            small.queries, large.queries,
            f'{path}: SQL query count scales with row count ({small.queries} at '
            f'{self.SMALL} rows vs {large.queries} at {self.LARGE} rows) - an N+1 query '
            f'or per-row DB lookup crept into the page.')
        self.assertEqual(
            small.config_parses, large.config_parses,
            f'{path}: config.yaml parse count scales with row count '
            f'({small.config_parses} at {self.SMALL} rows vs {large.config_parses} at '
            f'{self.LARGE} rows) - a per-row code path is re-reading config from disk.')

    def test_index_page(self):
        self._assert_row_independent(_seed_recordings, '/recordings')

    def test_guide_page(self):
        self._assert_row_independent(_seed_guide_channels, '/guide')

    def test_channels_hub_page(self):
        # The hub renders no result rows of its own any more - the search is
        # /api/channels/search, covered by its own cases below. What still scales here is
        # the guide-scoped chrome the shell paints before the first fetch answers (the
        # duplicate review set and the missing-channel count), which is what this seeds.
        self._assert_row_independent(_seed_guide_channels, '/channels')

    def test_readiness_report(self):
        # Not a page: the Readiness card lives on Maintenance, whose contents all arrive by
        # fetch, so this endpoint is where the per-account and per-group work actually
        # happens and where the guard belongs.
        self._assert_row_independent(_seed_readiness_shape, '/api/readiness')

    def test_groups_page(self):
        self._assert_row_independent(_seed_groups, '/channel-groups')

    def test_groups_page_with_attached_checks(self):
        self._assert_row_independent(_seed_pairs, '/channel-groups')

    def test_dashboard_page(self):
        self._assert_row_independent(_seed_converting, '/')

    def test_alerts_page(self):
        self._assert_row_independent(_seed_alerts, '/alerts')

    def test_ignored_alerts_page(self):
        self._assert_row_independent(_seed_ignored_patterns, '/alerts/ignored')

    def test_tags_page(self):
        self._assert_row_independent(_seed_tags, '/tags')

    def test_hide_rules_page(self):
        self._assert_row_independent(_seed_hide_rules, '/channels/hide-rules')

    def test_profiles_page(self):
        self._assert_row_independent(_seed_recording_profiles, '/profiles')

    def test_health_check_profiles_page(self):
        self._assert_row_independent(_seed_health_check_profiles, '/health-check-profiles')

    def test_recording_detail_page(self):
        self._assert_row_independent(_seed_recording_history,
                                     lambda: f'/recordings/{_scaling_recording_id()}')

    def test_jobs_page(self):
        """The Scheduled Jobs page's rows come from the scheduler, not from a query, so the
        row count is the number of APScheduler jobs - but each recording row resolves its
        Recording (name, status) and each sync row its Account, which is where the per-row
        DB lookup lives (dev/changelog/730, dev/docs/BUGS.md 2026-08-18)."""
        self._assert_row_independent(
            _seed_scheduled_recordings, '/jobs',
            prepare=lambda: mock.patch.object(
                scheduler_mod, 'get_scheduler',
                return_value=_fake_scheduler_for_scheduled_recordings()))

    def test_accounts_list_page(self):
        self._assert_row_independent(_seed_accounts, '/accounts')

    def test_account_detail_page(self):
        self._assert_row_independent(_seed_account_history,
                                     lambda: f'/accounts/{_scaling_account_id()}')

    def test_group_detail_page(self):
        self._assert_row_independent(_seed_group_members, lambda: f'/channel-groups/{_scaling_group_id()}')

    def test_health_check_detail_page(self):
        self._assert_row_independent(_seed_check_members, lambda: f'/channels/health-checks/{_scaling_job_id()}')

    def test_channel_detail_page(self):
        # Every list unpaginated, so the rows themselves scale - the default page sizes
        # would cap the rendered rows and hide a per-row lookup in the table loops.
        self._assert_row_independent(
            _seed_channel_history,
            lambda: f'/channels/{_scaling_channel_id()}?per_page=all&timeline_per_page=all')

    def test_channel_search_api(self):
        self._assert_row_independent(_seed_search_channels, '/api/channels/search')

    def test_channel_search_api_with_a_query(self):
        # A typed query adds the two enrichments that only exist while there is text to
        # match - the per-field "why" matrix and the program behind an EPG hit - and both
        # are shaped as one query for the page rather than one per row.
        self._assert_row_independent(
            _seed_search_channels,
            '/api/channels/search?q=wembley&in=name&in=epg-title&in=epg-desc')

    def test_channel_search_api_with_a_query_on_the_index(self):
        self._assert_row_independent(
            _seed_search_channels_indexed,
            '/api/channels/search?q=wembley&in=name&in=epg-title&in=epg-desc')

    def test_channel_search_api_rows_only(self):
        # `facets=` is the fetch the page makes on every keystroke; it must not quietly
        # pick up per-row work that the facet-bearing fetch was hiding.
        self._assert_row_independent(_seed_search_channels,
                                     '/api/channels/search?facets=')

    def test_channel_search_api_with_group_rows(self):
        """A page of GROUP rows (dev/changelog/811). Every one of them says how many members
        it has, how many are recording-enabled, its health, which member it would record
        from and what that member is airing - and `DESIGN-group-search-rows.md` §5.2's
        fourth rule is that all of it is batch-fetched. This is the case that rule asks for.
        """
        self._assert_row_independent(_seed_search_groups, '/api/channels/search')

    def test_channel_search_api_with_group_rows_unfolded(self):
        """The same page with the members put back as their own rows, which is the shape
        that carries BOTH row kinds at once - the group enrichments and the channel ones on
        one page, each batched over its own kind."""
        self._assert_row_independent(
            _seed_search_groups,
            '/api/channels/search?' + unfolded_query())

    def test_airing_search_api_relabelled_as_groups(self):
        """The airing grain with "Collapse channel groups" on, over a scaling number of
        groups: the surviving row is relabelled as the group it stands for, which needs the
        winners of the ranking window and then those groups' own payloads. Both are one
        query for the page, not one per row."""
        self._assert_row_independent(_seed_search_groups,
                                     '/api/channels/search?grain=airings')

    def test_airing_search_api(self):
        """The airing grain (dev/changelog/412) is a second row-scaling surface on the same
        endpoint, and it enriches MORE per row than the channel grain does - the channel's
        own values, plus a recording match and a rendered filename per showing. Every one of
        those has to be one query for the page."""
        self._assert_row_independent(_seed_search_channels,
                                     '/api/channels/search?grain=airings')

    def test_airing_search_api_with_a_query(self):
        self._assert_row_independent(
            _seed_search_channels,
            '/api/channels/search?grain=airings&q=wembley&in=name&in=epg-title&in=epg-desc')

    def test_airing_search_api_on_the_index(self):
        """With the index usable the planner probes before it commits. The probe is memoized
        on SearchContext, so it must not become one query per facet dimension - and it must
        certainly not become one per row."""
        self._assert_row_independent(
            _seed_search_channels_indexed,
            '/api/channels/search?grain=airings&q=wembley&in=name&in=epg-title&in=epg-desc')

    def test_airing_search_api_rows_only(self):
        self._assert_row_independent(_seed_search_channels,
                                     '/api/channels/search?grain=airings&facets=')

    def test_airing_search_api_with_tag_cleanup_configured(self):
        """The cases above render every filename with EMPTY cleanup lists, which is the one
        configuration where render_filename_template asks the database for nothing. With a
        cleanup list set it looks tags up by name, and without a prefetched map that is one
        Tag query per showing.

        This is not a hypothetical configuration: the filename designer
        (dev/changelog/441) is what makes setting those lists a one-modal change, so the
        surface that made this reachable is the reason this case exists. Patched rather
        than driven off config.yaml, because the route resolves its config at runtime and
        a test that edited the real file to exercise this would be writing into
        production - which is exactly how this was found.
        """
        with mock.patch.object(rows_mod, '_tag_cleanup',
                               return_value=[('live', 'remove'), ('new', 'replace')]):
            self._assert_row_independent(_seed_search_channels,
                                         '/api/channels/search?grain=airings&facets=')

    def test_guide_epg_api(self):
        """_entries_in_window in app/routes/guide.py used to run once per channel - one
        windowed EPGEntry query for the whole page now (dev/changelog/567,
        BUGS.md 2026-08-11)."""
        now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        start = now.strftime('%Y-%m-%dT%H:%M:%S')
        end = (now + timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%S')
        self._assert_row_independent(
            _seed_guide_channels, f'/api/guide/epg?start={start}&end={end}')

    def test_channel_search_catalog_api(self):
        self._assert_row_independent(_seed_search_channels,
                                     '/api/channels/search/catalog')

    def test_group_detail_rows_api(self):
        self._assert_row_independent(
            _seed_group_members, lambda: f'/api/channel-groups/{_scaling_group_id()}/detail-rows')


#: Page endpoints that have a case above, mapped to the test method that measures them.
#: An entry here is a claim that the page's I/O has been proven row-independent; the value
#: is what a reader follows to see what was actually seeded.
COVERED = {
    'dashboard.dashboard': 'test_dashboard_page',
    'recordings.index': 'test_index_page',
    'recordings.recording_detail': 'test_recording_detail_page',
    'guide.guide': 'test_guide_page',
    'channels.channel_browser': 'test_channels_hub_page',
    'channels.channel_detail': 'test_channel_detail_page',
    'channels.health_check_detail': 'test_health_check_detail_page',
    'channel_groups.groups_page': 'test_groups_page',
    'channel_groups.group_detail': 'test_group_detail_page',
    'alerts.alert_center': 'test_alerts_page',
    'alerts.ignored_alerts': 'test_ignored_alerts_page',
    'tags.tags_list': 'test_tags_page',
    'channel_hide_rules.hide_rules_page': 'test_hide_rules_page',
    'accounts.accounts_list': 'test_accounts_list_page',
    'accounts.account_detail': 'test_account_detail_page',
    'profiles.profiles_list': 'test_profiles_page',
    'health_check_profiles.health_check_profiles_list': 'test_health_check_profiles_page',
    'jobs.jobs_page': 'test_jobs_page',
}

#: Page endpoints that cannot scale with row count, and the checkable reason why. A reason
#: beginning with `_REDIRECT` is verified rather than trusted - see
#: `test_every_redirect_reason_still_redirects`.
_REDIRECT = 'Redirect: '

NOT_ROW_SCALING = {
    'channel_tests.channel_tests': _REDIRECT + 'old URL, now the Channels hub.',
    'channel_tests.guide_channel_tests': _REDIRECT + 'old URL, now the guide system check.',
    'channel_tests.on_demand_job_detail': _REDIRECT + 'old URL, now the check detail page.',
    'channels.channels_health': _REDIRECT + 'old URL, now the guide system check.',
    'channels.channels_health_checks': _REDIRECT + 'old URL, now the Groups tab.',
    'channels.channels_health_checks_guide': _REDIRECT + 'old URL, now the guide system check.',
    'channels.channels_test_runs': _REDIRECT + 'old URL, now the Groups tab.',
    'channels.test_run_detail': _REDIRECT + 'old URL, now the check detail page.',
    'guide.channel_browser': _REDIRECT + 'old URL, now the Channels hub.',
    'guide.epg_status': _REDIRECT + 'old URL, the EPG Browser is retired (changelog/631).',
    'recordings.new_recording': _REDIRECT + 'manual scheduling is a modal on the TV Guide.',

    'accounts.new_account': (
        'A blank account form. Renders no collection at all - the only variable-length thing '
        'on it is the fixed PRESET_COLORS/NORM_MODES vocabulary, which is a module constant.'),
    'auth.login': (
        'The login form. Renders no collection, and reaches the database for nothing - the '
        'gate reads app.config["AUTH"] and the in-memory lockout table.'),
    'logs.logs_page': (
        'Page chrome and the configured log path only; it renders no log lines. The lines '
        'arrive from /api/logs/history, which is a file tail hard-capped at 5000 lines and '
        'touches no database row.'),
    'system.maintenance': (
        'Seven cards whose contents all arrive by fetch. The only server-rendered values are '
        'the backup schedule and the Docker flag, which are config, not measurement. The '
        'Readiness card is the one whose payload counts rows, and /api/readiness carries its '
        'own case above (test_readiness_report).'),
    'settings.settings': (
        'Renders config.yaml, whose size is bounded by the key set in app/config.py. Nothing '
        'on it grows with a database table.'),
    'settings.notifications_settings': (
        'Renders one card per configured notification service - a config dict the user edits '
        'by hand, not a table that grows with recordings, channels or alerts.'),

    'channel_tests.serve_screenshot': (
        'Serves one screenshot file from disk. Not a page and renders no rows.'),
    'recordings.live_thumbnail': (
        'Serves one JPEG for one recording. Not a page and renders no rows.'),
}


class PageCaseCoverageTests(unittest.TestCase):
    """Every page route is either measured above or has a stated reason it cannot scale.

    This is the half that keeps the rule alive. CLAUDE.md has required a case here for every
    row-scaling page since the second of the five per-row-I/O incidents, but the case list
    was maintained by hand, so a page shipped without one was uncovered silently and forever
    - which is how `/jobs`, `/profiles`, `/health-check-profiles`, `/alerts/ignored` and
    `/recordings/<id>` all ended up with no guard. Same shape as
    test_global_state_isolation.py::ProcessGlobalCoverageTests: a new route with no decision
    is a red test now, rather than a page nobody notices is unmeasured.
    """

    @staticmethod
    def _page_endpoints(app):
        """Every GET rule that serves a user-facing page.

        `/api/*` is excluded because a JSON endpoint is not a page - the two search
        endpoints that DO scale are measured above by URL, deliberately, since their row
        payload is the thing at risk. `/mockups/*` is excluded because it is a dev-only
        static file server gated behind flask.serve_mockups (dev/docs/DESIGN.md 11.5) and is
        not registered at all unless that key is on.
        """
        out = {}
        for rule in app.url_map.iter_rules():
            if 'GET' not in rule.methods:
                continue
            if rule.endpoint == 'static' or rule.rule.startswith(('/api/', '/mockups/')):
                continue
            out[rule.endpoint] = rule.rule
        return out

    def test_every_page_route_is_measured_or_has_a_reason(self):
        t = make_test_app()
        try:
            endpoints = self._page_endpoints(t.app)
        finally:
            t.cleanup()
        undecided = sorted(
            f'{ep} ({path})' for ep, path in endpoints.items()
            if ep not in COVERED and ep not in NOT_ROW_SCALING)
        self.assertEqual(
            undecided, [],
            'page routes with no row-scaling decision: ' + str(undecided) + '. Either add a '
            'case to PageScalingTests and name it in COVERED, or add a NOT_ROW_SCALING entry '
            'saying in one checkable sentence why the page cannot grow with row count. '
            'Leaving it undecided is how five separate per-row-I/O incidents reached '
            'production.')

    def test_neither_list_names_a_route_that_no_longer_exists(self):
        """A stale entry reads as coverage this file is not actually providing."""
        t = make_test_app()
        try:
            endpoints = self._page_endpoints(t.app)
        finally:
            t.cleanup()
        stale = sorted(set(COVERED) | set(NOT_ROW_SCALING))
        stale = [ep for ep in stale if ep not in endpoints]
        self.assertEqual(stale, [], f'COVERED/NOT_ROW_SCALING name dead endpoints: {stale}')

    def test_every_covered_route_names_a_test_that_exists(self):
        missing = sorted(f'{ep} -> {name}' for ep, name in COVERED.items()
                         if not hasattr(PageScalingTests, name))
        self.assertEqual(missing, [],
                         f'COVERED points at test methods that do not exist: {missing}')

    def test_the_two_lists_do_not_overlap(self):
        both = sorted(set(COVERED) & set(NOT_ROW_SCALING))
        self.assertEqual(both, [], f'endpoints claimed as both measured and exempt: {both}')

    def test_every_redirect_reason_still_redirects(self):
        """The allowlist's largest family says "this page is only a redirect". That is a
        claim about live behavior, so check it rather than trust the prose - a redirect
        route quietly given a real template would otherwise keep its exemption."""
        t = make_test_app()
        try:
            endpoints = self._page_endpoints(t.app)
            not_redirecting = []
            for ep, reason in sorted(NOT_ROW_SCALING.items()):
                if not reason.startswith(_REDIRECT):
                    continue
                path = endpoints[ep].replace('<int:job_id>', '1') \
                                    .replace('<int:recording_id>', '1')
                resp = t.client.get(path)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    not_redirecting.append(f'{ep} ({path}) returned {resp.status_code}')
        finally:
            t.cleanup()
        self.assertEqual(
            not_redirecting, [],
            'NOT_ROW_SCALING claims these are redirects, but they are not: '
            f'{not_redirecting}. If one now renders a page, it needs a real case.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
