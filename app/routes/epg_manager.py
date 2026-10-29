"""EPG Manager: the home of the EPG tools that work across every account's guide, starting
with the similarity scan (app/epg_similarity.py, dev/changelog/1325) - which EPG ids carry
the same schedule, and how good each copy is. Read-only: nothing here writes a key, an
override, an alert or an event; a change a person decides on is made where it always
was (the channel page's Set key and Guide source controls).

The page is server-rendered and paged, a fixed number of queries whatever the guide's
size: the clusters to show are chosen in SQL, and only that page's schedules, channels
and links are loaded.
"""
import json
import threading
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for
from sqlalchemy import func, or_

from .. import db
from ..database import (Channel, ChannelGroup, ChannelGroupMember, EpgScan, EpgScanLink,
                        EpgScanSchedule, EpgScanScheduleChannel, EpgSource, EPG_SCAN_FAILED,
                        EPG_SCAN_RUNNING, EPG_SCAN_SUCCESS)
from ..epg_similarity import latest_result, latest_scan, start_scan
from ..epg_sources import (CMP_DESCRIPTIONS, CMP_IDENTICAL, CMP_MOSTLY, CMP_NO_OVERLAP,
                           CMP_SHIFTED, CMP_UNRELATED)
from ..fmt_utils import counted
from ..tz_utils import format_local, get_display_tz, is_24h

epg_manager_bp = Blueprint('epg_manager', __name__)

PER_PAGE = 40
#: Channel names listed per schedule before "and N more".
NAMES_SHOWN = 4

# What a cluster is: every copy identical in every field, or the same programs at the
# same times with some field differing. A schedule in no cluster can still be listed
# for its links. These are the page's kind filter, and its tab names.
KIND_IDENTICAL = 'identical'
KIND_DETAILS = 'details'
KIND_RELATED = 'related'
KINDS = (KIND_IDENTICAL, KIND_DETAILS, KIND_RELATED)

# The compare-sources page's verdict words and badges (templates/channels/guide_compare.html),
# for the links; the vocabulary is epg_sources.CMP_*.
VERDICTS = {
    CMP_IDENTICAL: ('b-done', 'Identical where both list'),
    CMP_DESCRIPTIONS: ('b-done', 'Identical, descriptions differ'),
    CMP_MOSTLY: ('b-sched', 'Mostly the same'),
    CMP_SHIFTED: ('b-warn', 'Shifted'),
    CMP_UNRELATED: ('b-fail', 'Unrelated'),
    CMP_NO_OVERLAP: ('b-abort', 'Nothing to compare'),
}

# The facets a copy can differ from its cluster's lead in, each a hash column of
# EpgScanSchedule, in the order they are named.
FACETS = (('h_titles', 'titles'), ('h_stops', 'end times'), ('h_subtitles', 'subtitles'),
          ('h_descriptions', 'descriptions'), ('h_extra', 'categories or ratings'))


def _scan_dict(scan):
    if scan is None:
        return None
    return {
        'id': scan.id, 'status': scan.status,
        'started_at': format_local(scan.started_at, 'short_datetime', none_value=None),
        'finished_at': format_local(scan.finished_at, 'short_datetime', none_value=None),
        'listings_as_of': format_local(scan.listings_as_of, 'short_datetime', none_value=None),
        'elapsed_ms': scan.elapsed_ms, 'listings': scan.listings, 'schedules': scan.schedules,
        'clusters': scan.clusters, 'identical': scan.identical, 'links': scan.links,
        'error': scan.error,
    }


@epg_manager_bp.route('/api/epg-manager/scan')
def scan_status_api():
    """The latest scan of any status, for the page's and the Maintenance card's poll."""
    return jsonify({'success': True, 'scan': _scan_dict(latest_scan())})


@epg_manager_bp.route('/api/epg-manager/scan', methods=['POST'])
def start_scan_api():
    """Start a similarity scan. 409 with the blocker named when admission refuses it, or
    when one is already running - a refusal is never silent."""
    running = EpgScan.query.filter_by(status=EPG_SCAN_RUNNING).first()
    if running is not None:
        return jsonify({'error': 'A scan is already running.'}), 409
    scan, reason = start_scan(current_app._get_current_object())
    if scan is None:
        return jsonify({'error': f'{reason[0].upper()}{reason[1:]}, so the scan did not start. '
                                 'Try again when it has finished.'}), 409
    return jsonify({'success': True, 'scan': _scan_dict(scan)}), 202


