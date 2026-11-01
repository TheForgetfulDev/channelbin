"""Stand-ins for APScheduler's job and scheduler, for tests of what /jobs, the Dashboard
timeline and the nav's background chip make of the scheduler's state.

A real BackgroundScheduler is not used by these tests because its jobstore writes to disk
and its next_run_time depends on the wall clock; what is under test reads three things off
a job - `id`, `next_run_time` (tz-aware, as APScheduler hands it over) and `trigger` - and
two off the scheduler, `get_jobs()` and `get_job(id)`. One copy here, so a fourth test
module does not spell it again (dev/changelog/1351).
"""
import types
from datetime import timezone

from apscheduler.triggers.interval import IntervalTrigger


class FakeJob:
    def __init__(self, job_id, next_run, trigger=None, kwargs=None):
        self.id = job_id
        self.next_run_time = (next_run.replace(tzinfo=timezone.utc)
                              if next_run is not None and next_run.tzinfo is None
                              else next_run)
        self.trigger = trigger if trigger is not None else IntervalTrigger(hours=1)
        self.kwargs = kwargs or {}


def fake_scheduler(jobs):
    """`get_jobs()` answers `jobs`; `get_job(id)` finds one by id, or None."""
    jobs = list(jobs)
    return types.SimpleNamespace(
        get_jobs=lambda: list(jobs),
        get_job=lambda job_id: next((j for j in jobs if j.id == job_id), None))
