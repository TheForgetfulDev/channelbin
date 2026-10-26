"""What went wrong with a recording, in plain words - the one place that says it.

Three surfaces tell the same story and used to word it three ways, or not at all: the
Recordings list's health tooltip, the RECORDING_RETRYING alert, and the recording page,
whose only plain account was that hover tooltip on another page (dev/changelog/1270). The
clauses here are shared so a fact reads the same wherever it is shown.

Pure functions over a loaded Recording and the events and stderr tails its page already
holds - no query, no disk. A caller inside a row loop hands in what it has.

Fault is named only where the app actually knows it. A provider's placeholder clip, an HTTP
refusal and a refused login are the provider's doing; a connection that could not be made
is the network or the provider's server and nothing here can tell which; a stream that
simply stopped sending says nothing about who stopped it, and the account says so rather
than guessing (CLAUDE.md principle 1).
"""
import json
import re

from . import fmt_utils
from .database import (
    GROUP_FAILOVER, REC_STATUS_COMPLETED, RECORDING_LOGIN_REFUSED, RECORDING_RETRY_SCHEDULED,
    RECORDING_STOPPED_EARLY,
)
from .proc_utils import stderr_cause

#: Below this share of its window a finished recording is partial. Every clean capture is a
#: few seconds off its window (ffmpeg start latency, a connect-time buffer), so the floor is
#: what keeps "partial" off every row; the list's duration cell and the guide's mark share it.
PARTIAL_SHARE = 0.98

# ffmpeg names the specific HTTP answer before the generic one that ends the run ("HTTP
# error 405 Method Not Allowed" before "Server returned 4XX Client Error, but not one of
# 40{0,1,3,4}"), so the specific spellings are tried first and the generic one is only
# reduced to its class.
_HTTP_SPECIFIC = re.compile(r'(?:HTTP error|Server returned) (\d{3})((?: [A-Za-z][A-Za-z\' -]*)?)')
_HTTP_GENERIC = re.compile(r'Server returned (\d)XX', re.IGNORECASE)
_UNREACHABLE = re.compile(
    r'Connection refused|Connection timed out|Connection reset by peer|No route to host'
    r'|Network is unreachable|Name or service not known|Temporary failure in name resolution'
    r'|Failed to resolve hostname[^\r\n]*', re.IGNORECASE)


def short_capture(rec):
    """(captured, scheduled) seconds when a finished recording holds less than
    PARTIAL_SHARE of its scheduled window, else None."""
    captured = rec.actual_duration_seconds
    scheduled = rec.scheduled_duration_seconds
    if captured and scheduled and captured < scheduled * PARTIAL_SHARE:
        return captured, scheduled
    return None


def gap_clause(rec):
    """The capture-gap sentence when the gap clears PARTIAL_SHARE's floor, else ''.

    Downtime is said with it when it is the larger: the two are shown side by side on the
    recording page, and "2m 3s ... nothing capturing at all" beside a Downtime of 2m 19s read
    as one number disagreeing with itself (dev/changelog/1287). Capture gaps is mostly inside
    Downtime, so "of that" holds whenever Downtime is the larger; when it is not (time spent
    paused counts as a gap only), the gap is said alone."""
    window = rec.duration_seconds
    gap = rec.capture_gap_seconds
    if gap and window and gap >= window * (1 - PARTIAL_SHARE):
        gap_str = fmt_utils.fmt_duration(gap, with_seconds=True)
        down = int(rec.total_downtime_seconds or 0)
        if down > int(gap):
            return (f'Nothing reached the file for '
                    f'{fmt_utils.fmt_duration(down, with_seconds=True)}, and for {gap_str} of '
                    f'that no capture was running at all.')
        return f'{gap_str} of the recording window with nothing capturing at all.'
    return ''