def _cluster_query(scan_id, kind, source_id, q, group_id):
    """The cluster ids to list, with their schedule counts, as a subquery: clusters of
    two or more, or any cluster with a link, narrowed by the filters. Every filter is a
    property some schedule in the cluster has."""
    S = EpgScanSchedule
    base = db.session.query(S.cluster.label('cluster'), func.count(S.id).label('n'),
                            func.count(func.distinct(S.h_exact)).label('variants'),
                            func.max(S.channel_count).label('top')).filter(S.scan_id == scan_id)
    # Two IN lists rather than a join on `a_id = id OR b_id = id`: SQLite cannot drive the
    # OR off an index, and the join form took 28 s over prod's 10,763 links where this
    # takes 20 ms (dev/changelog/1325).
    linked = db.session.query(S.cluster).filter(
        S.scan_id == scan_id,
        or_(S.id.in_(db.session.query(EpgScanLink.a_id).filter(EpgScanLink.scan_id == scan_id)),
            S.id.in_(db.session.query(EpgScanLink.b_id).filter(EpgScanLink.scan_id == scan_id))))
    if source_id is not None:
        base = base.filter(S.cluster.in_(
            db.session.query(S.cluster).filter(S.scan_id == scan_id, S.source_id == source_id)))
    if group_id is not None:
        member_ids = db.session.query(ChannelGroupMember.channel_id).filter(
            ChannelGroupMember.group_id == group_id)
        base = base.filter(S.cluster.in_(
            db.session.query(S.cluster).join(
                EpgScanScheduleChannel, EpgScanScheduleChannel.schedule_id == S.id)
            .filter(S.scan_id == scan_id, EpgScanScheduleChannel.channel_id.in_(member_ids))))
    if q:
        like = f'%{q}%'
        by_name = (db.session.query(EpgScanScheduleChannel.schedule_id)
                   .join(Channel, Channel.id == EpgScanScheduleChannel.channel_id)
                   .filter(EpgScanScheduleChannel.scan_id == scan_id, Channel.name.ilike(like)))
        base = base.filter(S.cluster.in_(
            db.session.query(S.cluster).filter(
                S.scan_id == scan_id, or_(S.epg_id.ilike(like), S.id.in_(by_name)))))
    grouped = base.group_by(S.cluster)
    if kind == KIND_IDENTICAL:
        grouped = grouped.having(func.count(S.id) > 1).having(func.count(func.distinct(S.h_exact)) == 1)
    elif kind == KIND_DETAILS:
        grouped = grouped.having(func.count(S.id) > 1).having(func.count(func.distinct(S.h_exact)) > 1)
    elif kind == KIND_RELATED:
        grouped = grouped.having(S.cluster.in_(linked))
    else:
        grouped = grouped.having(or_(func.count(S.id) > 1, S.cluster.in_(linked)))
    return grouped


def _schedule_view(s, source_names, channels, tz, h24):
    names = [c for c in channels.get(s.id, [])]
    return {
        'id': s.id, 'source_id': s.source_id,
        'source_name': source_names.get(s.source_id, 'No source') if s.source_id else 'No source',
        'epg_id': s.epg_id, 'spellings': [x for x in json.loads(s.spellings or '[]') if x != s.epg_id],
        'active': s.active, 'channel_count': s.channel_count,
        'channels': names[:NAMES_SHOWN], 'more_channels': max(s.channel_count - NAMES_SHOWN, 0),
        'listings': s.listings,
        'until': format_local(s.until, 'monthday_time', tz=tz, h24=h24) if s.until else None,
        'described': s.described, 'desc_chars': s.desc_chars, 'subtitled': s.subtitled,
        'marked': s.marked, 'row': s,
    }


def _lead_key(s):
    """The copy a cluster is shown against: the guide for a channel first, then the
    one with the most description text, listings and channels."""
    return (not s.active, -s.desc_chars, -s.listings, -s.channel_count, s.epg_id.lower())


