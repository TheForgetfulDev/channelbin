'use strict';

// Manual "Add Recording" modal - shared by guide.html and guide_empty.html.
// Each page defines a small MANUAL_RECORD_CONFIG object before loading this file:
//   { testUrlEndpoint, newRecordingUrl, recDetailUrlBase }
// and may optionally set window.onManualRecordSaved() to run after a successful save.
// Depends on util.js (buildModal, jsonFetch, showConflictError, recordingWarningsHtml,
// dateToTzInputValue, tzInputValueToDate, displayTz, escHtml).

function openManualRecordModal() {
  const existing = document.getElementById('manual-record-modal');
  if (existing) existing.remove();

  const tz = displayTz();
  const now = new Date();
  now.setMinutes(Math.ceil(now.getMinutes() / 5) * 5, 0, 0);
  const stop = new Date(now.getTime() + 60 * 60 * 1000);

  const body = document.createElement('div');
  body.innerHTML = `
    <p class="manual-record-info">
      Use this to schedule a one-off recording for something not yet in your TV Guide - for
      example a program with no EPG data, or a stream from an account you haven't added to
      ChannelBin yet. Just paste the stream URL and a time window below.
    </p>
    <div id="manual-modal-error" style="display:none; color: var(--bad); margin-bottom: 0.75rem; font-size: 0.875rem;"></div>
    <div class="form-group">
      <label for="manual-modal-name">Recording Name <span class="required">*</span></label>
      <input type="text" id="manual-modal-name" class="form-control" required
             placeholder="e.g. Game of Thrones S01E01">
    </div>
    <div class="form-group">
      <label for="manual-modal-url">IPTV URL <span class="required">*</span></label>
      <div class="manual-url-row">
        <input type="url" id="manual-modal-url" class="form-control" required
               placeholder="http://your-provider.com/stream/channel">
        <button type="button" class="btn btn-sm" id="manual-test-btn">Validate</button>
      </div>
      <small class="form-hint">HLS (.m3u8) or MPEG-TS stream URL</small>
      <div id="manual-test-result" class="manual-test-result" style="display:none;"></div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="manual-modal-start">Start Time <span class="tz-label">${escHtml(tz)}</span></label>
        <input type="datetime-local" id="manual-modal-start" class="form-control" required>
      </div>
      <div class="form-group">
        <label for="manual-modal-stop">Stop Time <span class="tz-label">${escHtml(tz)}</span></label>
        <input type="datetime-local" id="manual-modal-stop" class="form-control" required>
      </div>
    </div>`;

  function submit(close) {
    document.getElementById('manual-modal-error').style.display = 'none';
    const payload = new URLSearchParams({
      name: document.getElementById('manual-modal-name').value.trim(),
      url: document.getElementById('manual-modal-url').value.trim(),
      start_time: document.getElementById('manual-modal-start').value,
      stop_time: document.getElementById('manual-modal-stop').value,
    });
    submitManualRecord(payload, close);
  }

  // Shared by the initial submit and by "Proceed anyway" - a schedule that overlaps another
  // recording answers {success: false, overlap_warning, connection_limit_warning} rather than
  // failing outright (dev/changelog/858): this modal usually has no channel_id at all (an
  // ad-hoc URL with nothing matched), but new_recording_json still tries a URL match, so it
  // can trip the same warning as the guide's record modal.
  function submitManualRecord(payload, close) {
    jsonFetch(MANUAL_RECORD_CONFIG.newRecordingUrl, { method: 'POST', body: payload }).then((data) => {
      if (data && data.success === false) {
        showManualWarnings(data, payload, close);
        return;
      }
      close();
      if (typeof window.onManualRecordSaved === 'function') window.onManualRecordSaved();
    }).catch((e) => {
      showConflictError('manual-modal-error', e.message || 'Failed to schedule recording.',
        e.data && e.data.conflicts, MANUAL_RECORD_CONFIG.recDetailUrlBase);
    });
  }

  function showManualWarnings(data, payload, close) {
    const el = document.getElementById('manual-modal-error');
    el.innerHTML = recordingWarningsHtml(data, MANUAL_RECORD_CONFIG.recDetailUrlBase) +
      '<div style="margin-top:0.5rem;"><button type="button" class="btn btn-sm btn-danger-outline" ' +
      'id="manual-warn-force">Proceed anyway</button></div>';
    el.style.display = 'block';
    document.getElementById('manual-warn-force').addEventListener('click', () => {
      const forced = new URLSearchParams(payload);
      forced.set('force', '1');
      submitManualRecord(forced, close);
    });
  }

  const modal = buildModal({
    title: 'Add Manual Recording',
    body,
    footer: [
      { label: 'Cancel', class: 'btn' },
      { label: 'Schedule Recording', class: 'btn btn-primary',
        onClick: (close) => { submit(close); return false; } },
    ],
  });
  modal.id = 'manual-record-modal';

  document.getElementById('manual-modal-start').value = dateToTzInputValue(now, tz);
  document.getElementById('manual-modal-stop').value = dateToTzInputValue(stop, tz);

  document.getElementById('manual-test-btn').addEventListener('click', runManualUrlTest);
  document.getElementById('manual-modal-start').addEventListener('change', () => {
    const startInput = document.getElementById('manual-modal-start');
    const stopInput = document.getElementById('manual-modal-stop');
    const start = tzInputValueToDate(startInput.value, tz);
    if (!stopInput.value || tzInputValueToDate(stopInput.value, tz) <= start) {
      const newStop = new Date(start.getTime() + 60 * 60 * 1000);
      stopInput.value = dateToTzInputValue(newStop, tz);
    }
  });
}

