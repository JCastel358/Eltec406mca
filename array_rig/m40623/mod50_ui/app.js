/* Local operator UI and self-contained saved report. No external services. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const reportMode = Boolean(window.ELTEC_REPORT);
  const positionNames = Array.from({length: 50}, (_, i) => `${Math.floor(i / 10) + 1}-${i % 10 + 1}`);
  let state = {busy: false, phase: 'Ready', progress: 0, rows: [], result: null};
  let selected = null;
  let initialSourceSet = false;
  let requestPending = false;
  let connected = reportMode;
  let screen = reportMode ? 'test' : 'setup';
  let dismissedRun = null;
  let stateVersion = 0;
  let stopQueued = false;
  let stopPending = false;
  let actionError = '';
  let stopError = '';
  // Recovery is independent of the currently displayed batch. Clearing the
  // screen must not discard an eligible, durably saved DC recording.
  let noiseRecovery = null;
  let selectedRecovery = null;
  let recoveryError = '';
  const tiles = new Map();
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const fmt = (x, digits = 3, suffix = '') => finite(x) ? x.toFixed(digits) + suffix : '—';

  function rows() {
    const source = state.rows?.length ? state.rows : state.result?.rows || [];
    return positionNames.map((position, index) => source.find(row => row.position === position) ||
      {position, channel: index + 1, result: 'NOT_MEASURED', dc_check: 'NOT_MEASURED', presence: 'UNKNOWN', flags: []});
  }

  function acquisitionFlags(row) {
    return (row.flags || []).filter(flag => /^(DC|NOISE): acquisition invalid\/incomplete$/i.test(flag));
  }

  function signalFlags(row) {
    // Older reports mixed this one acquisition marker into signal flags.
    // Keep every other flag, including clipping, headroom and connection checks.
    return (row.flags || []).filter(flag => !/^(DC|NOISE): acquisition invalid\/incomplete$/i.test(flag));
  }

  function stageValidity(row, mode) {
    const explicit = row.acquisition_validity?.[mode];
    if (typeof explicit === 'boolean') return explicit;
    if (typeof row[mode + '_detail']?.valid_acquisition === 'boolean') return row[mode + '_detail'].valid_acquisition;
    if (acquisitionFlags(row).some(flag => flag.toLowerCase().startsWith(mode + ':'))) return false;
    if (mode === 'dc' && ['IN_WINDOW_PROVISIONAL', 'OUTSIDE_WINDOW_PROVISIONAL', 'RANGE_LIMIT', 'NO_SIGNAL'].includes(row.dc_check)) return true;
    if (mode === 'noise' && finite(row.daq_3hz_q3_rms_uv)) return true;
    return null;
  }

  function classification(row) {
    if (row.presence === 'NO_SIGNAL' || row.result === 'NO_SIGNAL') return ['empty', 'No signal'];
    if (row.result === 'NOT_INSTALLED' || row.installed === false) return ['empty', 'Excluded'];
    if (row.dc_check === 'OUTSIDE_WINDOW_PROVISIONAL' || row.dc_check === 'RANGE_LIMIT') return ['fail', 'Offset fail'];
    if (signalFlags(row).length || (row.result === 'CHECK' && !acquisitionFlags(row).length && !(row.acquisition_errors || []).length)) return ['fail', 'Check'];
    if (state.busy && stageValidity(row, 'dc') === true && stageValidity(row, 'noise') !== true) return ['testing', 'Noise pending'];
    if ((row.acquisition_errors || []).length || acquisitionFlags(row).length ||
        (state.result && stageValidity(row, 'noise') !== true)) {
      return ['incomplete', stageValidity(row, 'dc') === true ? 'Noise incomplete' : 'Incomplete'];
    }
    if (row.dc_check === 'IN_WINDOW_PROVISIONAL') {
      return finite(row.daq_3hz_q3_rms_uv) && stageValidity(row, 'noise') !== false ? ['review', 'Review'] : ['pass', 'Offset pass'];
    }
    if (state.busy) return ['testing', 'Testing'];
    return ['ready', state.result ? 'Incomplete' : 'Ready'];
  }

  function buildTray() {
    for (const position of positionNames) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sensor-cell';
      button.dataset.status = 'ready';
      button.innerHTML = `<span class="slot-number">${position}</span><span class="slot-label">Ready</span><i class="offset-dot" hidden aria-hidden="true"></i>`;
      button.addEventListener('click', () => selectDetector(position, true));
      $('sensorGrid').append(button);
      tiles.set(position, button);
    }
  }

  function selectDetector(position, scroll) {
    selected = position;
    $('detailSection').hidden = false;
    paintTray();
    paintDetail();
    if (scroll) {
      $('detailTitle').focus({preventScroll: true});
      $('detailSection').scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  }

  function paintTray() {
    const data = rows();
    let good = 0, failures = 0, empty = 0, review = 0;
    for (const row of data) {
      const [type, label] = classification(row);
      const button = tiles.get(row.position);
      button.dataset.status = type;
      button.classList.toggle('selected', selected === row.position);
      button.setAttribute('aria-pressed', String(selected === row.position));
      button.setAttribute('aria-label', `Position ${row.position}: ${label}${row.dc_check === 'IN_WINDOW_PROVISIONAL' ? ', offset passes provisional limits' : ''}. Select for details.`);
      button.querySelector('.slot-label').textContent = label;
      button.querySelector('.offset-dot').hidden = row.dc_check !== 'IN_WINDOW_PROVISIONAL';
      if (row.dc_check === 'IN_WINDOW_PROVISIONAL') good++;
      if (type === 'fail') failures++;
      if (type === 'empty') empty++;
      if (type === 'review') review++;
    }
    $('passCount').textContent = good;
    $('failCount').textContent = failures;
    $('emptyCount').textContent = empty;
    $('reviewCount').textContent = review;
    if (state.result) {
      const result = state.result;
      $('traySummary').textContent = `Lot ${result.lot || '—'} · Tray ${result.tray || '—'} · ${result.operator || '—'}`;
    } else $('traySummary').textContent = state.busy ? 'Checking all 50 positions · click any detector' : '50 positions · select any detector for details';
  }

  function badge(text, type) {
    const span = document.createElement('span');
    span.className = 'badge ' + type;
    span.textContent = text;
    return span;
  }

  function metrics(target, items) {
    target.replaceChildren();
    for (const [label, value] of items) {
      const div = document.createElement('div');
      const name = document.createElement('span');
      name.textContent = label;
      const output = document.createElement('strong');
      output.textContent = value;
      div.append(name, output);
      target.append(div);
    }
  }

  function chart(target, detail, mode) {
    const emptyText = mode === 'dc' ? 'Offset trace will appear after DC recording.' : 'Noise trace will appear after noise recording.';
    if (!detail || !Array.isArray(detail.time_s) || !Array.isArray(detail.volts)) {
      target.innerHTML = `<div class="chart-empty">${emptyText}</div>`;
      return;
    }
    const scale = mode === 'noise' ? 1000 : 1;
    let points = detail.time_s.map((t, i) => [t, detail.volts[i]]).filter(([t, v]) => finite(t) && finite(v)).map(([t, v]) => [t, v * scale]);
    if (points.length < 2) {
      target.innerHTML = '<div class="chart-empty">Not enough recorded samples for a waveform.</div>';
      return;
    }
    const domainStart = points[0][0], domainEnd = points[points.length - 1][0];
    // Preserve visible extrema rather than simply skipping each Nth sample.
    if (points.length > 1800) {
      const reduced = [], step = Math.ceil(points.length / 900);
      for (let i = 0; i < points.length; i += step) {
        const block = points.slice(i, i + step);
        let low = 0, high = 0;
        block.forEach((p, n) => {if (p[1] < block[low][1]) low = n; if (p[1] > block[high][1]) high = n;});
        reduced.push(...(low === high ? [block[low]] : low < high ? [block[low], block[high]] : [block[high], block[low]]));
      }
      points = reduced;
    }
    const w = 720, h = 260, left = 80, right = 18, top = 22, bottom = 39;
    const x0 = domainStart, x1 = domainEnd;
    let low = Infinity, high = -Infinity;
    for (const p of points) {low = Math.min(low, p[1]); high = Math.max(high, p[1]);}
    const range = Math.max(high - low, mode === 'dc' ? .0002 : .001);
    low -= range * .14; high += range * .14;
    const x = v => left + (v - x0) / Math.max(x1 - x0, 1e-9) * (w - left - right);
    const y = v => top + (high - v) / (high - low) * (h - top - bottom);
    const tickDigits = mode === 'dc' ? Math.min(6, Math.max(2, 2 - Math.floor(Math.log10(high - low)))) : Math.min(4, Math.max(1, 1 - Math.floor(Math.log10(high - low))));
    let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${mode === 'dc' ? 'Offset over time in volts' : 'Noise excerpt in millivolts'}"><rect width="${w}" height="${h}" fill="white"/>`;
    if (mode === 'dc') {
      const bandLow = Math.max(low, finite(detail.limit_min_v) ? detail.limit_min_v : .3);
      const bandHigh = Math.min(high, finite(detail.limit_max_v) ? detail.limit_max_v : 1.2);
      if (bandHigh > bandLow) svg += `<rect x="${left}" y="${y(bandHigh)}" width="${w-left-right}" height="${y(bandLow)-y(bandHigh)}" fill="#edf9f3"/>`;
    }
    for (let i = 0; i <= 4; i++) {
      const value = low + i * (high - low) / 4, py = y(value);
      svg += `<line x1="${left}" y1="${py}" x2="${w-right}" y2="${py}" stroke="#e3e8ef"/><text x="${left-10}" y="${py+4}" text-anchor="end" font-family="Segoe UI, sans-serif" font-size="12" fill="#5c6979">${value.toFixed(tickDigits)}</text>`;
      const time = x0 + i * (x1 - x0) / 4, px = x(time);
      svg += `<text x="${px}" y="${h-16}" text-anchor="middle" font-family="Segoe UI, sans-serif" font-size="12" fill="#5c6979">${time.toFixed(1)} s</text>`;
    }
    if (mode === 'dc') for (const limit of [.3, 1.2]) {
      if (limit >= low && limit <= high) svg += `<line x1="${left}" y1="${y(limit)}" x2="${w-right}" y2="${y(limit)}" stroke="#13805a" stroke-width="1.5" stroke-dasharray="6 5"/>`;
    }
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`).join(' ');
    svg += `<path d="${path}" fill="none" stroke="${mode === 'dc' ? '#244a9f' : '#6b49ac'}" stroke-width="1.5" stroke-linejoin="round"/>`;
    if (mode === 'noise' && finite(detail.event_time_s) && detail.event_time_s >= x0 && detail.event_time_s <= x1) {
      const eventX = x(detail.event_time_s);
      const labelWidth = 126, labelX = Math.max(left + 3, Math.min(eventX + 7, w - right - labelWidth - 3));
      svg += `<g aria-label="Flagged event at ${detail.event_time_s.toFixed(3)} seconds"><line x1="${eventX}" y1="${top}" x2="${eventX}" y2="${h-bottom}" stroke="#b42318" stroke-width="1.8" stroke-dasharray="5 4"/><rect x="${labelX}" y="${top+5}" width="${labelWidth}" height="23" rx="4" fill="#fde7e5" stroke="#efa9a2"/><text x="${labelX+7}" y="${top+20}" font-family="Segoe UI, sans-serif" font-size="11" font-weight="700" fill="#b42318">Flag at ${detail.event_time_s.toFixed(3)} s</text></g>`;
    }
    svg += `<text x="${left}" y="13" font-family="Segoe UI, sans-serif" font-size="11" fill="#5c6979">${mode === 'dc' ? 'Zoomed to measured range · green area is inside offset limits' : 'Recorded excerpt · time is relative to the noise recording'}</text></svg>`;
    target.innerHTML = svg;
  }

  function paintDetail() {
    if (!selected) return;
    const row = rows().find(r => r.position === selected);
    if (!row) return;
    $('detailTitle').textContent = `Detector ${row.position}`;
    $('detailPlaceholder').hidden = true;
    $('detailContent').hidden = false;
    const badges = $('detailBadges');
    badges.replaceChildren();
    const noSignal = row.presence === 'NO_SIGNAL' || row.result === 'NO_SIGNAL';
    if (noSignal) badges.append(badge('No detector sensed', 'empty'));
    else if (row.dc_check === 'IN_WINDOW_PROVISIONAL') badges.append(badge('Offset PASS · provisional', 'pass'));
    else if (['OUTSIDE_WINDOW_PROVISIONAL', 'RANGE_LIMIT'].includes(row.dc_check)) badges.append(badge('Offset FAIL · provisional', 'fail'));
    else badges.append(badge('Offset not measured', 'empty'));
    if (noSignal) badges.append(badge('Noise analysis excluded', 'empty'));
    else if (signalFlags(row).some(f => /^NOISE:/i.test(f))) badges.append(badge('Noise · signal check', 'fail'));
    else if (state.busy && stageValidity(row, 'noise') !== true) badges.append(badge('Noise recording pending', 'empty'));
    else if (stageValidity(row, 'noise') === false || (state.result && !finite(row.daq_3hz_q3_rms_uv))) badges.append(badge('Noise recording incomplete', 'incomplete'));
    else if (finite(row.daq_3hz_q3_rms_uv)) badges.append(badge('Noise · review', 'review'));
    else badges.append(badge(noSignal ? 'Noise analysis excluded' : 'Noise not measured', 'empty'));
    $('detailReason').textContent = noSignal
      ? 'No detector sensed — this may be an empty socket, a dead detector, or a disconnected detector. Noise analysis is excluded automatically; the slot and recorded evidence stay available for inspection.'
      : [...signalFlags(row), ...(row.acquisition_errors || []), ...acquisitionFlags(row),
          stageValidity(row, 'dc') === true && stageValidity(row, 'noise') !== true
            ? 'The valid offset result is retained. Noise has not completed a valid recording; this is an acquisition issue, not a detector failure.'
            : finite(row.daq_3hz_q3_rms_uv) ? 'Noise is saved for review; no calibrated noise pass/fail limit is applied yet.'
            : 'This position has not completed its measurements yet.'].join(' · ');
    const dc = row.dc_detail, noise = row.noise_detail;
    chart($('offsetChart'), dc, 'dc');
    chart($('noiseChart'), noise, 'noise');
    metrics($('offsetMetrics'), [
      ['Mean', fmt(row.dc_v, 5, ' V')], ['Minimum', fmt(dc?.min_v, 5, ' V')], ['Maximum', fmt(dc?.max_v, 5, ' V')],
      ['Slope', finite(dc?.slope_v_per_s) ? `${dc.slope_v_per_s >= 0 ? '+' : ''}${(dc.slope_v_per_s * 1000).toFixed(3)} mV/s` : '—'],
      ['Start → end', finite(dc?.start_v) && finite(dc?.end_v) ? `${dc.start_v.toFixed(4)} → ${dc.end_v.toFixed(4)} V` : '—'],
      ['Allowed window', '0.3–1.2 V']
    ]);
    let trend = dc?.trend || '';
    if (finite(dc?.estimated_seconds_to_window)) trend += `${trend ? ' ' : ''}At this slope, the window would be reached in about ${Math.ceil(dc.estimated_seconds_to_window)} s. This is an estimate, not a guarantee or a passing result.`;
    $('offsetNote').textContent = dc ? [stageValidity(row, 'dc') === false ? 'Diagnostic trace only: the DC acquisition was incomplete and is not a valid offset result.' : '', trend, dc.prediction_note || (!finite(dc.estimated_seconds_to_window) ? 'The trend does not guarantee that more settling time will produce a pass.' : '')].filter(Boolean).join(' ') : 'The DC trace, minimum, maximum and slope are saved after offset recording.';
    const noiseMetrics = [
      ['3 Hz / Q3 RMS', fmt(row.daq_3hz_q3_rms_uv, 3, ' µV')], ['1–100 Hz RMS', fmt(row.daq_1_100_hz_rms_uv, 3, ' µV')],
      ['Excerpt', finite(noise?.start_s) && finite(noise?.end_s) ? `${noise.start_s.toFixed(1)}–${noise.end_s.toFixed(1)} s` : '—'],
      ['Excerpt minimum', finite(noise?.min_v) ? fmt(noise.min_v * 1000, 3, ' mV') : '—'],
      ['Excerpt maximum', finite(noise?.max_v) ? fmt(noise.max_v * 1000, 3, ' mV') : '—'], ['Noise acceptance', 'Not calibrated']
    ];
    if (finite(noise?.event_time_s) && finite(noise?.start_s) && finite(noise?.end_s) &&
        noise.event_time_s >= noise.start_s && noise.event_time_s <= noise.end_s) {
      noiseMetrics.push(['Flagged event', fmt(noise.event_time_s, 3, ' s')]);
    }
    metrics($('noiseMetrics'), noiseMetrics);
    $('noiseNote').textContent = noise ? `${stageValidity(row, 'noise') === false ? 'Diagnostic excerpt only: the interrupted recording is not a valid noise result. ' : ''}${noise.selection_reason || 'Representative recorded waveform.'} Values are at the DAQ output and include front-end gain. Full raw data are saved.` : 'No valid noise recording is available yet. A waveform will appear after recording; no sensitivity test is performed.';
    $('detailIdentity').textContent = `Position ${row.position} · ${row.card || 'M25'}${finite(row.card_channel) ? ` channel ${row.card_channel}` : ''}${finite(row.physical_daq_channel) ? ` · DAQ CH${row.physical_daq_channel}` : ''} · Full recordings and these charts are retained with the run.`;
  }

  function locked() {
    return Boolean(state.busy || requestPending || (!connected && screen === 'test'));
  }

  function blankState() {
    return {busy: false, phase: 'Ready', progress: 0, rows: [], result: null,
      restart_required: state.restart_required,
      message: 'Ready when you are. Press Start to begin the 50-detector check.'};
  }

  function showScreen(next, focusId) {
    screen = next;
    document.body.dataset.screen = next;
    for (const name of ['setup', 'prep', 'test', 'batch', 'recovery']) $(name + 'Section').hidden = name !== next;
    $('detailSection').hidden = next !== 'test' || !selected;
    paint();
    window.scrollTo({top: 0, behavior: 'instant'});
    if (focusId) $(focusId).focus({preventScroll: true});
  }

  function discardDisplayedRun() {
    // Keep saved evidence on the server. Polls for this finished run must not
    // repaint its results under the next batch's identity.
    dismissedRun = state.run_id || dismissedRun;
    state = blankState();
    selected = null;
    actionError = '';
    stopError = '';
    stateVersion++;
  }

  function mainSetup() {
    if (reportMode || locked()) return;
    selectedRecovery = null;
    recoveryError = '';
    if (state.result) {
      $('lotInput').value = '';
      $('trayInput').value = '1';
    }
    discardDisplayedRun();
    showScreen('setup', 'setupTitle');
  }

  function nextBatch() {
    if (reportMode || locked() || !state.result || state.restart_required) return;
    $('newBatchInput').value = '';
    showScreen('batch', 'newBatchInput');
  }

  function validSetup() {
    for (const id of ['lotInput', 'operatorInput']) {
      const input = $(id);
      input.value = input.value.trim();
    }
    return $('controls').reportValidity();
  }

  function availableRecovery() {
    if (reportMode || !noiseRecovery || state.restart_required) return null;
    const result = state.result;
    if (result) return !result.data_complete && noiseRecovery.lot === result.lot &&
      noiseRecovery.operator === result.operator && Number(noiseRecovery.tray) === Number(result.tray) &&
      Boolean(noiseRecovery.simulate) === Boolean(result.simulated) ? noiseRecovery : null;
    return Boolean(noiseRecovery.simulate) === ($('sourceInput').value === 'demo') ? noiseRecovery : null;
  }

  function recoveryDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || 'Date unavailable') : date.toLocaleString();
  }

  function openRecovery() {
    const candidate = availableRecovery();
    if (locked() || !connected || !candidate) return;
    selectedRecovery = {...candidate};
    recoveryError = '';
    $('recoveryConfirm').checked = false;
    showScreen('recovery', 'recoveryTitle');
  }

  function paintRecovery() {
    const candidate = availableRecovery();
    const canOffer = Boolean(candidate && !state.busy && !requestPending);
    $('savedRecoveryCard').hidden = !canOffer || Boolean(state.result);
    document.body.dataset.recoveryAvailable = String(!$('savedRecoveryCard').hidden);
    $('recoverSavedButton').disabled = !connected || locked();
    $('retryNoiseButton').hidden = !canOffer || !state.result;
    $('retryNoiseButton').disabled = !connected || locked();
    $('completionPrompt').textContent = candidate ? 'Offset saved. Noise can be retried.' : 'All set with this tray?';
    if (candidate) $('savedRecoverySummary').textContent = `Batch ${candidate.lot} · Tray ${candidate.tray} · ${candidate.operator}. Valid offset saved; noise is incomplete.`;
    if (!selectedRecovery) return;
    const saved = selectedRecovery;
    $('recoveryBatch').textContent = `${saved.lot} / ${saved.tray}`;
    $('recoveryOperator').textContent = saved.operator;
    $('recoveryDate').textContent = recoveryDate(saved.created_at);
    $('recoverySource').textContent = saved.simulate ? 'SIMULATION · generated data' : 'ACTUAL DETECTORS · physical DAQ';
    $('recoveryTiming').textContent = `${fmt(saved.dc_recorded_seconds, 1, ' s')} DC retained · ${fmt(saved.recorded_noise_seconds, 2, ' / 100 s')} noise retained for diagnostics. The fixture returned to DC: allow 90 seconds to settle in noise mode, then 100 seconds to record. About 3 min 10 s plus connecting and saving.`;
    const stale = !noiseRecovery || noiseRecovery.id !== saved.id;
    const issue = recoveryError || (stale ? 'This recovery is no longer available. Return to Main setup to refresh the available check.' : '') ||
      (state.restart_required ? 'Restart the app after resolving the DAQ shutdown error before retrying.' : '') ||
      (!connected ? 'The tester is offline. Reconnecting before a retry can begin…' : '');
    $('recoveryError').textContent = issue;
    $('recoveryError').hidden = !issue;
    $('recoveryConfirm').disabled = Boolean(state.busy || requestPending || stale);
    $('recoveryStart').disabled = Boolean(locked() || !connected || stale || state.restart_required || !$('recoveryConfirm').checked);
  }

  function paint() {
    const result = state.result;
    const busy = Boolean(state.busy || requestPending);
    document.body.dataset.busy = String(busy);
    const simulation = Boolean(busy ? state.simulate : result ? result.simulated : $('sourceInput').value === 'demo');
    $('footerSource').textContent = simulation ? 'SIMULATION · generated data · separate result files' : 'Actual detectors · data saved automatically';
    $('phaseText').textContent = `${state.phase || 'Ready'}${simulation ? ' · SIMULATION' : ''}`;
    $('statusText').textContent = !connected && !reportMode
      ? 'Connection lost. A test may still be running. Try Stop, or use physical STOP if needed. Reconnecting…'
      : stopError || actionError || (stopQueued || stopPending ? 'Stopping acquisition and saving available data…' : '') || state.message ||
        (result ? result.data_complete ? 'All measurements saved. Select a detector to inspect its traces.' :
          (result.errors || []).join(' · ') || 'The check is incomplete.' : 'Ready when you are. Press Start to begin.');
    $('timeNote').textContent = reportMode ? 'Saved report' : simulation ? 'Demo · accelerated waiting' : busy ? 'Settling and recording automatically' : 'About 4 min 50 s';
    $('runProgress').value = finite(state.progress) ? Math.max(0, Math.min(1, state.progress)) : 0;
    const runState = $('runState');
    runState.dataset.state = !connected ? 'fail' : busy ? 'testing' : result ? result.data_complete ? 'review' : 'fail' : 'ready';
    runState.querySelector('strong').textContent = !connected ? 'OFFLINE' : busy ? 'TESTING' : result ? result.data_complete ? 'COMPLETE' : 'INCOMPLETE' : 'READY';
    for (const el of document.querySelectorAll('.field input,.field select')) el.disabled = busy;
    $('startButton').disabled = Boolean(reportMode || busy || result || screen !== 'test' || state.restart_required || !connected);
    $('stopButton').disabled = reportMode || (!busy && connected);
    $('completionActions').hidden = reportMode || busy || !result;
    $('nextBatchButton').disabled = locked() || Boolean(state.restart_required);
    $('homeButton').disabled = locked();
    $('returnSetupButton').hidden = reportMode || Boolean(result);
    $('returnSetupButton').disabled = locked();
    $('prepContinue').disabled = busy;
    const identity = result || state.run_metadata || {model: $('modelInput').value,
      lot: $('lotInput').value, operator: $('operatorInput').value, tray: $('trayInput').value};
    $('batchContext').textContent = `Model ${identity.model || '40623'} · ${identity.operator || 'Technician'} · Batch ${identity.lot || '—'} · Tray ${identity.tray || '1'}`;
    const phase = (state.phase || '').toLowerCase();
    const step = phase.includes('noise') ? phase.includes('settl') || phase.includes('switch') ? 2 : 3 : phase.includes('dc') ? phase.includes('settl') ? 0 : 1 : phase.includes('sav') || phase.includes('return') ? 4 : -1;
    [...document.querySelectorAll('.sequence span')].forEach((el, i) => el.classList.toggle('active', state.busy && i === step));
    paintTray();
    paintDetail();
    paintRecovery();
  }

  function paintSetupNote() {
    $('setupNote').textContent = $('sourceInput').value === 'demo'
      ? 'Demo rehearsal: synthetic signals, accelerated waiting, and no connection to the DAQ. Leave the hardware alone for this one.'
      : 'Also check cooling and supplies, prepare the vacuum, and close other DAQ apps. Software Stop ends acquisition; hardware power stays on.';
  }

  async function action(path, body) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: AbortSignal.timeout(10000)});
    let payload;
    try {payload = await response.json();} catch {payload = {};}
    if (!response.ok) {
      const error = new Error(payload.error || payload.message || `Request failed (${response.status})`);
      error.httpStatus = response.status;
      throw error;
    }
    return payload;
  }

  function restoreIdentity(next) {
    const identity = next.run_metadata || next.result;
    if (!identity) return;
    $('modelInput').value = identity.model || '40623';
    $('lotInput').value = identity.lot || '';
    $('operatorInput').value = identity.operator || '';
    $('trayInput').value = identity.tray || 1;
    $('sourceInput').value = next.simulate ? 'demo' : 'real';
    paintSetupNote();
  }

  async function poll() {
    const version = stateVersion;
    try {
      const response = await fetch('api/state', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error('The local tester is not responding.');
      const next = await response.json();
      // A poll may have read the previous run just before Start was accepted.
      // Never let that delayed response overwrite a new run or a new batch.
      if (version !== stateVersion || requestPending) return;
      connected = true;
      if (!initialSourceSet) {
        $('sourceInput').value = next.simulate ? 'demo' : 'real';
        initialSourceSet = true;
        paintSetupNote();
      }
      // Only snapshots from this poll generation can change recovery eligibility.
      // Keep it outside state so next-batch navigation does not erase it.
      noiseRecovery = next.noise_recovery || null;
      const differentRun = next.run_id && next.run_id !== state.run_id && next.run_id !== dismissedRun;
      const finished = state.busy && !next.busy && next.result;
      if (next.busy || (next.result && next.run_id !== dismissedRun)) {
        if (differentRun || screen === 'setup' || screen === 'prep') restoreIdentity(next);
        if (differentRun) {
          selected = null;
          $('detailSection').hidden = true;
        }
        state = next;
        if (next.busy || differentRun || screen === 'setup' || screen === 'prep') {
          if (screen !== 'test') showScreen('test', 'trayTitle');
        }
        if (finished && ['startButton', 'stopButton'].includes(document.activeElement?.id)) $('trayTitle').focus({preventScroll: true});
      } else if (next.run_id && next.run_id === dismissedRun) {
        state = {...blankState(), restart_required: next.restart_required};
      } else state = next;
      if (!state.busy) {stopQueued = false; stopPending = false; stopError = '';}
      if (state.busy || state.result) actionError = '';
      paint();
    } catch (error) {
      if (version !== stateVersion || requestPending) return;
      connected = false;
      paint();
    } finally {window.setTimeout(poll, 700);}
  }

  async function stopRun() {
    if (reportMode) return;
    stopQueued = true;
    actionError = '';
    stopError = '';
    paint();
    // If Start is still in flight, send Stop after it is acknowledged so the
    // cancellation cannot arrive at the server before there is a worker.
    if (requestPending || stopPending) return;
    stopPending = true;
    try {await action('api/stop', {});}
    catch (error) {
      stopError = `Stop request could not reach the tester: ${error.message}. Use physical STOP if needed.`;
      stopQueued = false;
    } finally {stopPending = false; paint();}
  }

  async function startRun() {
    if (reportMode || locked() || state.result || state.restart_required || !connected || screen !== 'test') return;
    if (!validSetup()) {showScreen('setup', 'setupTitle'); return;}
    requestPending = true;
    stateVersion++;
    actionError = '';
    stopError = '';
    stopQueued = false;
    selected = null;
    $('detailSection').hidden = true;
    state = {...blankState(), busy: true, phase: 'Starting', simulate: $('sourceInput').value === 'demo', message: 'Starting the tray check…'};
    paint();
    try {
      await action('api/start', {model: $('modelInput').value, lot: $('lotInput').value.trim(),
        operator: $('operatorInput').value.trim(), tray: Number($('trayInput').value),
        simulate: $('sourceInput').value === 'demo'});
    } catch (error) {
      // A lost response does not prove the worker failed to start. Poll before
      // allowing another Start or leaving the test view.
      connected = false;
      actionError = error.message;
      state.busy = false;
    } finally {
      requestPending = false;
      stateVersion++;
      paint();
      if (stopQueued) await stopRun();
    }
  }

  async function retryNoise() {
    const candidate = selectedRecovery;
    if (reportMode || screen !== 'recovery' || locked() || !connected || state.restart_required ||
        !candidate || candidate.id !== noiseRecovery?.id || !$('recoveryConfirm').checked) return;
    if (!$('recoveryForm').reportValidity()) return;
    const previousState = state, previousDismissed = dismissedRun;
    const previousFields = Object.fromEntries(['modelInput', 'lotInput', 'operatorInput', 'trayInput', 'sourceInput'].map(id => [id, $(id).value]));
    requestPending = true;
    stateVersion++;
    actionError = ''; stopError = ''; recoveryError = ''; stopQueued = false;
    selected = null;
    dismissedRun = state.run_id || dismissedRun;
    restoreIdentity({run_metadata: {...candidate, model: '40623'}, simulate: candidate.simulate});
    initialSourceSet = true;
    state = {...blankState(), busy: true, phase: 'Preparing noise retry', simulate: candidate.simulate,
      run_metadata: {...candidate, model: '40623'}, message: 'Keeping the saved offset and preparing a fresh noise recording…'};
    showScreen('test', 'trayTitle');
    try {
      await action('api/retry-noise', {recovery_id: candidate.id, confirm_unchanged: true});
      selectedRecovery = null;
    } catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500) {
        // A rejected request did not start a worker. Preserve the old display
        // and the operator's draft identity, and require confirmation again.
        state = previousState;
        dismissedRun = previousDismissed;
        for (const [id, value] of Object.entries(previousFields)) $(id).value = value;
        paintSetupNote();
        stopQueued = false;
        recoveryError = error.message;
        $('recoveryConfirm').checked = false;
        showScreen('recovery', 'recoveryTitle');
      } else {
        // A lost acknowledgement is ambiguous. Keep Stop available and wait
        // for state polling rather than permitting a duplicate recovery.
        connected = false;
        actionError = error.message;
        state.busy = false;
      }
    } finally {
      requestPending = false;
      stateVersion++;
      paint();
      if (stopQueued) await stopRun();
    }
  }

  $('controls').addEventListener('submit', event => {
    event.preventDefault();
    if (reportMode || locked() || !validSetup()) return;
    paintSetupNote();
    showScreen('prep', 'prepTitle');
  });
  $('prepContinue').addEventListener('click', () => {
    if (!reportMode && !locked()) showScreen('test', 'trayTitle');
  });
  $('prepBack').addEventListener('click', mainSetup);
  $('startButton').addEventListener('click', startRun);
  $('stopButton').addEventListener('click', stopRun);
  $('nextBatchButton').addEventListener('click', nextBatch);
  $('homeButton').addEventListener('click', mainSetup);
  $('returnSetupButton').addEventListener('click', mainSetup);
  $('recoverSavedButton').addEventListener('click', openRecovery);
  $('retryNoiseButton').addEventListener('click', openRecovery);
  $('recoveryCancel').addEventListener('click', mainSetup);
  $('recoveryConfirm').addEventListener('change', paintRecovery);
  $('recoveryForm').addEventListener('submit', event => {event.preventDefault(); retryNoise();});
  $('batchCancel').addEventListener('click', () => {if (!locked()) showScreen('test', 'trayTitle');});
  $('batchForm').addEventListener('submit', event => {
    event.preventDefault();
    if (reportMode || locked() || state.restart_required) return;
    $('newBatchInput').value = $('newBatchInput').value.trim();
    if (!$('batchForm').reportValidity()) return;
    const lot = $('newBatchInput').value;
    const tray = lot === $('lotInput').value ? Number($('trayInput').value) + 1 : 1;
    discardDisplayedRun();
    $('lotInput').value = lot;
    $('trayInput').value = tray;
    paintSetupNote();
    showScreen('prep', 'prepTitle');
  });
  $('backToTrayButton').addEventListener('click', () => {
    $('trayTitle').focus({preventScroll: true});
    $('testSection').scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  $('sourceInput').addEventListener('change', () => {initialSourceSet = true; paintSetupNote(); paint();});
  document.addEventListener('keydown', event => {
    if (reportMode || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
    if (event.repeat && ['Enter', 'Escape'].includes(event.key)) {event.preventDefault(); return;}
    if (event.key === 'Escape') {
      if (screen !== 'setup') {event.preventDefault(); mainSetup();}
      return;
    }
    if (event.key !== 'Enter' || locked()) return;
    // Keep normal activation for buttons and dropdowns, and normal form
    // submission for inputs. Page shortcuts act when focus is on page content.
    if (event.target.closest('button,input,select,textarea,a,[contenteditable=true]')) return;
    event.preventDefault();
    if (screen === 'setup') $('controls').requestSubmit();
    else if (screen === 'prep') $('prepContinue').click();
    else if (screen === 'batch') $('batchForm').requestSubmit();
    else if (screen === 'test' && state.result) nextBatch();
  });
  window.addEventListener('pagehide', () => {
    if (!reportMode && (state.busy || requestPending || (!connected && screen === 'test'))) {
      navigator.sendBeacon('api/stop', new Blob(['{}'], {type: 'application/json'}));
    }
  });
  buildTray();
  if (reportMode) {
    const result = window.ELTEC_REPORT;
    document.body.dataset.report = 'true';
    document.title = `Eltec tray report · ${result.lot || 'Run'} · ${result.tray || ''}`;
    state = {busy: false, phase: result.data_complete ? 'Check complete' : 'Check incomplete', progress: result.data_complete ? 1 : 0,
      simulate: result.simulated, result, rows: result.rows || [], message: (result.errors || []).join(' · ')};
    selected = rows().find(row => classification(row)[0] === 'fail')?.position || rows().find(row => row.dc_detail)?.position || '1-1';
    showScreen('test');
  } else {paintSetupNote(); showScreen('setup', 'setupTitle'); poll();}
})();