@epg_manager_bp.route('/epg-manager')
def epg_manager():
    scan = latest_scan()
    result = latest_result()
    kind = request.args.get('kind') or ''
    if kind not in KINDS:
        kind = ''
    source_id = request.args.get('source', type=int)
    group_id = request.args.get('group_id', type=int)
    q = (request.args.get('q') or '').strip()[:100]
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    group = db.session.get(ChannelGroup, group_id) if group_id else None
    if group_id and group is None:
        abort(404)
    sources = EpgSource.query.order_by(EpgSource.name).all()
    source_names = {s.id: s.name for s in sources}
    tz, h24 = get_display_tz(), is_24h()

    clusters, total, pages = [], 0, 1
    counts = {}
    if result is not None:
        sub = _cluster_query(result.id, kind, source_id, q, group_id).subquery()
        total = db.session.query(func.count()).select_from(sub).scalar() or 0
        pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
        page = min(page, pages)
        chosen = (db.session.query(sub.c.cluster)
                  .order_by(sub.c.n.desc(), sub.c.top.desc(), sub.c.cluster)
                  .offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())
        cluster_ids = [c for (c,) in chosen]
        # The tab counts, each the same query under its own kind - four fixed queries.
        for k in KINDS + ('',):
            counts[k] = (total if k == kind else db.session.query(func.count()).select_from(
                _cluster_query(result.id, k, source_id, q, group_id).subquery()).scalar() or 0)

        S = EpgScanSchedule
        rows = (S.query.filter(S.scan_id == result.id, S.cluster.in_(cluster_ids)).all()
                if cluster_ids else [])
        ids = [s.id for s in rows]
        link_rows = (EpgScanLink.query.filter(
            EpgScanLink.scan_id == result.id,
            or_(EpgScanLink.a_id.in_(ids), EpgScanLink.b_id.in_(ids))).all() if ids else [])
        other_ids = {x.a_id for x in link_rows} | {x.b_id for x in link_rows}
        other_ids -= set(ids)
        others = S.query.filter(S.id.in_(list(other_ids))).all() if other_ids else []
        by_id = {s.id: s for s in rows + others}
        channels: dict = {}
        all_ids = list(by_id)
        if all_ids:
            for sched_id, name in (db.session.query(EpgScanScheduleChannel.schedule_id, Channel.name)
                                   .join(Channel, Channel.id == EpgScanScheduleChannel.channel_id)
                                   .filter(EpgScanScheduleChannel.schedule_id.in_(all_ids))
                                   .order_by(Channel.name)):
                channels.setdefault(sched_id, []).append(name)
        views = {sid: _schedule_view(s, source_names, channels, tz, h24) for sid, s in by_id.items()}

        links_of: dict = {}
        for x in link_rows:
            for me, other, only_me, only_other in ((x.a_id, x.b_id, x.only_a, x.only_b),
                                                   (x.b_id, x.a_id, x.only_b, x.only_a)):
                if me in by_id and by_id[me].cluster in cluster_ids:
                    cls, label = VERDICTS[x.verdict]
                    links_of.setdefault(by_id[me].cluster, []).append({
                        'from': views[me], 'to': views[other], 'verdict': x.verdict,
                        'badge': cls, 'label': label, 'compared': x.compared, 'same': x.same,
                        'moved': x.moved, 'different': x.different, 'only_here': only_me,
                        'only_there': only_other, 'description_gaps': x.description_gaps,
                        'largest_move_min': x.largest_move_min,
                        'summary': _link_summary(x, only_me, only_other)})

        by_cluster: dict = {}
        for s in rows:
            by_cluster.setdefault(s.cluster, []).append(s)
        for cid in cluster_ids:
            members = sorted(by_cluster.get(cid, []), key=_lead_key)
            if not members:
                continue
            lead = members[0]
            copies = []
            for s in members:
                v = views[s.id]
                v['is_lead'] = s.id == lead.id
                v['same_as_lead'] = s.h_exact == lead.h_exact
                v['differs_in'] = [label for col, label in FACETS
                                   if getattr(s, col) != getattr(lead, col)]
                copies.append(v)
            identical = all(s.h_exact == lead.h_exact for s in members)
            clusters.append({
                'id': cid, 'n': len(members), 'identical': identical,
                'kind_label': ('Identical in every way' if identical and len(members) > 1
                               else 'Same programs, details differ' if len(members) > 1
                               else 'One copy'),
                'copies': copies,
                'links': sorted(links_of.get(cid, []),
                                key=lambda x: (list(VERDICTS).index(x['verdict']), x['to']['epg_id'].lower())),
            })

    def qs(**over):
        args = {'kind': kind or None, 'source': source_id, 'group_id': group_id, 'q': q or None,
                'page': None}
        args.update(over)
        return url_for('epg_manager.epg_manager', **{k: v for k, v in args.items() if v})

    return render_template(
        'epg_manager.html', scan=scan, scan_json=_scan_dict(scan), result=result,
        result_json=_scan_dict(result), kind=kind, kinds=KINDS, counts=counts,
        source_id=source_id, sources=sources, group=group, q=q, clusters=clusters,
        page=page, pages=pages, total=total, per_page=PER_PAGE, qs=qs,
        running=scan is not None and scan.status == EPG_SCAN_RUNNING,
        failed=scan is not None and scan.status == EPG_SCAN_FAILED,
        scan_when=format_local(result.finished_at, 'day_datetime') if result else None,
        listings_as_of=format_local(result.listings_as_of, 'day_datetime') if result else None,
        counted=counted)


def _link_summary(x, only_me, only_other):
    """The compare-sources page's sentence for a verdict (routes/channels.py), in the
    scan's two-sided wording."""
    if x.verdict == CMP_IDENTICAL:
        return f'All {counted(x.compared, "program")} match where both list.'
    if x.verdict == CMP_DESCRIPTIONS:
        return (f'All {counted(x.compared, "program")} match, but only one side describes '
                f'{counted(x.description_gaps, "of them", "of them")}.')
    parts = []
    if x.moved:
        parts.append(f'{counted(x.moved, "program")} moved'
                     + (f' (up to {x.largest_move_min} min)' if x.largest_move_min else ''))
    if x.different:
        parts.append(f'{x.different} different')
    if only_me:
        parts.append(f'{only_me} only here')
    if only_other:
        parts.append(f'{only_other} only there')
    return (f'{x.same + x.moved:,} of {counted(x.compared, "program")} match'
            + (': ' + ', '.join(parts) if parts else '') + '.')


# Referenced so a template can name the kind and status words from the one declaration.
__all__ = ['epg_manager_bp', 'EPG_SCAN_SUCCESS', 'threading', 'datetime']
