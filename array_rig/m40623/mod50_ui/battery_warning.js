/* Shared, advisory-only battery guidance. Never starts or retries acquisition. */
(() => {
  'use strict';
  const storageKey = `eltecBatteryWarning:${new URL('.', window.location.href).pathname}`;
  let dismissed = null, activeId = null;
  try {dismissed = sessionStorage.getItem(storageKey);} catch (_) {}
  const dialog = document.createElement('dialog');
  dialog.id = 'batteryWarningDialog';
  dialog.className = 'operator-dialog battery-warning-dialog';
  dialog.setAttribute('aria-labelledby', 'batteryWarningTitle');
  dialog.setAttribute('aria-describedby', 'batteryWarningReason');
  dialog.innerHTML = `
    <p class="eyebrow">Repeated power / RUN readiness failure</p>
    <h2 id="batteryWarningTitle">Check the rig batteries</h2>
    <p id="batteryWarningReason">Both M25 cards were detected, but the rig repeatedly failed to become ready. Low batteries can cause a brief start followed by shutdown, even after pressing physical RUN.</p>
    <ol>
      <li>Check the voltage of <strong>both battery packs</strong>.</li>
      <li>Press physical STOP, turn main power OFF and disconnect both packs before charging or replacing them.</li>
      <li><strong>Recharge both batteries or replace them with charged packs.</strong> Follow the approved charging procedure.</li>
      <li>Restore DAQ power and cooling, turn main power ON, then press and release physical RUN before starting another check.</li>
    </ol>
    <p class="battery-warning-note">The app does not measure battery voltage. If charged batteries do not resolve this, check main power, physical RUN and the control cable.</p>
    <div class="workflow-actions"><button class="primary" id="batteryWarningDismiss" type="button" autofocus>Understood</button></div>`;
  document.body.append(dialog);
  function dismiss() {
    dismissed = activeId;
    if (dismissed) {try {sessionStorage.setItem(storageKey, dismissed);} catch (_) {}}
    activeId = null;
    dialog.close();
  }
  document.getElementById('batteryWarningDismiss').addEventListener('click', dismiss);
  dialog.addEventListener('cancel', event => {event.preventDefault(); dismiss();});
  window.EltecBatteryWarning = {
    update(warning, {busy = false, enabled = true} = {}) {
      if (!enabled || busy || !warning?.id || warning.consecutive_failures < 2) {
        activeId = null;
        if (dialog.open) dialog.close();
        return;
      }
      if (warning.id === dismissed) return;
      // Do not stack this popup over a technician's existing selection dialog.
      if (!dialog.open && document.querySelector('dialog[open]')) return;
      activeId = warning.id;
      if (!dialog.open) dialog.showModal();
    }
  };
})();