def _trouble_clauses(rec):
    """(sentence, lost) pairs for trouble_clauses(). `lost` is False for the two that say the
    file holds MORE than real time: those are worth saying, but nothing went missing, so they
    are not a reason to ask who was at fault (dev/changelog/1287)."""
    out = []
    gap = gap_clause(rec)
    if gap:
        out.append((gap, True))
    window = rec.duration_seconds
    vs_capture = rec.content_vs_capture_seconds
    if vs_capture and window and abs(vs_capture) >= window * (1 - PARTIAL_SHARE):
        amount = fmt_utils.fmt_duration(abs(vs_capture), with_seconds=True)
        # "Holds more content than the time the capture ran" was a sentence no first-time
        # user could explain. The cause is said only as far as the length shows it: video
        # ahead of the clock is video the provider already had, and nothing here compares a
        # frame, so a repeat is a possibility, never a claim (Recording.content_vs_capture_seconds).
        if vs_capture > 0:
            out.append((f'The file is {amount} longer than the time ChannelBin spent '
                        f'capturing. The provider sent that video faster than real time, so it '
                        f'already had it - usually video it replays each time ChannelBin '
                        f'connects - and some of it may repeat.', False))
        else:
            out.append((f'The file is {amount} shorter than the time ChannelBin spent '
                        f'capturing: the stream stayed connected but sent less than real time.',
                        True))
    # Unconditional, unlike the two figures above: a discard is an action the app took, not a
    # measurement that is a few seconds off on every clean capture, so there is no noise floor
    # to clear and nothing to gate it on.
    if rec.discarded_segment_count:
        out.append((
            f'{rec.discarded_segment_count} segment'
            f'{"" if rec.discarded_segment_count == 1 else "s"} discarded '
            f'({fmt_utils.fmt_duration(rec.discarded_seconds or 0, with_seconds=True)} of '
            f'provider placeholder video), not joined into the file.', True))
    # Unconditional for the same reason as the discard above: the threshold behind this is
    # already three times clear of the worst honest surplus ever measured here, so there is
    # no noise floor left for a percentage gate to clear.
    if rec.fast_delivery_segment_count:
        fast_secs = fmt_utils.fmt_duration(rec.fast_delivery_seconds or 0, with_seconds=True)
        out.append((
            f'{rec.fast_delivery_segment_count} segment'
            f'{"" if rec.fast_delivery_segment_count == 1 else "s"} delivered video faster '
            f'than real time ({fast_secs} of the file) - kept, but worth checking before '
            f'you watch.', False))
    return out


def trouble_clauses(rec):
    """The sentences about what the file is missing or holds that it should not: the gap,
    the content-versus-capture difference, discarded placeholder segments, segments that
    arrived faster than real time. [] for a clean recording."""
    return [text for text, _ in _trouble_clauses(rec)]


def stopped_early_note(rec):
    """'You stopped it after 2m 35s of 8m scheduled.' when the user's own Stop is the whole
    reason a COMPLETED recording is short of its schedule, else None.

    A recording started late and stopped on purpose read as "Partial" in amber and "Finished
    with trouble", and the user went looking for a fault that was not there (dev/changelog/1287).
    The gap is still said, as the user's: measured against the window it actually ran, which
    the Stop pulled back, the capture must clear PARTIAL_SHARE with no capture gap, or
    partial_note() says what else went missing. Reads the segments only, which every caller
    of partial_note() already loads - the last one's exit reason is the Stop."""
    if rec.status != REC_STATUS_COMPLETED or not short_capture(rec):
        return None
    if not any(s.exit_reason == 'MANUAL_STOP' for s in rec.segments):
        return None
    actual = rec.actual_duration_seconds
    if gap_clause(rec) or actual < rec.duration_seconds * PARTIAL_SHARE:
        return None
    return (f'You stopped it after {fmt_utils.fmt_duration(actual, with_seconds=True)} of '
            f'{fmt_utils.fmt_duration(rec.scheduled_duration_seconds)} scheduled.')


def partial_note(rec):
    """Why a COMPLETED recording is not the whole program, in one or two sentences, or None
    when it is - or when the user's own Stop is the only reason, which stopped_early_note()
    says instead. The guide's recorded mark reads this; anything else that has to tell a
    partial file from a whole one should too."""
    if rec.status != REC_STATUS_COMPLETED or stopped_early_note(rec):
        return None
    parts = []
    short = short_capture(rec)
    if short:
        parts.append(f'{fmt_utils.fmt_duration(short[0])} captured of '
                     f'{fmt_utils.fmt_duration(short[1])} scheduled.')
    gap = gap_clause(rec)
    if gap:
        parts.append(gap)
    return ' '.join(parts) or None


