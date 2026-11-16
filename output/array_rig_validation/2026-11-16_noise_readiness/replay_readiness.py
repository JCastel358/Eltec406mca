import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

folder = Path(__file__).parent
sys.path.insert(0, str(folder.parents[2] / 'array_rig' / 'm40623'))
import mod50_hardware as hw
from mod50_settling import NoiseSettlingMonitor
spec = importlib.util.spec_from_file_location('old_settling', folder / 'baseline-v1-settling.py')
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
source = folder / 'baseline-before-raw.npz'
with np.load(source, allow_pickle=False) as data:
    raw = data['converter_counts']
    meta = json.loads(str(data['metadata_json']))
rate = meta['sample_rate_hz']
codes = raw[:, hw.MAPPING[0], 1:]
values = hw.daq.counts_to_volts(codes.mean(axis=1), hw.NOISE_RANGE)[None, :]
clips = ((codes <= hw.RAIL_GUARD_COUNTS) | (codes >= 65535 - hw.RAIL_GUARD_COUNTS)).any(axis=1)[None, :]
del raw
old_monitor = old.NoiseSettlingMonitor(rate, hw.daq.lsb_volts(hw.NOISE_RANGE), ['1-1'])
new_monitor = NoiseSettlingMonitor(rate, hw.daq.lsb_volts(hw.NOISE_RANGE), ['1-1'])
rows, old_first = [], None
for index in range(0, values.shape[1] - round(rate) + 1, round(rate)):
    y, clip = values[:, index:index+round(rate)], clips[:, index:index+round(rate)]
    old_monitor.feed(y, clip, stop_when_ready=False)
    new_monitor.feed(y, clip, stop_when_ready=False)
    if old_monitor.complete and old_first is None:
        old_first = old_monitor.samples / rate
    rows.append({'seconds':new_monitor.samples / rate, 'old':old_monitor.channels[0], 'new':new_monitor.channels[0]})
report = {'source':source.name, 'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
    'sample_rate_hz':rate, 'samples':values.shape[1], 'simulated':meta['simulate'],
    'source_errors':meta['state']['errors'], 'source_diagnostics':meta['state']['diagnostics'],
    'old_first_ready_s':old_first, 'new_first_ready_s':new_monitor.first_ready_s,
    'old_ready_windows':sum(r['old']['stable'] for r in rows),
    'new_ready_windows':sum(r['new']['stable'] for r in rows), 'windows':len(rows), 'rows':rows}
(folder / 'replay-comparison.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
t=np.array([r['seconds'] for r in rows]); center=np.array([r['new']['mean_v'] for r in rows])*1000
fig, ax=plt.subplots(3,1,figsize=(12,8),sharex=True,gridspec_kw={'height_ratios':[2,1.3,1]})
ax[0].plot(np.arange(values.shape[1])[::10]/rate,values[0,::10]*1000,color='#b1c0d7',lw=.5,label='Noise waveform (display decimated)')
ax[0].plot(t,center,color='#153c70',lw=1.8,label='Five-second average')
ax[0].axhspan(-10,10,color='#2d936c',alpha=.10,label='Baseline reference ±10 mV')
ax[0].set_ylabel('DAQ output (mV)');ax[0].legend(loc='upper right',fontsize=8,ncol=3)
ax[0].set_title('Actual top slot 1-1: centered noise was mistaken for startup drift',loc='left',fontweight='bold')
ax[1].plot(t,[r['new']['slope_v_per_s']*1000 for r in rows],color='#4477aa',label='Fitted slope')
threshold=np.array([r['new']['trend_threshold_v_per_s'] for r in rows])*1000
ax[1].fill_between(t,-threshold,threshold,color='#88c999',alpha=.28,label='Variation-based trend allowance')
ax[1].axhline(.25,color='#b6454c',ls='--',lw=1,label='Previous ±0.25 mV/s threshold');ax[1].axhline(-.25,color='#b6454c',ls='--',lw=1)
ax[1].set_ylabel('Slope (mV/s)');ax[1].legend(loc='upper right',fontsize=8,ncol=3)
ax[2].step(t,np.array([r['old']['stable'] for r in rows])*.3+1,where='post',color='#b6454c',label='Previous rule')
ax[2].step(t,np.array([r['new']['stable'] for r in rows])*.3,where='post',color='#27805b',label='Revised rule')
ax[2].set_yticks([0,.3,1,1.3],['Wait','Ready','Wait','Ready']);ax[2].set_ylabel('Readiness');ax[2].set_xlabel('Seconds after noise stream starts')
ax[2].axvline(5,color='#27805b',ls=':',label='Revised first ready: 5 s');ax[2].axvline(90,color='#b6454c',ls=':',label='Previous fallback: 90 s')
ax[2].legend(loc='upper right',fontsize=8,ncol=2)
for axis in ax: axis.grid(alpha=.15);axis.set_xlim(0,t[-1])
fig.tight_layout();fig.savefig(folder/'readiness-comparison.png',dpi=160);plt.close(fig)
print(json.dumps({k:v for k,v in report.items() if k not in ('rows','source_diagnostics')},indent=2))
