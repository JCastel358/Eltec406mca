/* Browser checks for the live diagnostic UI. Uses generated responses, never a DAQ.
 * Set PLAYWRIGHT_MODULE / PLAYWRIGHT_EXECUTABLE as for mod50_workflow_browser.cjs.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const ui = path.resolve(__dirname, '../mod50_ui');
const origin = 'http://eltec-live.test';
const clone = value => JSON.parse(JSON.stringify(value));
const positions = Array.from({length:50}, (_,i)=>`${Math.floor(i/10)+1}-${i%10+1}`);
const idle = () => ({busy:false, check_busy:false, simulate:true, restart_required:false});
function active(overrides={}) {
  return {busy:true, check_busy:false, simulate:true, restart_required:false, box_side:'bottom',
    phase:'Live noise', message:'Ready at 5.0 seconds · example recording has 55.0 seconds left',
    elapsed_s:10, first_ready_s:5, recording_start_s:5, recording_remaining_s:55, deadline_fallback:false,
    settling:{positions:['1-1','1-2'], stable_count:1, required_count:2},
    rows:[{position:'1-1', installed:true, state:'ready', mean_v:.002, slope_v_per_s:.0001, baseline_span_v:.0008, trend_threshold_v_per_s:.0008, clipped:false,
      trace:{time_s:[0,2,4,6,8,10], voltage_v:[0,.005,-.003,.002,-.001,.002]}},
    {position:'1-2', installed:true, state:'clipped', mean_v:.999, slope_v_per_s:null, baseline_span_v:null, clipped:true,
      trace:{time_s:[8,9,10],voltage_v:[.999,.999,.999]}},
    {position:'5-10', installed:false, state:'excluded', mean_v:0, slope_v_per_s:null, baseline_span_v:null, clipped:false,
      trace:{time_s:[8,9,10],voltage_v:[0,0,0]}}], ...overrides};
}
async function fixture(browser,{initial=idle(), entry='live.html', context=null, failStop=false}={}) {
  const browserContext = await browser.newContext({viewport:{width:1366,height:900}});
  const page = await browserContext.newPage(); page.setDefaultTimeout(5000);
  const errors=[], requests=[]; let current=clone(initial), releaseStart=null, startBlocked=false;
  page.on('pageerror',e=>errors.push(e.message));
  if(context)await page.addInitScript(value=>sessionStorage.setItem('eltecLiveContext',JSON.stringify(value)),context);
  await page.route('**/*', async route=>{
    const request=route.request(), resource=new URL(request.url()).pathname.slice(1)||'index.html';
    if(resource==='api/live-state')return route.fulfill({json:current});
    if(resource==='api/state')return route.fulfill({json:{...idle(),phase:'Ready',rows:[],result:null}});
    if(resource==='api/close')return route.fulfill({json:{ok:true}});
    if(resource==='api/live-start'){
      const payload=request.postDataJSON();requests.push({action:'start',payload});
      if(startBlocked)await new Promise(resolve=>{releaseStart=resolve;});
      current=active({simulate:payload.simulate,box_side:payload.box_side,settling:{positions:payload.installed_positions,stable_count:0,required_count:Math.ceil(payload.installed_positions.length*.8)}});
      return route.fulfill({json:{ok:true}});
    }
    if(resource==='api/live-stop'){
      requests.push({action:'stop'});
      if(failStop)return route.fulfill({status:503,json:{error:'Stop failed; retry'}});
      current={...current,busy:false,phase:'Live viewer stopped',message:'No detector results were saved.',stopped:true};
      return route.fulfill({json:{ok:true}});
    }
    assert(['live.html','live.js','live.css','styles.css','index.html','app.js','battery_warning.js','assets/eltec-logo-transparent.webp'].includes(resource),`Unexpected ${resource}`);
    return route.fulfill({body:fs.readFileSync(path.join(ui,resource)),contentType:resource.endsWith('.js')?'text/javascript':resource.endsWith('.css')?'text/css':resource.endsWith('.webp')?'image/webp':'text/html'});
  });
  await page.goto(`${origin}/${entry}`);
  if(entry==='live.html')await page.waitForFunction(()=>document.querySelector('#livePhase').textContent!=='Viewer disconnected');
  return {page,requests,setState:value=>{current=clone(value);},holdStart:()=>{startBlocked=true;},release:()=>releaseStart?.(),
    close:async()=>{releaseStart?.();await browserContext.close();assert.deepEqual(errors,[]);}};
}
const cases=[
  ['live battery advice stays dismissed when returning to the tray and back',async browser=>{
    const warning={id:'live-battery-warning',consecutive_failures:2};
    const initial={...idle(),simulate:false,battery_warning:warning,phase:'Live viewer error',errors:['Cards present but READY inactive']};
    const t=await fixture(browser,{initial});
    try{
      const p=t.page,dialog=p.locator('#batteryWarningDialog');
      await dialog.waitFor({state:'visible'});
      assert.match(await dialog.textContent(),/Check the voltage of both battery packs/);
      await p.locator('#batteryWarningDismiss').click();
      await p.locator('#backLink').click();
      await p.waitForURL('**/index.html');
      await p.waitForFunction(()=>document.querySelector('#runState strong').textContent!=='OFFLINE');
      await p.locator('#sourceInput').selectOption('real');
      await p.locator('#liveViewerButton').click();
      await p.waitForURL('**/live.html');
      await p.waitForFunction(()=>document.querySelector('#livePhase').textContent==='Live viewer error');
      assert.equal(await dialog.isVisible(),false);
      t.setState({...initial,battery_warning:{id:'another-live-streak',consecutive_failures:2}});
      await dialog.waitFor({state:'visible'});
      assert.deepEqual(t.requests,[],'Advice must not issue acquisition actions');
    }finally{await t.close();}
  }],
  ['main setup opens live viewer without batch identity and transfers source and box side',async browser=>{
    const t=await fixture(browser,{entry:'index.html'});
    try{
      await t.page.locator('#sourceInput').selectOption('demo');
      await t.page.locator('#boxSideInput').selectOption('bottom');
      await t.page.locator('#liveViewerButton').click();
      await t.page.waitForURL('**/live.html');
      assert.equal(await t.page.locator('#liveSource').inputValue(),'demo');
      assert.equal(await t.page.locator('#liveSide').inputValue(),'bottom');
      assert.equal(await t.page.locator('#liveGrid strong').first().textContent(),'6-1');
      assert.equal(t.requests.length,0);
    }finally{await t.close();}
  }],
  ['rows preserve mixed state and send only loaded wiring positions',async browser=>{
    const t=await fixture(browser);
    try{
      const p=t.page;await p.locator('#liveSide').selectOption('bottom');await p.locator('#livePopulationSummary').click();
      await p.locator('#liveNone').click();assert(await p.locator('#liveStart').isDisabled());
      await p.getByLabel('Row 9',{exact:true}).check();
      await p.getByLabel('9-4',{exact:true}).uncheck();
      assert.equal(await p.getByLabel('Row 9',{exact:true}).evaluate(el=>el.indeterminate),true);
      assert.equal(await p.locator('#livePopulationSummary').textContent(),'Loaded slots: 9 of 50');
      await p.getByLabel('Row 9',{exact:true}).check();
      await p.locator('#liveStart').click();
      await p.waitForFunction(()=>document.querySelector('#livePhase').textContent==='Live noise');
      assert.deepEqual(t.requests[0].payload.installed_positions,Array.from({length:10},(_,i)=>`4-${i+1}`));
      assert.equal(t.requests[0].payload.box_side,'bottom');
      assert.equal(await p.locator('.population-row-toggle input:disabled').count(),5);
    }finally{await t.close();}
  }],
  ['actual source requires readiness and pending start locks controls until Stop releases them',async browser=>{
    const t=await fixture(browser,{initial:{...idle(),simulate:false}});
    try{
      const p=t.page;assert(await p.locator('#liveStart').isDisabled());await p.locator('#liveReady').check();
      const started=p.waitForRequest(request=>request.url().endsWith('/api/live-start'));
      t.holdStart();await p.locator('#liveStart').click();await started;
      assert(await p.locator('#liveSource').isDisabled());assert(await p.locator('#liveStart').isDisabled());
      assert.equal(await p.locator('#backLink').getAttribute('aria-disabled'),'true');
      await p.waitForFunction(()=>document.querySelector('#liveSource').disabled);
      assert.equal(t.requests[0].payload.fixture_ready,true);assert.equal(t.requests[0].payload.simulate,false);
      t.release();await p.waitForFunction(()=>!document.querySelector('#liveStop').disabled);
      await p.locator('#liveStop').click();await p.waitForFunction(()=>document.querySelector('#livePhase').textContent==='Live viewer stopped');
      assert.equal(await p.locator('#liveSource').isDisabled(),false);assert(await p.locator('#liveStop').isDisabled());
      assert.deepEqual(t.requests.map(r=>r.action),['start','stop']);
    }finally{await t.close();}
  }],
  ['running state restores source, bottom labels, waveform units, excluded slots and fallback origin',async browser=>{
    const t=await fixture(browser,{initial:active()});
    try{
      const p=t.page;await p.waitForFunction(()=>document.querySelector('#liveChart svg'));
      assert.equal(await p.locator('#liveSource').inputValue(),'demo');assert.equal(await p.locator('#liveSide').inputValue(),'bottom');
      assert.equal(await p.locator('#livePopulationSummary').textContent(),'Loaded slots: 2 of 50');
      assert.match(await p.locator('#liveMetrics').textContent(),/2\.00 mV.*0\.100 mV\/s.*Trend threshold0\.800 mV\/s/);
      assert.equal(await p.locator('#liveChart polyline').count(),1);assert.equal(await p.locator('#liveTrigger').textContent(),'5.0 s');
      assert.equal(await p.locator('#liveGrid button[data-position="1-2"]').getAttribute('data-state'),'clipped');
      await p.locator('#liveGrid button[data-position="1-2"]').click();
      assert.match(await p.locator('#liveMetrics').textContent(),/Slope— mV\/sTrend threshold— mV\/s/);
      await p.locator('#liveGrid button[data-position="5-10"]').click();assert.equal(await p.locator('#liveDetailState').textContent(),'Not selected');
      t.setState(active({first_ready_s:100,recording_start_s:90,deadline_fallback:true,recording_remaining_s:0,elapsed_s:150}));
      await p.waitForFunction(()=>document.querySelector('#liveTrigger').textContent.includes('fallback'));
      assert.equal(await p.locator('#liveRecording').textContent(),'Complete');assert(await p.locator('#liveStart').isDisabled());
    }finally{await t.close();}
  }],
  ['changing source or box side clears stopped samples until a new acquisition begins',async browser=>{
    const t=await fixture(browser,{initial:active()});
    try{
      const p=t.page;await p.waitForFunction(()=>document.querySelector('#liveChart svg'));
      await p.locator('#liveStop').click();await p.waitForFunction(()=>document.querySelector('#livePhase').textContent==='Live viewer stopped');
      await p.locator('#liveSource').selectOption('real');await p.locator('#liveSide').selectOption('top');
      assert.equal(await p.locator('#liveChart svg').count(),0);
      assert.equal(await p.locator('#liveSourceNote').textContent(),'ACTUAL DETECTORS');
      assert.equal(await p.locator('#liveTrigger').textContent(),'Waiting');
      assert.equal(await p.locator('#livePhase').textContent(),'Ready to view');
      assert.equal(await p.locator('#liveDetailTitle').textContent(),'Slot 1-1');
      assert.match(await p.locator('#liveMetrics').textContent(),/Baseline · 5 s— mV/);
      await p.waitForResponse(response=>response.url().endsWith('/api/live-state'));
      assert.equal(await p.locator('#liveChart svg').count(),0,'Old stopped samples must not return on polling');
      await p.locator('#liveReady').check();await p.locator('#liveStart').click();
      await p.waitForFunction(()=>document.querySelector('#liveChart svg'));
      assert.equal(t.requests.at(-1).payload.simulate,false);
      assert.equal(t.requests.at(-1).payload.box_side,'top');
    }finally{await t.close();}
  }],
  ['reopening a stopped preview preserves provenance and a new setup context clears it',async browser=>{
    const initial=active({busy:false,stopped:true,phase:'Live viewer stopped'});
    const t=await fixture(browser,{initial});
    try{
      await t.page.waitForFunction(()=>document.querySelector('#liveChart svg'));
      assert.equal(await t.page.locator('#liveSide').inputValue(),'bottom');
      assert.equal(await t.page.locator('#liveDetailTitle').textContent(),'Slot 6-1');
      assert.equal(await t.page.locator('#livePopulationSummary').textContent(),'Loaded slots: 2 of 50');
    }finally{await t.close();}
    const fresh=await fixture(browser,{initial,context:{simulate:false,box_side:'top',installed_positions:['3-1']}});
    try{
      assert.equal(await fresh.page.locator('#liveChart svg').count(),0);
      assert.equal(await fresh.page.locator('#liveSource').inputValue(),'real');
      assert.equal(await fresh.page.locator('#livePopulationSummary').textContent(),'Loaded slots: 1 of 50');
    }finally{await fresh.close();}
  }],
  ['tray ownership and driver restart block Start; Stop failure stays visible',async browser=>{
    const t=await fixture(browser,{initial:{...idle(),check_busy:true}});
    try{
      const p=t.page;assert(await p.locator('#liveStart').isDisabled());assert.equal(await p.locator('#livePhase').textContent(),'Tray check running');
      t.setState({...idle(),restart_required:true});await p.waitForFunction(()=>!document.querySelector('#liveSource').disabled);
      assert(await p.locator('#liveStart').isDisabled());
    }finally{await t.close();}
    const s=await fixture(browser,{initial:active(),failStop:true});
    try{await s.page.locator('#liveStop').click();await s.page.waitForFunction(()=>document.querySelector('#liveError').textContent.includes('Stop failed'));assert.equal(await s.page.locator('#liveStop').isDisabled(),false);}
    finally{await s.close();}
  }],
  ['desktop and mobile layouts contain the controls, expanded rows and waveform',async browser=>{
    const t=await fixture(browser,{initial:active()});
    try{
      const p=t.page;await p.waitForFunction(()=>document.querySelector('#liveChart svg'));await p.locator('#livePopulationSummary').click();
      for(const [name,width,height] of [['desktop',1366,900],['mobile',390,844]]){
        await p.setViewportSize({width,height});
        const extent=await p.evaluate(()=>({page:document.documentElement.scrollWidth,window:innerWidth}));
        assert(extent.page<=extent.window+1,`${name} has horizontal overflow: ${JSON.stringify(extent)}`);
        assert.equal(await p.locator('.population-row-toggle').count(),5);
        if(process.env.ELTEC_SCREENSHOT_DIR)await p.screenshot({path:path.join(process.env.ELTEC_SCREENSHOT_DIR,`live-${name}.png`),fullPage:true});
      }
    }finally{await t.close();}
  }]
];
(async()=>{
  const browser=await chromium.launch({headless:true,...process.env.PLAYWRIGHT_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_EXECUTABLE}:{}});
  let failures=0;
  try{for(const [name,run] of cases){try{await run(browser);console.log(`PASS ${name}`);}catch(e){failures++;console.error(`FAIL ${name}\n${e.stack}`);}}}
  finally{await browser.close();}
  console.log(`${cases.length-failures}/${cases.length} live browser cases passed`);process.exitCode=failures?1:0;
})();
