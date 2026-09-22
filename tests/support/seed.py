"""Row factories for Tier 2 tests - seed a temp DB with the pathological rows the
route smoke sweep and API tests need.

Each factory adds to db.session and flushes (so .id is available) but does NOT commit -
tests own their commit. `seed_all()` is the convenience path: it builds one of
everything (a Recording in every status, an all-NULL ChannelTest, a group + members, a
guide channel with EPG) and returns a Seeded namespace of the created objects/ids.

Everything here writes only to the throwaway DB created by make_test_app().
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app import db
from app.database import (
    M3uAccount, Channel, ChannelGroup, ChannelGroupMember, ChannelTest,
    Recording, RecordingEvent, RecordingSegment, EPGEntry, OnDemandTestJob,
    RECORDING_ABORTED,
)

# Every Recording.status the state machine can persist (see Recording model comment).
RECORDING_STATUSES = [
    'SCHEDULED', 'IN_PROGRESS', 'PAUSED', 'CONCATENATING', 'ANALYZING',
    'COMPLETED', 'FAILED', 'ABORTED',
]

_UTC_NOW = datetime.utcnow


def make_account(name='Test M3U', **kw):
    acc = M3uAccount(name=name, m3u_url='http://example.test/list.m3u',
                     epg_url='http://example.test/epg.xml', status='OK', **kw)
    db.session.add(acc)
    db.session.flush()
    return acc


def make_channel(account, stream_id=None, name='Test Channel', in_guide=False, **kw):
    if stream_id is None:
        # Unique within the account (uq_channel_account_stream) - count existing + 1.
        stream_id = Channel.query.filter_by(account_id=account.id).count() + 1
    # A default rather than a fixture constant: "which EPG id does this channel carry" is
    # the subject of a filter, a column and a warning banner, so a caller has to be able to
    # seed two members sharing one id, or a member carrying none at all.
    kw.setdefault('epg_channel_id', f'ch{stream_id}.test')
    ch = Channel(
        account_id=account.id, stream_id=stream_id, name=name,
        stream_url=f'http://example.test/live/{stream_id}',
        raw_stream_url=f'http://example.test/live/{stream_id}.ts',
        in_guide=in_guide, guide_sort_order=stream_id if in_guide else 0,
        **kw)
    db.session.add(ch)
    db.session.flush()
    return ch


def make_group(name='Test Group', members=(), in_guide=True, disabled=None,
               test_disabled=None, recording=True, **kw):
    """A ChannelGroup plus ChannelGroupMember rows for `members` (position = list order).

    `recording` is the DEFAULT for every member's recording_enabled and is True here on
    purpose, unlike the production column, whose default is False: almost every test
    that builds a group is testing a group that records, and spelling that out at ~90
    call sites would say nothing. Pass recording=False for a group that has not been
    promoted yet.

    `disabled` is an iterable of channel ids whose Recording switch starts off;
    `test_disabled` the same for the Health check switch.

    Every group carries exactly one OnDemandTestJob (`grp.check`), minted here the way
    `channel_groups.build_group_with_members()` mints it - QUEUED, no schedule - because
    the unique index on `on_demand_test_jobs.group_id` makes a second one impossible and
    the pages assume the one is there (dev/changelog/1077). `job` is a dict of column
    overrides for that check (`status`, `recurring`, `recur_paused`, ...); `job_name`
    names it when the default `<name> - health check` is not what a test asserts on.
    """
    job = kw.pop('job', None) or {}
    job_name = kw.pop('job_name', None)
    grp = ChannelGroup(name=name, in_guide=in_guide, guide_sort_order=1, **kw)
    db.session.add(grp)
    db.session.flush()
    disabled = set(disabled or ())
    test_disabled = set(test_disabled or ())
    for pos, ch in enumerate(members):
        db.session.add(ChannelGroupMember(
            group_id=grp.id, channel_id=ch.id, position=pos,
            recording_enabled=recording and ch.id not in disabled,
            test_enabled=ch.id not in test_disabled))
    job_kw = {'name': job_name or f'{name} - health check', 'status': 'QUEUED'}
    job_kw.update(job)
    db.session.add(OnDemandTestJob(group_id=grp.id, **job_kw))
    db.session.flush()
    return grp


def set_check(group, **fields):
    """Reshape `group`'s one health check in place - status, schedule, profile, name -
    and return it. The replacement for adding a second OnDemandTestJob to a group a test
    already built, which the unique index refuses (dev/changelog/1077)."""
    job = group.check
    for k, v in fields.items():
        setattr(job, k, v)
    db.session.flush()
    return job


def make_test_job(name='Job', channels=(), disabled=(), status='QUEUED', **kw):
    """The one OnDemandTestJob of a fresh group holding `channels`, Recording off on every
    member (the group is a health check until someone records from it). `disabled` is an
    iterable of channel ids whose Health check switch starts off; `kw` are column
    overrides on the job. The job is named `name`, exactly as the group is - a test that
    asserts on the name gets the string it passed."""
    grp = make_group(name=name, members=channels, in_guide=False,
                     recording=False, test_disabled=disabled,
                     job_name=name, job=dict(status=status, **kw))
    return grp.check


def make_recording(status='SCHEDULED', name=None, channel_id=None, group_id=None,
                   url=None, start_time=None, stop_time=None,
                   with_events=False, with_segment=False, **kw):
    now = _UTC_NOW()
    start = start_time if start_time is not None else now - timedelta(hours=1)
    stop = stop_time if stop_time is not None else now + timedelta(hours=1)
    rec = Recording(
        name=name or f'rec_{status.lower()}',
        url=url or 'http://example.test/live/1',
        start_time=start, stop_time=stop,
        scheduled_start_time=start, scheduled_stop_time=stop,
        status=status, channel_id=channel_id, group_id=group_id, **kw)
    db.session.add(rec)
    db.session.flush()
    if with_events:
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type=RECORDING_ABORTED, detail='seed event'))
    if with_segment:
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0,
            file_path=f'/tmp/seed_{rec.id}_seg_000.ts', started_at=start,
            ended_at=stop, exit_reason='STOP_TIME_REACHED', bytes_recorded=1024))
    db.session.flush()
    return rec


def make_segment(recording, channel, started_at, ended_at, segment_number=0, **kw):
    """A RecordingSegment captured on `channel` (None for a pre-column row). ended_at=None
    is a segment still capturing."""
    seg = RecordingSegment(
        recording_id=recording.id, channel_id=channel.id if channel is not None else None,
        segment_number=segment_number,
        file_path=f'/tmp/seed_{recording.id}_seg_{segment_number:03d}.ts',
        started_at=started_at, ended_at=ended_at, bytes_recorded=1024, **kw)
    db.session.add(seg)
    db.session.flush()
    return seg


def make_channel_test(channel, all_null=True, status='FAILED', **kw):
    """A ChannelTest row. all_null=True leaves every nullable column NULL - the
    pathological row that previously 500'd detail/health rendering.

    status is a named parameter, not part of **kw, so callers can seed COMPLETED and
    CANCELLED rows without colliding with the default.

    test_ended_at is the one nullable column all_null does NOT leave empty: a NULL there
    is not a pathological value, it means the test is running right now, and every tally
    treats such a row as no result yet (app/routes/channel_tests.py::_tally). Pass
    test_ended_at=None explicitly to seed that row."""
    kw.setdefault('test_started_at', _UTC_NOW())
    kw.setdefault('test_ended_at', _UTC_NOW())
    ct = ChannelTest(
        channel_id=channel.id,
        status=status,
        **kw)
    db.session.add(ct)
    db.session.flush()
    return ct


def make_epg_entry(channel, title='Test Program', offset_minutes=0, duration_minutes=60,
                   sub_title=None, description=None, category=None, rating=None,
                   start_time=None):
    """One showing. `sub_title`, `description`, `category` and `rating` default to NULL
    deliberately - that is the shape most provider rows have, and it is what the airing
    search's `-word` handling has to survive (tests/test_airing_search_negation.py).

    `start_time` pins the air time outright, for a caller that has to land a row on an
    exact minute rather than an offset from now - the record-start metadata refresh finds
    a program by channel plus exact start_time (dev/changelog/1055)."""
    start = (start_time if start_time is not None
             else _UTC_NOW() + timedelta(minutes=offset_minutes))
    entry = EPGEntry(
        channel_id=channel.id, title=title, sub_title=sub_title, description=description,
        category=category, rating=rating,
        start_time=start, stop_time=start + timedelta(minutes=duration_minutes))
    db.session.add(entry)
    db.session.flush()
    return entry


def seed_all():
    """Build one-of-everything and commit. Returns a Seeded namespace.

    Covers: a Recording in every status; an all-NULL ChannelTest; a group with two
    members; a standalone guide channel with an EPG program.
    """
    acc = make_account()

    guide_ch = make_channel(acc, name='Guide Channel', in_guide=True)
    make_epg_entry(guide_ch)

    m1 = make_channel(acc, name='Group Member A')
    m2 = make_channel(acc, name='Group Member B')
    group = make_group(name='Seed Group', members=[m1, m2])

    null_test = make_channel_test(guide_ch, all_null=True)

    recordings = {
        status: make_recording(
            status=status, channel_id=guide_ch.id,
            with_events=True, with_segment=(status != 'SCHEDULED'))
        for status in RECORDING_STATUSES
    }
    # One group-backed recording so group_id routes have a row too.
    group_rec = make_recording(status='COMPLETED', name='group_rec', group_id=group.id)

    db.session.commit()

    return SimpleNamespace(
        account=acc,
        guide_channel=guide_ch,
        group=group,
        group_members=[m1, m2],
        null_test=null_test,
        recordings=recordings,
        group_recording=group_rec,
    )