function hideManualTestResult() {
  const el = document.getElementById('manual-test-result');
  el.style.display = 'none';
  el.className = 'manual-test-result';
  el.innerHTML = '';
}

function showManualTestResult(success, html) {
  const el = document.getElementById('manual-test-result');
  el.className = 'manual-test-result ' + (success ? 'manual-test-pass' : 'manual-test-fail');
  el.innerHTML = html;
  el.style.display = 'block';
}

async function runManualUrlTest() {
  const urlInput = document.getElementById('manual-modal-url');
  const url = urlInput.value.trim();
  if (!url) {
    showManualTestResult(false, 'Enter a URL first.');
    return;
  }

  const btn = document.getElementById('manual-test-btn');
  btn.disabled = true;
  btn.classList.add('btn-spinner');
  const origText = btn.textContent;
  btn.textContent = 'Validating…';
  hideManualTestResult();

  try {
    const data = await jsonFetch(MANUAL_RECORD_CONFIG.testUrlEndpoint, {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
    const parts = [];
    if (data.resolution) parts.push(data.resolution);
    if (data.fps) parts.push(`${data.fps.toFixed(0)} fps`);
    if (data.bitrate_kbps) parts.push(`${Math.round(data.bitrate_kbps)} kbps`);
    if (data.audio_codec) {
      let a = data.audio_codec.toUpperCase();
      if (data.audio_channels) a += ` ${data.audio_channels}ch`;
      if (data.audio_sample_rate) a += ` ${Math.round(data.audio_sample_rate / 1000)}kHz`;
      parts.push(a);
    }
    showManualTestResult(true, '✓ Connected - ' + (parts.join(' · ') || 'received a data stream'));
  } catch (e) {
    showManualTestResult(false, '✗ ' + (e.message || 'Request failed. Check your connection.'));
  } finally {
    btn.disabled = false;
    btn.classList.remove('btn-spinner');
    btn.textContent = origText;
  }
}

document.addEventListener('DOMContentLoaded', function () {
  const openBtn = document.getElementById('btn-manual-record');
  if (openBtn) openBtn.addEventListener('click', function (e) {
    e.preventDefault();
    openManualRecordModal();
  });

  const params = new URLSearchParams(window.location.search);
  if (params.get('new') === '1') {
    openManualRecordModal();
  }
});