def _http_answer(causes):
    """'HTTP 405 Method Not Allowed' / 'an HTTP 4xx error' from ffmpeg's error lines, or None."""
    http = next((m for m in (_HTTP_SPECIFIC.search(c) for c in causes) if m), None)
    if http:
        return f'HTTP {http.group(1)}{http.group(2).rstrip()}'
    generic = next((m for m in (_HTTP_GENERIC.search(c) for c in causes) if m), None)
    return f'an HTTP {generic.group(1)}xx error' if generic else None


def _unreachable(causes):
    """ffmpeg's own words for a connection it could not make ('Connection refused'), or None."""
    for cause in causes:
        m = _UNREACHABLE.search(cause)
        if m:
            return m.group(0).strip()
    return None


def plain_cause(cause):
    """One ffmpeg error line in plain words, or None when it is not one the app can word.

    The plain account never quotes ffmpeg itself: "[in#0/mpegts @ 0x599c26402080] Error
    during demuxing: ..." inside a sentence was code words to every reader
    (dev/changelog/1287). ffmpeg's own text stays where it is shown as ffmpeg's - the
    Segments table's "why" and the event log."""
    if not cause:
        return None
    from .watchdog import PLACEHOLDER_CAUSE
    if cause == PLACEHOLDER_CAUSE:
        return cause
    answer = _http_answer([cause])
    if answer:
        return f'the provider\'s server answered {answer}'
    unreachable = _unreachable([cause])
    if unreachable:
        return f'ChannelBin could not reach the provider\'s server: {unreachable}'
    if re.search(r'end of file', cause, re.IGNORECASE):
        return 'the provider closed the stream'
    return None


def dead_stream_clause(streak, cause=None, channel_name=None):
    """'the channel "X" failed on 3 connections in a row (cause)' - the one wording of a
    dead-stream trip, shared by the RECORDING_RETRYING alert and the recording page. The
    cause is plain_cause()'s, and left out when it has none."""
    channel = f' "{channel_name}"' if channel_name else ''
    plain = plain_cause(cause)
    return (f'the channel{channel} failed on {streak} connections in a row'
            + (f' ({plain})' if plain else ''))


def _extra(evt):
    if not evt.extra_data:
        return {}
    try:
        data = json.loads(evt.extra_data)
    except (ValueError, TypeError):  # a malformed blob costs the detail, never the page
        return {}
    return data if isinstance(data, dict) else {}


def fault_sentence(causes, placeholder=False, login_refused=False):
    """Who was at fault, said only as far as the evidence goes. `causes` are ffmpeg's own
    error lines (proc_utils.stderr_cause) from the segments and retries that failed."""
    if placeholder:
        return ('For part of the recording the provider served its "channel offline" clip in '
                'place of the channel, so the channel was down at the provider\'s end.')
    if login_refused:
        return 'The provider refused the account\'s login.'
    answer = _http_answer(causes)
    if answer:
        return (f'Where ChannelBin could see a reason, the provider\'s server answered the '
                f'stream request with {answer}.')
    unreachable = _unreachable(causes)
    if unreachable:
        return (f'Where ChannelBin could see a reason, it could not reach the provider\'s '
                f'server ({unreachable}). That is either the network between here '
                f'and the provider or the provider\'s server itself, and ChannelBin cannot '
                f'tell which from here.')
    return ('Nothing ChannelBin saw says whether the provider, the network or the channel '
            'was at fault.')


def retry_got_stream_back(rec, open_segment_bytes=0):
    """Did the last dead-stream retry get the stream back - a segment started after it that
    captured data, or the open one with bytes on disk (`open_segment_bytes`, which its row
    does not carry until it ends)."""
    retries = [e for e in rec.events if e.event_type == RECORDING_RETRY_SCHEDULED]
    if not retries:
        return False
    since = retries[-1].timestamp
    return any(s.started_at and s.started_at >= since and not s.excluded
               and (s.bytes_recorded or (s.ended_at is None and open_segment_bytes))
               for s in rec.segments)


