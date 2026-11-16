(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const positions = Array.from({length:50}, (_,i) => `${Math.floor(i/10)+1}-${i%10+1}`);
  const labels = {collecting:'Collecting', clipped:'Clipped', rising:'Rising', falling:'Falling', above_zero:'Above zero', below_zero:'Below zero', moving:'Moving', ready:'Ready', unselected:'Not selected'};
  let state = {}, selected = '1-1', pending = false, connected = false, initialized = false, previewReset = false;
  let context = null;
  try {context = JSON.parse(sessionStorage.getItem('eltecLiveContext') || 'null'); sessionStorage.removeItem('eltecLiveContext');} catch (_) {}
  if (context) {$('liveSource').value = context.simulate ? 'demo' : 'real'; $('liveSide').value = context.box_side || 'top';}
  const loaded = () => [...document.querySelectorAll('#liveChoices .population-slot input:checked')].map(input=>input.value);
  const busy = () => Boolean(state.busy || state.check_busy || pending);
  const side = () => state.busy && state.box_side ? state.box_side : $('liveSide').value;
  const label = position => {const [r,c] = position.split('-').map(Number); return `${r+(side()==='bottom'?5:0)}-${c}`;};
  const number = (v, digits=2) => Number.isFinite(v) ? v.toFixed(digits) : '—';
  const millivolts = (v, digits=2) => number(Number.isFinite(v) ? v*1000 : NaN, digits);
  for (let r=1;r<=5;r++) {
    const row=document.createElement('div'); row.className='population-row'; row.dataset.row=r;
    const toggle=document.createElement('label'); toggle.className='population-row-toggle';
    const input=document.createElement('input'); input.type='checkbox'; const text=document.createElement('span'); toggle.append(input,text);row.append(toggle);
    input.addEventListener('change',()=>{if (!busy()) {previewReset=true;for (const slot of row.querySelectorAll('.population-slot input')) slot.checked=input.checked;} paint();});
    for (const position of positions.filter(p=>p.startsWith(`${r}-`))) {
      const item=document.createElement('label'); item.className='population-slot';
      const choice=document.createElement('input');choice.type='checkbox';choice.value=position;choice.checked=!context?.installed_positions || context.installed_positions.includes(position);
      const name=document.createElement('span');item.append(choice,name);row.append(item);choice.addEventListener('change',()=>{previewReset=true;paint();});
    }
    $('liveChoices').append(row);
  }
  for (const position of positions) {
    const button=document.createElement('button');button.type='button';button.dataset.position=position;
    button.append(document.createElement('strong'),document.createElement('small'));
    button.addEventListener('click',()=>{selected=position;paint();});$('liveGrid').append(button);
  }
  function chart(row) {
    const trace=row?.trace, ts=trace?.time_s || [], vs=trace?.voltage_v || [];
    if (ts.length<2 || ts.length!==vs.length) {$('liveChart').innerHTML='<div class="chart-empty">Waiting for live samples</div>';return;}
    const values=vs.map(v=>v*1000), lo=Math.min(-10,...values), hi=Math.max(10,...values), pad=Math.max(1,(hi-lo)*.08);
    const width=Math.max(360,Math.min(1000,$('liveChart').clientWidth||1000)),left=55,right=width-20;
    const min=lo-pad,max=hi+pad,start=ts[0],end=ts.at(-1),x=t=>left+(t-start)/Math.max(.001,end-start)*(right-left),y=v=>205-(v-min)/(max-min)*180;
    const points=values.map((v,i)=>`${x(ts[i]).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
    $('liveChart').innerHTML=`<svg viewBox="0 0 ${width} 240" style="aspect-ratio:${width}/240" role="img" aria-label="Live noise waveform for slot ${label(selected)}"><rect x="${left}" y="25" width="${right-left}" height="180" fill="#f8fafc"/><rect x="${left}" y="${y(10)}" width="${right-left}" height="${y(-10)-y(10)}" fill="#e5f4ed"/><path d="M${left} ${y(0)} H${right}" stroke="#8d9bae" stroke-dasharray="5 5"/><polyline points="${points}" fill="none" stroke="#244a9f" stroke-width="1.4"/><g fill="#64748b" font-size="12"><text x="5" y="35">${number(max,1)}</text><text x="5" y="205">${number(min,1)}</text><text x="${left}" y="228">${number(start,1)} s</text><text x="${right}" y="228" text-anchor="end">${number(end,1)} s</text></g></svg>`;
  }
  function paint() {
    // A stopped recording belongs to its original source, side and population.
    // Hide that preview after setup edits until a new acquisition publishes data.
    const view=previewReset?{}:state;
    const locked=busy(),demo=$('liveSource').value==='demo',chosen=new Set(loaded()),rows=new Map((view.rows||[]).map(row=>[row.position,row]));
    for (const el of document.querySelectorAll('.live-controls input,.live-controls select,#liveAll,#liveNone')) el.disabled=locked;
    $('liveReadyLabel').hidden=demo;
    $('liveStart').disabled=locked || !connected || state.restart_required || !chosen.size || (!demo&&!$('liveReady').checked);
    $('liveStop').disabled=!state.busy || pending;
    $('backLink').setAttribute('aria-disabled',String(locked));
    $('livePopulationSummary').textContent=`Loaded slots: ${chosen.size} of 50`;
    for (const row of $('liveChoices').children) {
      const slots=[...row.querySelectorAll('.population-slot input')],count=slots.filter(i=>i.checked).length,toggle=row.querySelector('.population-row-toggle input');
      toggle.checked=count===10;toggle.indeterminate=count>0&&count<10;
      row.querySelector('.population-row-toggle span').textContent=`Row ${Number(row.dataset.row)+(side()==='bottom'?5:0)}`;
      for (const item of row.querySelectorAll('.population-slot')) item.querySelector('span').textContent=label(item.querySelector('input').value);
    }
    $('liveRange').textContent=side()==='bottom'?'Bottom · rows 6–10':'Top · rows 1–5';
    $('liveSourceNote').textContent=(state.busy?state.simulate:demo)?'GENERATED DEMO · simulated signal':'ACTUAL DETECTORS';
    $('livePhase').textContent=!connected?'Viewer disconnected':state.check_busy?'Tray check running':view.phase||'Ready to view';
    $('liveMessage').textContent=state.check_busy?'Finish or stop the tray check before starting the viewer.':view.message||'Select the loaded slots, then start viewing.';
    $('liveElapsed').textContent=`${number(view.elapsed_s||0,1)} s`;
    const evidence=view.settling||{};
    $('liveCount').textContent=`${evidence.stable_count||0} / ${(evidence.positions||loaded()).length} (need ${evidence.required_count||Math.ceil(chosen.size*.8)})`;
    $('liveTrigger').textContent=Number.isFinite(view.recording_start_s)?`${number(view.recording_start_s,1)} s${view.deadline_fallback?' · fallback':''}`:'Waiting';
    $('liveRecording').textContent=Number.isFinite(view.recording_remaining_s)?view.recording_remaining_s>0?`${number(view.recording_remaining_s,1)} s left`:'Complete':'60 s';
    for (const button of $('liveGrid').children) {
      const p=button.dataset.position,row=rows.get(p),kind=row?.installed===false?'unselected':row?.state||(chosen.has(p)?'collecting':'unselected');
      button.dataset.state=kind;button.setAttribute('aria-pressed',String(p===selected));button.querySelector('strong').textContent=label(p);button.querySelector('small').textContent=labels[kind]||kind;
      button.title=`Slot ${label(p)} · ${labels[kind]||kind}`;
    }
    const row=rows.get(selected);
    $('liveDetailTitle').textContent=`Slot ${label(selected)}`;
    $('liveDetailState').textContent=row?.installed===false?'Not selected':labels[row?.state]||'Waiting for samples';
    $('liveMetrics').replaceChildren();
    for (const [name,value] of [['Baseline · 5 s',`${millivolts(row?.mean_v)} mV`],['Slope',`${millivolts(row?.slope_v_per_s,3)} mV/s`],['Trend threshold',`${millivolts(row?.trend_threshold_v_per_s,3)} mV/s`],['Clipping',row?row.clipped?'Yes':'No':'—']]) {
      const div=document.createElement('div'),span=document.createElement('span'),strong=document.createElement('strong');span.textContent=name;strong.textContent=value;div.append(span,strong);$('liveMetrics').append(div);
    }
    chart(row);
    window.EltecBatteryWarning?.update(state.battery_warning, {busy: locked, enabled: connected && !demo});
  }
  function error(message) {$('liveError').textContent=message;$('liveError').hidden=!message;}
  async function action(route,payload={}) {
    pending=true;error('');paint();
    try {const response=await fetch(route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json();if(!response.ok)throw Error(result.error||'Viewer action failed');await poll(false);}
    catch(e){error(e.message);}finally{pending=false;paint();}
  }
  async function poll(repeat=true) {
    try {const response=await fetch('api/live-state',{cache:'no-store'});if(!response.ok)throw Error('Cannot read viewer state');state=await response.json();connected=true;
      if(!initialized){initialized=true;if(!context){$('liveSource').value=state.simulate?'demo':'real';if(state.box_side)$('liveSide').value=state.box_side;
        if(state.settling?.positions)for(const input of document.querySelectorAll('.population-slot input'))input.checked=state.settling.positions.includes(input.value);
      }else if(!state.busy&&state.rows?.length){previewReset=true;}}
      if(state.busy){previewReset=false;$('liveSource').value=state.simulate?'demo':'real';if(state.box_side)$('liveSide').value=state.box_side;
        if(state.settling?.positions)for(const input of document.querySelectorAll('.population-slot input'))input.checked=state.settling.positions.includes(input.value);}
      if(state.errors?.length)error(state.errors.join(' · '));
    }catch(e){connected=false;error(e.message);}paint();if(repeat)setTimeout(poll,500);
  }
  $('liveStart').addEventListener('click',()=>action('api/live-start',{simulate:$('liveSource').value==='demo',box_side:$('liveSide').value,installed_positions:loaded(),fixture_ready:$('liveReady').checked}));
  $('liveStop').addEventListener('click',()=>action('api/live-stop'));
  $('liveAll').addEventListener('click',()=>{if(!busy()){previewReset=true;for(const input of document.querySelectorAll('.population-slot input'))input.checked=true;}paint();});
  $('liveNone').addEventListener('click',()=>{if(!busy()){previewReset=true;for(const input of document.querySelectorAll('.population-slot input'))input.checked=false;}paint();});
  for (const id of ['liveSource','liveSide'])$(id).addEventListener('change',()=>{previewReset=true;paint();});
  $('liveReady').addEventListener('change',paint);
  $('backLink').addEventListener('click',event=>{if(busy())event.preventDefault();});
  window.addEventListener('resize',paint);
  window.addEventListener('pagehide',()=>navigator.sendBeacon('api/close',new Blob(['{}'],{type:'application/json'})));
  paint();poll();
})();
