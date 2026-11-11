/* Optional browser regression checks; no DAQ, server process, or results files.
 *
 * Install/use Playwright separately, then run:
 *   node array_rig/m40623/tests/mod50_workflow_browser.cjs
 * PLAYWRIGHT_MODULE can name a bundled playwright-core module directory.
 * PLAYWRIGHT_EXECUTABLE can name a locally installed Chromium/Edge executable.
 * This script is intentionally separate from the Python unittest runner.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const ui = path.resolve(__dirname, '../mod50_ui');
const origin = 'http://eltec-workflow.test';
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
};
const gate = () => ({arrived: deferred(), release: deferred()});
async function bounded(promise, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), 5000);
    })]);
  } finally {clearTimeout(timer);}
}

function snapshot({id = null, lot = 'BATCH-A', busy = false, complete = false} = {}) {
  const metadata = {model: '40623', lot, operator: 'Test Technician', tray: 1};
  const rows = complete ? [{position: '1-1', dc_v: .2, result: 'CHECK',
    dc_check: 'OUTSIDE_WINDOW_PROVISIONAL', presence: 'SIGNAL', flags: [],
    dc_detail: {time_s: [0, 1, 2], volts: [.19, .2, .21], min_v: .19, max_v: .21}}] : [];
  const result = complete ? {...metadata, data_complete: true, simulated: true, rows, errors: []} : null;
  return {busy, phase: busy ? 'Recording DC' : complete ? 'Check complete' : 'Ready',
    progress: complete ? 1 : busy ? .3 : 0, message: '', simulate: true,
    rows, result, restart_required: false, run_id: id, run_metadata: id ? metadata : null};
}

async function fixture(browser, {initial = snapshot(), report = null, holdInitialPoll = false, stopFailure = null, retryFailure = null} = {}) {
  const context = await browser.newContext({viewport: {width: 1366, height: 650}});
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [], requests = [];
  const initialPoll = holdInitialPoll ? gate() : null;
  let current = clone(initial), stateRequests = 0, startGate = null, stateGate = initialPoll;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    const resource = url.pathname.replace(/^\//, '') || 'index.html';
    if (resource === 'api/state') {
      stateRequests++;
      const captured = clone(current);
      if (stateGate) {
        const gate = stateGate;
        stateGate = null;
        gate.arrived.resolve();
        await gate.release.promise;
      }
      return route.fulfill({json: captured});
    }
    if (resource === 'api/start') {
      const payload = request.postDataJSON();
      requests.push({action: 'start', payload});
      if (startGate) {
        const gate = startGate;
        startGate = null;
        gate.arrived.resolve();
        await gate.release.promise;
      }
      current = snapshot({id: `run-${requests.length}`, lot: payload.lot, busy: true});
      current.run_metadata.operator = payload.operator;
      current.run_metadata.tray = payload.tray;
      return route.fulfill({json: {ok: true}});
    }
    if (resource === 'api/stop') {
      requests.push({action: 'stop'});
      if (stopFailure) return route.fulfill({status: 503, json: {error: stopFailure}});
      return route.fulfill({json: {ok: true}});
    }
    if (resource === 'api/retry-noise') {
      const payload = request.postDataJSON();
      requests.push({action: 'retry-noise', payload});
      if (startGate) {
        const gate = startGate;
        startGate = null;
        gate.arrived.resolve();
        await gate.release.promise;
      }
      if (retryFailure) return route.fulfill({status: 400, json: {error: retryFailure}});
      const candidate = current.noise_recovery;
      current = snapshot({id: `retry-${requests.length}`, lot: candidate.lot, busy: true});
      current.simulate = candidate.simulate;
      current.run_metadata.operator = candidate.operator;
      current.run_metadata.tray = candidate.tray;
      return route.fulfill({json: {ok: true}});
    }
    const allowed = ['index.html', 'app.js', 'styles.css', 'assets/eltec-logo-transparent.webp'];
    assert(allowed.includes(resource), `Unexpected network request: ${request.url()}`);
    let body = fs.readFileSync(path.join(ui, resource));
    if (resource === 'index.html' && report) {
      body = body.toString().replace('<script src="app.js"></script>',
        `<script>window.ELTEC_REPORT=${JSON.stringify(report).replace(/</g, '\\u003c')};</script><script src="app.js"></script>`);
    }
    const contentType = resource.endsWith('.js') ? 'text/javascript' : resource.endsWith('.css') ? 'text/css' :
      resource.endsWith('.webp') ? 'image/webp' : 'text/html';
    return route.fulfill({contentType, body});
  });
  await page.goto(origin);
  if (!report && !holdInitialPoll) await page.waitForFunction(() => document.querySelector('#runState strong').textContent !== 'OFFLINE');
  return {
    page, requests, errors, initialPoll,
    get stateRequests() {return stateRequests;},
    setState(value) {current = clone(value);},
    holdNextPoll() {stateGate = gate(); return stateGate;},
    holdStart() {startGate = gate(); return startGate;},
    async close() {await context.close(); assert.deepEqual(errors, [], 'Browser JavaScript errors');}
  };
}

async function expectScreen(page, name) {
  await page.waitForFunction(expected => document.body.dataset.screen === expected, name);
  assert(await page.locator(`#${name}Section`).isVisible(), `${name} screen must be visible`);
}

async function setupToTray(test, lot = 'BATCH-A') {
  const {page} = test;
  await expectScreen(page, 'setup');
  await page.locator('#operatorInput').fill('Test Technician');
  await page.locator('#lotInput').fill(lot);
  await page.locator('#lotInput').press('Enter');
  await expectScreen(page, 'prep');
  await page.keyboard.press('Enter');
  await expectScreen(page, 'test');
}

async function waitForPolls(test, count = 2) {
  const target = test.stateRequests + count;
  const deadline = Date.now() + 5000;
  while (test.stateRequests < target) {
    assert(Date.now() < deadline, 'Timed out waiting for synthetic state polls');
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  // Wait for the last response's browser paint, without depending on sleep.
  await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function expectCompactViewport(page, {tray = false} = {}) {
  const layout = await page.evaluate(() => {
    const rect = element => {
      const r = element.getBoundingClientRect();
      return {x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: r.width, height: r.height};
    };
    return {width: innerWidth, height: innerHeight, scrollHeight: document.documentElement.scrollHeight,
      scrollWidth: document.documentElement.scrollWidth,
      tiles: [...document.querySelectorAll('.sensor-cell')].map(tile => ({
        rect: rect(tile), labels: [...tile.querySelectorAll('.slot-number,.slot-label')].map(rect)
      })),
      controls: ['startButton', 'stopButton', 'runProgress', 'homeButton', 'nextBatchButton'].map(id =>
        ({id, rect: rect(document.getElementById(id))}))};
  });
  const description = `${layout.width}×${layout.height}`;
  assert(layout.scrollHeight <= layout.height + 1, `${description}: vertical overflow ${layout.scrollHeight}`);
  assert(layout.scrollWidth <= layout.width + 1, `${description}: horizontal overflow ${layout.scrollWidth}`);
  if (!tray) return;
  assert.equal(layout.tiles.length, 50);
  const inViewport = (r, label) => {
    assert(r.width > 0 && r.height > 0, `${description}: ${label} must be displayed`);
    assert(r.x >= -1 && r.y >= -1 && r.right <= layout.width + 1 && r.bottom <= layout.height + 1,
      `${description}: ${label} outside viewport: ${JSON.stringify(r)}`);
  };
  for (const [index, tile] of layout.tiles.entries()) {
    inViewport(tile.rect, `detector ${index + 1}`);
    for (const label of tile.labels) {
      assert(label.x >= tile.rect.x - 1 && label.y >= tile.rect.y - 1 &&
        label.right <= tile.rect.right + 1 && label.bottom <= tile.rect.bottom + 1,
      `${description}: detector ${index + 1} text outside tile: ${JSON.stringify({tile: tile.rect, label})}`);
    }
  }
  for (const control of layout.controls) inViewport(control.rect, control.id);
}

const cases = [
  ['saved recovery requires confirmation and preserves identity and pending Stop', async browser => {
    const candidate = {id: 'saved-recovery-1', lot: 'ORIGINAL-BATCH', operator: 'Original Technician', tray: 7,
      simulate: true, created_at: '2026-11-04T12:10:00-05:00', dc_recorded_seconds: 10,
      recorded_noise_seconds: 4.07, requires_settling: true};
    const initial = {...snapshot(), noise_recovery: candidate};
    const test = await fixture(browser, {initial});
    try {
      const {page} = test;
      await page.locator('#recoverSavedButton').waitFor({state: 'visible'});
      await expectCompactViewport(page);
      await page.locator('#operatorInput').fill('Draft Technician');
      await page.locator('#lotInput').fill('DRAFT-BATCH');
      await page.locator('#sourceInput').selectOption('real');
      assert(!(await page.locator('#recoverSavedButton').isVisible()), 'Demo recovery must not appear under actual detectors');
      await page.locator('#sourceInput').selectOption('demo');
      await page.locator('#recoverSavedButton').click();
      await expectScreen(page, 'recovery');
      await expectCompactViewport(page);
      await page.setViewportSize({width: 1280, height: 600});
      await expectCompactViewport(page);
      await page.setViewportSize({width: 1366, height: 650});
      assert((await page.locator('#recoveryBatch').textContent()).includes('ORIGINAL-BATCH / 7'));
      assert.equal(await page.locator('#recoveryOperator').textContent(), 'Original Technician');
      assert((await page.locator('#recoverySource').textContent()).includes('SIMULATION'));
      assert((await page.locator('#recoveryTiming').textContent()).includes('4.07 / 100 s'));
      assert(await page.locator('#recoveryStart').isDisabled());
      await page.keyboard.press('Enter');
      assert.deepEqual(test.requests, [], 'Opening recovery or Enter without confirmation must not start hardware');
      await page.keyboard.press('Escape');
      await expectScreen(page, 'setup');
      assert.equal(await page.locator('#lotInput').inputValue(), 'DRAFT-BATCH');
      assert.equal(await page.locator('#operatorInput').inputValue(), 'Draft Technician');
      await page.locator('#recoverSavedButton').click();
      await page.locator('#recoveryConfirm').check();
      const pending = test.holdStart();
      await page.locator('#recoveryStart').click();
      await bounded(pending.arrived.promise, 'Expected held recovery acknowledgement');
      await expectScreen(page, 'test');
      await page.locator('#stopButton').click();
      assert.deepEqual(test.requests, [{action: 'retry-noise', payload: {recovery_id: candidate.id, confirm_unchanged: true}}]);
      assert.equal(await page.locator('#lotInput').inputValue(), 'ORIGINAL-BATCH');
      assert.equal(await page.locator('#operatorInput').inputValue(), 'Original Technician');
      assert.equal(await page.locator('#trayInput').inputValue(), '7');
      pending.release.resolve();
      await page.waitForResponse(response => response.url().endsWith('/api/stop'));
      assert.deepEqual(test.requests.map(item => item.action), ['retry-noise', 'stop']);
    } finally {await test.close();}
  }],
  ['interrupted noise preserves 46 offset failures and separates 4 acquisition issues', async browser => {
    const current = snapshot({id: 'incomplete', complete: true});
    current.result.data_complete = false;
    current.rows = Array.from({length: 50}, (_, index) => ({position: `${Math.floor(index / 10) + 1}-${index % 10 + 1}`,
      dc_v: index < 4 ? .6 : .2, dc_check: index < 4 ? 'IN_WINDOW_PROVISIONAL' : 'OUTSIDE_WINDOW_PROVISIONAL',
      result: 'INCOMPLETE', presence: 'SIGNAL', flags: index < 4 ? [] : ['DC: outside provisional 0.3-1.2 V window'],
      acquisition_validity: {dc: true, noise: false}, acquisition_errors: ['NOISE: acquisition invalid/incomplete'],
      noise_detail: {time_s: [0, 1, 2], volts: [.001, -.001, .002], valid_acquisition: false}}));
    current.result.rows = current.rows;
    current.noise_recovery = {id: 'incomplete-token', lot: current.result.lot, operator: current.result.operator,
      tray: 1, simulate: true, created_at: '2026-11-04T12:10:00-05:00', dc_recorded_seconds: 10, recorded_noise_seconds: 4.07};
    const test = await fixture(browser, {initial: current});
    try {
      const {page} = test;
      await expectScreen(page, 'test');
      assert.equal(await page.locator('#passCount').textContent(), '4');
      assert.equal(await page.locator('#failCount').textContent(), '46');
      assert.equal(await page.locator('#reviewCount').textContent(), '0');
      assert.equal(await page.locator('.sensor-cell[data-status=incomplete]').count(), 4);
      assert(await page.locator('#retryNoiseButton').isVisible());
      await expectCompactViewport(page, {tray: true});
      await page.setViewportSize({width: 1280, height: 600});
      await expectCompactViewport(page, {tray: true});
      await page.setViewportSize({width: 1366, height: 650});
      await page.locator('.sensor-cell').first().click();
      assert((await page.locator('#detailBadges').textContent()).includes('Noise recording incomplete'));
      assert((await page.locator('#noiseNote').textContent()).includes('Diagnostic excerpt only'));
      test.setState({...current, busy: true, result: null, phase: 'Settling NOISE'});
      await page.waitForFunction(() => document.querySelectorAll('.sensor-cell[data-status=testing]').length === 4);
      assert.equal(await page.locator('.sensor-cell[data-status=fail]').count(), 46);
      assert.equal(await page.locator('.sensor-cell').first().locator('.slot-label').textContent(), 'Noise pending');
      test.setState(current);
      await page.locator('#retryNoiseButton').waitFor({state: 'visible'});
      await page.locator('#retryNoiseButton').click();
      await expectScreen(page, 'recovery');
      await waitForPolls(test);
      await expectScreen(page, 'recovery');
      assert.deepEqual(test.requests, []);
    } finally {await test.close();}
  }],
  ['rejected recovery preserves the setup draft and requires confirmation again', async browser => {
    const candidate = {id: 'reject-token', lot: 'OLD', operator: 'Original', tray: 3, simulate: true,
      created_at: '2026-11-04T12:10:00-05:00', dc_recorded_seconds: 10, recorded_noise_seconds: 4.07};
    const test = await fixture(browser, {initial: {...snapshot(), noise_recovery: candidate}, retryFailure: 'Saved DC hash changed'});
    try {
      const {page} = test;
      await page.locator('#recoverSavedButton').waitFor({state: 'visible'});
      await page.locator('#lotInput').fill('DRAFT');
      await page.locator('#operatorInput').fill('Draft operator');
      await page.locator('#recoverSavedButton').click();
      await page.locator('#recoveryConfirm').check();
      await page.locator('#recoveryStart').click();
      await page.waitForFunction(() => document.querySelector('#recoveryError').textContent.includes('Saved DC hash changed'));
      await expectScreen(page, 'recovery');
      await waitForPolls(test);
      assert.equal(await page.locator('#lotInput').inputValue(), 'DRAFT');
      assert.equal(await page.locator('#operatorInput').inputValue(), 'Draft operator');
      assert(!(await page.locator('#recoveryConfirm').isChecked()));
      assert(await page.locator('#recoveryStart').isDisabled());
      assert.equal(test.requests.length, 1);
    } finally {await test.close();}
  }],
  ['a changed saved recovery cannot reuse the previous confirmation', async browser => {
    const candidate = {id: 'original-token', lot: 'OLD', operator: 'Original', tray: 1, simulate: true,
      created_at: '2026-11-04T12:10:00-05:00', dc_recorded_seconds: 10, recorded_noise_seconds: 4.07};
    const test = await fixture(browser, {initial: {...snapshot(), noise_recovery: candidate}});
    try {
      const {page} = test;
      await page.locator('#recoverSavedButton').waitFor({state: 'visible'});
      await page.locator('#recoverSavedButton').click();
      await page.locator('#recoveryConfirm').check();
      test.setState({...snapshot(), noise_recovery: {...candidate, id: 'different-token', lot: 'DIFFERENT'}});
      await page.waitForFunction(() => document.querySelector('#recoveryError').textContent.includes('no longer available'));
      assert(await page.locator('#recoveryStart').isDisabled());
      assert((await page.locator('#recoveryBatch').textContent()).includes('OLD'), 'The confirmation must still describe the original selection');
      await page.locator('#recoveryForm').dispatchEvent('submit');
      assert.deepEqual(test.requests, []);
    } finally {await test.close();}
  }],
  ['older saved reports distinguish acquisition markers without hiding signal faults', async browser => {
    const report = snapshot({complete: true}).result;
    report.data_complete = false;
    report.rows = [
      {position: '1-1', dc_check: 'IN_WINDOW_PROVISIONAL', result: 'INCOMPLETE', flags: ['NOISE: acquisition invalid/incomplete']},
      {position: '1-2', dc_check: 'IN_WINDOW_PROVISIONAL', result: 'INCOMPLETE', flags: ['NOISE: acquisition invalid/incomplete', 'NOISE: Outside +/-0.8 V noise headroom']}
    ];
    const test = await fixture(browser, {report});
    try {
      assert.equal(await test.page.locator('.sensor-cell').nth(0).getAttribute('data-status'), 'incomplete');
      assert.equal(await test.page.locator('.sensor-cell').nth(1).getAttribute('data-status'), 'fail');
      assert.equal(await test.page.locator('#reviewCount').textContent(), '0');
      assert(!(await test.page.locator('#retryNoiseButton').isVisible()));
      assert.deepEqual(test.requests, []);
    } finally {await test.close();}
  }],
  ['setup, checklist and completed 50-detector tray fit compact desktop viewports', async browser => {
    for (const viewport of [{width: 1366, height: 650}, {width: 1280, height: 600}]) {
      const test = await fixture(browser, {initial: snapshot({id: 'layout-run', complete: true})});
      try {
        await test.page.setViewportSize(viewport);
        await expectScreen(test.page, 'test');
        assert(!(await test.page.locator('#detailSection').isVisible()));
        await expectCompactViewport(test.page, {tray: true});
      } finally {await test.close();}
    }
    const test = await fixture(browser);
    try {
      const {page} = test;
      await expectScreen(page, 'setup');
      await expectCompactViewport(page);
      await page.locator('#operatorInput').fill('Test Technician');
      await page.locator('#lotInput').fill('LAYOUT-BATCH');
      await page.locator('#lotInput').press('Enter');
      await expectScreen(page, 'prep');
      await expectCompactViewport(page);
    } finally {await test.close();}
  }],
  ['setup, checklist, keyboard flow and subsequent batches', async browser => {
    const test = await fixture(browser);
    try {
      const {page} = test;
      await page.keyboard.press('Enter');
      await expectScreen(page, 'setup');
      await page.locator('#operatorInput').fill('Test Technician');
      await page.locator('#operatorInput').press('Enter');
      await expectScreen(page, 'setup');
      await page.locator('#lotInput').fill('BATCH-A');
      await page.locator('#lotInput').press('Enter');
      await expectScreen(page, 'prep');
      await page.locator('#prepTitle').dispatchEvent('keydown', {key: 'Enter', repeat: true, bubbles: true});
      await expectScreen(page, 'prep');
      assert.deepEqual(test.requests, [], 'Setup and readiness must not start acquisition');
      await page.keyboard.press('Enter');
      await expectScreen(page, 'test');
      await page.keyboard.press('Enter');
      assert.deepEqual(test.requests, [], 'Page Enter on idle tray must not start acquisition');
      assert.equal(await page.locator('.sensor-cell').count(), 50);
      await page.locator('#startButton').click();
      await page.waitForFunction(() => document.body.dataset.busy === 'true');
      await page.keyboard.press('Escape');
      await expectScreen(page, 'test');
      assert.equal(test.requests.filter(item => item.action === 'start').length, 1);
      test.setState(snapshot({id: 'run-1', complete: true}));
      await page.waitForFunction(() => !document.querySelector('#completionActions').hidden);
      await page.locator('.sensor-cell').first().click();
      assert(await page.locator('#detailSection').isVisible());
      await page.waitForFunction(() => window.scrollY > 0);
      await page.keyboard.press('Enter');
      await expectScreen(page, 'batch');
      assert.equal(await page.locator('#operatorInput').inputValue(), 'Test Technician');
      assert.equal(await page.locator('#modelInput').inputValue(), '40623');
      await page.locator('#newBatchInput').fill('   ');
      await page.locator('#newBatchInput').press('Enter');
      await expectScreen(page, 'batch');
      await page.locator('#newBatchInput').fill('BATCH-B');
      await page.locator('#newBatchInput').press('Enter');
      await expectScreen(page, 'prep');
      await waitForPolls(test);
      await expectScreen(page, 'prep');
      assert.equal(await page.locator('#lotInput').inputValue(), 'BATCH-B');
      assert.equal(await page.locator('#failCount').textContent(), '0', 'Old result must not repaint under new batch');
      assert(!(await page.locator('#detailSection').isVisible()));
      await page.keyboard.press('Enter');
      await expectScreen(page, 'test');
      await page.keyboard.press('Escape');
      await expectScreen(page, 'setup');
    } finally {await test.close();}
  }],
  ['late old-run poll cannot overwrite a new Start', async browser => {
    const old = snapshot({id: 'old-run', complete: true});
    const test = await fixture(browser, {initial: old});
    try {
      const {page} = test;
      await expectScreen(page, 'test');
      await page.locator('#trayTitle').focus();
      await page.keyboard.press('Enter');
      await expectScreen(page, 'batch');
      await page.locator('#newBatchInput').fill('BATCH-B');
      await page.locator('#newBatchInput').press('Enter');
      await expectScreen(page, 'prep');
      await page.keyboard.press('Enter');
      await expectScreen(page, 'test');
      const stale = test.holdNextPoll();
      await bounded(stale.arrived.promise, 'Expected held state request');
      const startResponse = page.waitForResponse(response => response.url().endsWith('/api/start'));
      await page.locator('#startButton').click();
      await startResponse;
      await page.waitForFunction(() => document.body.dataset.busy === 'true');
      const staleResponse = page.waitForResponse(response => response.url().endsWith('/api/state'));
      stale.release.resolve();
      await staleResponse;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      // Check before the next poll; accepting the stale result and then
      // correcting it 700 ms later would still be a UI state regression.
      assert.equal(await page.locator('#runState strong').textContent(), 'TESTING');
      assert.equal(await page.locator('#failCount').textContent(), '0');
      assert((await page.locator('#batchContext').textContent()).includes('BATCH-B'));
      assert(!(await page.locator('#completionActions').isVisible()));
      await waitForPolls(test, 1);
      assert.equal(await page.locator('#runState strong').textContent(), 'TESTING');
    } finally {await test.close();}
  }],
  ['Stop waits for a pending Start acknowledgement', async browser => {
    const test = await fixture(browser);
    try {
      const {page} = test;
      await setupToTray(test);
      const start = test.holdStart();
      await page.locator('#startButton').click();
      await bounded(start.arrived.promise, 'Expected held Start request');
      await page.locator('#stopButton').click();
      assert.deepEqual(test.requests.map(item => item.action), ['start']);
      await page.keyboard.press('Escape');
      await expectScreen(page, 'test');
      start.release.resolve();
      await page.waitForResponse(response => response.url().endsWith('/api/stop'));
      assert.deepEqual(test.requests.map(item => item.action), ['start', 'stop']);
    } finally {await test.close();}
  }],
  ['operator source selection wins over the initial delayed poll', async browser => {
    const test = await fixture(browser, {holdInitialPoll: true});
    try {
      const {page} = test;
      await bounded(test.initialPoll.arrived.promise, 'Expected initial held state request');
      // The server starts in demo mode, but the operator changes the source
      // before the initial response arrives. Their explicit choice wins.
      await page.locator('#sourceInput').selectOption('demo');
      await page.locator('#sourceInput').selectOption('real');
      test.initialPoll.release.resolve();
      await page.waitForFunction(() => document.querySelector('#runState strong').textContent === 'READY');
      await waitForPolls(test, 1);
      assert.equal(await page.locator('#sourceInput').inputValue(), 'real');
      assert.deepEqual(test.requests, []);
    } finally {await test.close();}
  }],
  ['failed Stop remains visible through subsequent busy polls', async browser => {
    const test = await fixture(browser, {
      initial: snapshot({id: 'busy-run', busy: true}), stopFailure: 'Synthetic stop transport failure'
    });
    try {
      const {page} = test;
      await expectScreen(page, 'test');
      await page.locator('#stopButton').click();
      await page.waitForFunction(() => document.querySelector('#statusText').textContent.includes('Synthetic stop transport failure'));
      await waitForPolls(test);
      assert((await page.locator('#statusText').textContent()).includes('Synthetic stop transport failure'));
      assert.equal(await page.locator('#runState strong').textContent(), 'TESTING');
      assert(await page.locator('#stopButton').isEnabled(), 'A failed Stop must be retryable');
    } finally {await test.close();}
  }],
  ['another run hides old detector details and restores its identity', async browser => {
    const test = await fixture(browser, {initial: snapshot({id: 'old-run', complete: true})});
    try {
      const {page} = test;
      await expectScreen(page, 'test');
      await page.locator('.sensor-cell').first().click();
      assert(await page.locator('#detailSection').isVisible());
      test.setState(snapshot({id: 'other-tab-run', lot: 'OTHER-BATCH', busy: true}));
      await page.waitForFunction(() => document.querySelector('#batchContext').textContent.includes('OTHER-BATCH'));
      assert(!(await page.locator('#detailSection').isVisible()), 'A new run must hide the old selected detail');
      assert.equal(await page.locator('#lotInput').inputValue(), 'OTHER-BATCH');
    } finally {await test.close();}
  }],
  ['saved report remains interactive without polling or acquisition actions', async browser => {
    const test = await fixture(browser, {report: snapshot({id: 'saved', complete: true}).result});
    try {
      const {page} = test;
      await expectScreen(page, 'test');
      assert(await page.locator('#detailSection').isVisible());
      await page.locator('.sensor-cell').nth(49).click();
      assert.equal(await page.locator('#detailTitle').textContent(), 'Detector 5-10');
      await page.keyboard.press('Enter');
      await page.keyboard.press('Escape');
      await expectScreen(page, 'test');
      assert(!(await page.locator('#completionActions').isVisible()));
      assert(!(await page.locator('#setupSection').isVisible()));
      // Longer than one polling interval: offline report must issue no API request.
      await new Promise(resolve => setTimeout(resolve, 800));
      assert.equal(test.stateRequests, 0);
      assert.deepEqual(test.requests, []);
    } finally {await test.close();}
  }]
];

(async () => {
  const options = {headless: true};
  if (process.env.PLAYWRIGHT_EXECUTABLE) options.executablePath = process.env.PLAYWRIGHT_EXECUTABLE;
  const browser = await chromium.launch(options);
  let failures = 0;
  try {
    for (const [name, run] of cases) {
      try {await run(browser); console.log(`PASS ${name}`);}
      catch (error) {failures++; console.error(`FAIL ${name}\n${error.stack}`);}
    }
  } finally {await browser.close();}
  console.log(`${cases.length - failures}/${cases.length} browser workflow cases passed`);
  process.exitCode = failures ? 1 : 0;
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