def what_happened(rec, restart_attempts=0, stderr_tails=None, open_segment_bytes=0,
                  retry_alert_clears_after=None):
    """The recording page's plain account: a list of sentences saying what went wrong, what
    ChannelBin did about it and what is left, or [] when nothing went wrong.

    `restart_attempts` is the RESTART_ATTEMPTED count the page's Restarts row shows, so the
    two say the same number; `stderr_tails` is {segment_number: tail}, the Segments table's
    "why" text, read here only for ffmpeg's error lines. `open_segment_bytes` is the open
    segment's size on disk; `retry_alert_clears_after` is set, to the seconds the stream has
    to keep coming, while a "Recording not capturing" alert still stands on a live recording.
    """
    lines = []
    trouble = False

    if rec.started_at and rec.program_start_time:
        late = (rec.started_at - rec.program_start_time).total_seconds()
        if late > 60:
            lines.append(f'It started {fmt_utils.fmt_duration(late)} after the program began, '
                         f'so the start of the program is not in the file.')

    stalls = rec.total_stall_count or 0
    if stalls:
        trouble = True
        line = f'The stream stopped delivering video {fmt_utils.counted(stalls, "time")}.'
        if restart_attempts:
            back = rec.total_restart_count or 0
            # The noun follows the count it sits beside: "0 of those restart" keyed its
            # plural on the attempts (dev/changelog/1287).
            if restart_attempts == 1:
                line += (' ChannelBin restarted the capture once, and it '
                         + ('got the stream back.' if back else 'did not get the stream back.'))
            else:
                line += (f' ChannelBin restarted the capture {restart_attempts} times, and '
                         + (f'{back} of those restarts' if back else 'none of those restarts')
                         + ' got the stream back.')
        lines.append(line)

    causes = [c for c in (stderr_cause(t) for t in (stderr_tails or {}).values()) if c]
    retries = [e for e in rec.events if e.event_type == RECORDING_RETRY_SCHEDULED]
    if retries:
        trouble = True
        first = _extra(retries[0])
        streak = first.get('streak')
        if first.get('cause'):
            causes.insert(0, first['cause'])
        what = (dead_stream_clause(streak, first.get('cause')) if streak
                else 'the channel kept failing right after each reconnect')
        waits = len(retries)
        lines.append(f'{what[0].upper()}{what[1:]}, so ChannelBin waited before trying again'
                     + (f' ({waits} waits).' if waits > 1 else '.'))
        # The account stopped at the wait, so a stream that came back read as one still
        # down beside a strip saying Recording (dev/changelog/1285).
        if retry_got_stream_back(rec, open_segment_bytes):
            back = 'Trying again got the stream back.'
            if retry_alert_clears_after:
                back += (f' The "Recording not capturing" alert clears by itself once it has '
                         f'kept coming for {retry_alert_clears_after} s.')
            lines.append(back)

    failovers = sum(1 for e in rec.events if e.event_type == GROUP_FAILOVER)
    if failovers:
        trouble = True
        lines.append(f'It moved to another channel in its group '
                     f'{fmt_utils.counted(failovers, "time")}.')

    # Only a clause that lost something asks who was at fault: a file a few seconds longer
    # than its capture, with nothing else wrong, drew "Nothing ChannelBin saw says whether
    # the provider, the network or the channel was at fault" (dev/changelog/1287).
    for text, lost in _trouble_clauses(rec):
        lines.append(text)
        trouble = trouble or lost

    # The early stop pulls stop_time back to the moment of the stop, and scheduled_stop_time
    # keeps what was asked for, so the difference is how early.
    if any(e.event_type == RECORDING_STOPPED_EARLY for e in rec.events):
        early = ((rec.scheduled_stop_time - rec.stop_time).total_seconds()
                 if rec.scheduled_stop_time and rec.stop_time else 0)
        lines.append(f'You stopped it {fmt_utils.fmt_duration(early)} before its scheduled end.'
                     if early > 60 else 'You stopped it before its scheduled end.')

    if trouble:
        from .watchdog import PLACEHOLDER_CAUSE
        lines.append(fault_sentence(
            causes,
            placeholder=bool(rec.discarded_segment_count) or PLACEHOLDER_CAUSE in causes,
            login_refused=any(e.event_type == RECORDING_LOGIN_REFUSED for e in rec.events)))

    # First, not last: it is the answer to the question a reader opens this card with, and as
    # the last line it sat below the fold on a phone (dev/changelog/1285).
    if lines and rec.status == REC_STATUS_COMPLETED and rec.actual_duration_seconds:
        size = (f', {fmt_utils.fmt_bytes(rec.final_file_size)}'
                if rec.final_file_size else '')
        lines.insert(0, f'You still have a file: '
                        f'{fmt_utils.fmt_duration(rec.actual_duration_seconds, with_seconds=True)} '
                        f'of video{size}.')
    return lines
