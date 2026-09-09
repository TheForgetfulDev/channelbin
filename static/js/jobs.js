/* Scheduled jobs (templates/jobs.html).
   Rollout: dev/changelog/446.

   Lifted out of the template. The row actions now live behind a kebab
   (DESIGN.md 3.6), so they bind through one delegated listener rather than one
   querySelectorAll per button class.

   The three confirm() prompts this replaced were all doing the same illegal
   thing: overloading OK/Cancel with two different outcomes, neither of which was
   "don't run it". Two said so in their own prose - "Click Cancel to clear the
   scheduled run" - which means Cancel ran the job. Each is now a modal with
   verb-named buttons (DESIGN.md 4), so the button pressed is the thing done. */
(() => {
  'use strict';

  // A schedule-changing action leaves every "next run" on the page stale, so the
  // page is re-read from the scheduler afterwards. The delay is what makes the
  // toast readable before the reload takes it away (dashboard.js does the same).
  function reloadAfter(msg) {
    showToast(msg);
    setTimeout(() => location.reload(), 1500);
  }

  function run(url, body, skipUrl, name) {
    jsonFetch(url, { method: 'POST', body })
      .then(() => {
        if (!skipUrl) { showToast(`${name} started.`); return null; }
        return jsonFetch(skipUrl, { method: 'POST' })
          .then(() => reloadAfter(`${name} started, and its next scheduled run was skipped.`))
          .catch((e) => showToast(
            `${name} started, but its next scheduled run could not be skipped: ${e.message}`,
            { type: 'warning' }));
      })
      .catch((e) => {
        // A 409 carrying `conflicts` is the manual-sync warn-and-override path
        // (DESIGN-concurrency.md 5.4): show what the conflict is, then resubmit
        // with force only if the user says so. Any other error is terminal.
        const conflicts = e.data && e.data.conflicts;
        if (e.status === 409 && conflicts && conflicts.length) {
          buildModal({
            title: 'Something else is already running',
            body: `<p>Running <strong>${escHtml(name)}</strong> now would overlap:</p><ul>` +
                  conflicts.map((c) => `<li>${escHtml(c)}</li>`).join('') + '</ul>',
            footer: [
              { label: 'Leave it', class: 'btn' },
              {
                label: 'Run anyway',
                class: 'btn btn-danger',
                onClick: (close) => { close(); run(url, JSON.stringify({ force: true }), skipUrl, name); },
              },
            ],
          });
          return;
        }
        showToast(`Could not start ${name}: ${e.message}`, { type: 'error' });
      });
  }

  // Run now, for a job that also has a future run this one could replace. The
  // three outcomes are three buttons; the prompt this replaced had two, and
  // neither of them meant "don't run it".
  function runPrompt(btn) {
    const name = btn.dataset.name;
    const nextRun = btn.dataset.nextRun || 'time unknown';
    const keepPrompt = btn.dataset.keepPrompt === '1';
    const skipUrl = btn.dataset.skipUrl || null;
    const footer = [{ label: 'Cancel', class: 'btn' }];

    if (keepPrompt) {
      // The keep-prompt job answers in the request body; the skip-url job answers
      // with a second request. One question, two mechanisms - which is why both
      // used to be spelled as the same overloaded confirm().
      footer.push({
        label: 'Run and drop the schedule',
        class: 'btn',
        onClick: (c) => { c(); run(btn.dataset.url, JSON.stringify({ keep_schedule: false }), null, name); },
      });
      footer.push({
        label: 'Run and keep it',
        class: 'btn btn-primary',
        onClick: (c) => { c(); run(btn.dataset.url, JSON.stringify({ keep_schedule: true }), null, name); },
      });
    } else {
      footer.push({
        label: 'Run and skip the next one',
        class: 'btn',
        onClick: (c) => { c(); run(btn.dataset.url, null, skipUrl, name); },
      });
      footer.push({
        label: 'Run and keep the next one',
        class: 'btn btn-primary',
        onClick: (c) => { c(); run(btn.dataset.url, null, null, name); },
      });
    }

    buildModal({
      title: `Run ${name} now`,
      body: `<p>This job is scheduled to run again at <strong>${escHtml(nextRun)}</strong>. ` +
            'Running it now does not change that unless you say so.</p>',
      footer,
    });
  }

  function runPlain(btn) {
    buildModal({
      title: `Run ${btn.dataset.name} now`,
      body: '<p>This runs the job immediately, outside its schedule.</p>',
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Run now',
          class: 'btn btn-primary',
          onClick: (c) => { c(); run(btn.dataset.url, null, null, btn.dataset.name); },
        },
      ],
    });
  }

  function skip(btn) {
    buildModal({
      title: 'Skip the next run',
      body: `<p>The run at <strong>${escHtml(btn.dataset.nextRun || 'the next scheduled time')}</strong> ` +
            'will not happen. The job itself is untouched and runs at the time after that.</p>',
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Skip it',
          class: 'btn btn-primary',
          onClick: (c) => {
            c();
            jsonFetch(btn.dataset.url, { method: 'POST' })
              .then(() => reloadAfter('The next run was skipped.'))
              .catch((e) => showToast(`Could not skip that run: ${e.message}`, { type: 'error' }));
          },
        },
      ],
    });
  }

  function cancel(btn) {
    buildModal({
      title: 'Cancel this scheduled run',
      body: `<p>The scheduled run of <strong>${escHtml(btn.dataset.name)}</strong> is removed. ` +
            'The health check itself is kept as it is - only this run goes away.</p>',
      footer: [
        { label: 'Keep it', class: 'btn' },
        {
          label: 'Cancel the run',
          class: 'btn btn-danger',
          onClick: (c) => {
            c();
            jsonFetch(btn.dataset.url, { method: 'POST' })
              .then(() => reloadAfter('The scheduled run was cancelled.'))
              .catch((e) => showToast(`Could not cancel that run: ${e.message}`, { type: 'error' }));
          },
        },
      ],
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#jb-refresh')) { location.reload(); return; }
    const btn = e.target.closest('.menu-item[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'run') {
      // A job with neither a keep-prompt nor a next run to skip has nothing to
      // ask beyond "really?", which is what the plain modal is.
      if (btn.dataset.keepPrompt === '1' || btn.dataset.skipUrl) runPrompt(btn);
      else runPlain(btn);
    } else if (btn.dataset.act === 'run-disabled') {
      // Greyed but clickable (CLAUDE.md "loud, safe, recoverable failure") - explains what
      // the job does and why Run Now isn't offered, instead of a working-looking button
      // that a real click would 400 on.
      showToast(btn.dataset.reason, { type: 'info' });
    } else if (btn.dataset.act === 'skip') skip(btn);
    else if (btn.dataset.act === 'cancel') cancel(btn);
  });
})();
