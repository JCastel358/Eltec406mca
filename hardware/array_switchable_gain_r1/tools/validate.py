"""Independent native ERC/netlist audit and exported-circuit linear analysis.

The numerical model is a linear engineering model, not a TI transistor-level
model and not physical qualification. It includes actual exported R/C topology,
relay states, 1 Mohm DAQ input and optional one-pole finite op-amp gain.
"""
from pathlib import Path
import hashlib
import itertools
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
REP=ROOT/'reports';OUT=ROOT/'exports';PROJ=ROOT/'project'
LIB=ROOT/'libraries'
CLI=Path('C:/Program Files/KiCad/10.0/bin/kicad-cli.exe')
commands=[]
def run(args):
    r=subprocess.run([str(x) for x in args],capture_output=True,text=True,timeout=180)
    commands.append({'argv':[str(x) for x in args],'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
    if r.returncode:raise RuntimeError(r.stdout+'\n'+r.stderr)
def value(s):
    token=s.split()[0]
    m=re.fullmatch(r'(\d+)([RkMmunp])(\d*)',token)
    if not m:raise ValueError(token)
    return float(m[1]+('.'+m[3] if m[3] else ''))*{'R':1,'k':1e3,'M':1e6,'m':1e-3,'u':1e-6,'n':1e-9,'p':1e-12}[m[2]]
def main():
    run([CLI,'sch','erc','--format','json','--severity-all','--exit-code-violations','-o',REP/'erc.json',PROJ/'switchable_gain.kicad_sch'])
    run([CLI,'sch','export','netlist','--format','kicadxml','-o',REP/'netlist.xml',PROJ/'switchable_gain.kicad_sch'])
    run([CLI,'sch','export','pdf','-o',OUT/'switchable_gain_schematic.pdf',PROJ/'switchable_gain.kicad_sch'])
    erc=json.loads((REP/'erc.json').read_text())
    violations=[v for sh in erc['sheets'] for v in sh['violations']]
    assert not violations and not erc['ignored_checks']
    root=ET.parse(REP/'netlist.xml').getroot()
    comps={c.attrib['ref']:c for c in root.find('components')}
    pmap={(p.attrib['ref'],p.attrib['pin']):n.attrib['name'] for n in root.find('nets') for p in n.findall('node')}
    net=lambda ref,p:pmap[(ref,str(p))]
    checks=[]
    def check(name,condition,detail=''):
        checks.append({'name':name,'passed':bool(condition),'detail':detail})
        if not condition:raise AssertionError(name+' '+str(detail))
    check('OPA4192 physical pin map and power',net('U1',4)=='+5VA' and net('U1',11)=='AGND' and comps['U1'].findtext('value')=='OPA4192IDR')
    check('Detector remains on noninverting input',net('J1',1)==net('U1',3) and net('J1',2)=='AGND')
    check('Relay coil polarity and flyback cathode',net('K1',1)=='+5V_COIL' and net('K1',8)==net('Q1',3)==net('D1',2) and net('D1',1)=='+5V_COIL')
    check('Relay pole A NC bypasses feedback resistors',net('K1',3)==net('U1',2) and net('K1',2)==net('U1',1))
    check('Relay pole A NO is deliberately unused',net('K1',4).startswith('unconnected-'))
    check('Relay pole B routes direct or filtered output',net('K1',7)==net('U1',1) and net('K1',5)==net('U1',8) and net('K1',6)==net('R7',1) and net('R7',2)==net('J2',1))
    check('Feedback chain always exists',net('R1',1)==net('U1',2) and net('R5',2)==net('U1',1) and all(net(f'R{i}',2)==net(f'R{i+1}',1) for i in range(1,5)))
    check('AC leg connects from feedback through R then film cap to ground',net('R6',1)==net('U1',2) and net('R6',2)==net('C1',1) and net('C1',2)=='AGND')
    for unit,r1,r2,cf,cg,plus,minus,out in [(2,'R10','R11',['C2','C3'],'C4',5,6,7),(3,'R12','R13',['C5'],'C6',10,9,8)]:
        check(f'Filter {unit-1} is unity feedback',net('U1',minus)==net('U1',out))
        check(f'Filter {unit-1} R/C topology',net(r1,2)==net(r2,1) and net(r2,2)==net('U1',plus)==net(cg,1) and net(cg,2)=='AGND' and all(net(c,1)==net(r1,2) and net(c,2)==net('U1',out) for c in cf))
    check('Filter stages cascade',net('R10',1)==net('U1',1) and net('R12',1)==net('U1',7))
    check('Ground star explicit and singular',set([net('R30',1),net('R30',2)])=={'AGND','PGND'})
    check('Manual and auto command are separate until jumper fitted',net('JP1',1)=='MANUAL_REQ' and net('JP1',2)=='MODE_REQ' and net('JP1',3)=='AUTO_NOISE_REQ' and net('J4',1)=='AUTO_NOISE_REQ')
    check('Gate has default pulldown',net('R22',1)==net('Q1',1)==net('R21',2) and net('R22',2)==net('Q1',2)=='PGND')
    check('Spare section parked in feedback',net('U1',13)==net('U1',14) and net('U1',12)=='PARK_1V25')
    refs=[f'R{i}' for i in range(1,8)]+['R10','R11','R12','R13']+[f'C{i}' for i in range(1,7)]
    vals={r:value(comps[r].findtext('value')) for r in refs}
    passive=[(r,net(r,1),net(r,2),vals[r]) for r in refs]
    gain_in=net('J1',1);gain_out=net('U1',1);daq_out=net('J2',1)
    amps=[(net('U1',o),net('U1',p),net('U1',m)) for o,p,m in [(1,3,2),(7,5,6),(8,10,9)]]
    nodes=sorted(set(x for _,a,b,_ in passive for x in (a,b))|{gain_in,daq_out}|set(x for a in amps for x in a))
    nodes.remove('AGND');idx={n:i for i,n in enumerate(nodes)}
    def mna(f,mode,finite=False,dc_input=1,cap_leak=math.inf):
        s=2j*math.pi*f;N=len(nodes);A=np.zeros((N+4,N+4),complex);z=np.zeros(N+4,complex)
        def yy(a,b,y):
            ia=idx.get(a);ib=idx.get(b)
            if ia is not None:A[ia,ia]+=y
            if ib is not None:A[ib,ib]+=y
            if ia is not None and ib is not None:A[ia,ib]-=y;A[ib,ia]-=y
        for r,a,b,v in passive:yy(a,b,1/v if r[0]=='R' else s*v)
        if math.isfinite(cap_leak):yy(net('C1',1),net('C1',2),1/cap_leak)
        yy(daq_out,'AGND',1/1e6)
        # Manufacturer contact resistance limit is used as a nominal screening value.
        if mode=='buffer':
            yy(net('K1',3),net('K1',2),10)
            yy(net('K1',6),net('K1',7),10)
        else:yy(net('K1',6),net('K1',5),10)
        A[idx[gain_in],N]=1;A[N,idx[gain_in]]=1;z[N]=dc_input
        for i,(out,plus,minus) in enumerate(amps,1):
            row=N+i;A[idx[out],row]=1
            if finite:
                av=1e6/(1+s/(2*math.pi*10)) # explicit A0=120dB, GBP=10MHz screening model
                A[row,idx[out]]=1/av
            A[row,idx[plus]]=-1;A[row,idx[minus]]=1
        sol=np.linalg.solve(A,z)
        return sol[idx[daq_out]]
    def formula(f,v=vals):
        s=2j*np.pi*np.asarray(f)
        rf=sum(v[f'R{i}'] for i in range(1,6))
        h=1+rf/(v['R6']+1/(s*v['C1']))
        for r1,r2,cg,cf in [('R10','R11','C4',['C2','C3']),('R12','R13','C6',['C5'])]:
            h/=1+s*v[cg]*(v[r1]+v[r2])+s*s*v[r1]*v[r2]*v[cg]*sum(v[c] for c in cf)
        return h*1e6/(1e6+v['R7']+.1)
    freqs=[.85,3,5,18,50,100,160,200,500,1000]
    response=[]
    for f in freqs:
        ideal=mna(f,'noise');closed=formula(f);finite=mna(f,'noise',True)
        check(f'Exported circuit agrees with transfer function at {f:g} Hz',abs(ideal-closed)/abs(closed)<1e-8)
        response.append({'frequency_hz':f,'buffer_gain':abs(mna(f,'buffer')),'noise_gain_ideal':abs(ideal),'noise_gain_finite_A':abs(finite),'noise_relative_100_db':20*math.log10(abs(ideal)/100)})
    check('1X sensitivity remains about 500mV p-p through 100Hz',all(abs(mna(f,'buffer')-.9999)<2e-6 for f in [3,50,100]))
    dc={str(v):float(mna(0,'buffer',dc_input=v).real) for v in [.3,.45,.7,.95,1.2]}
    check('0.7V offset and 500mV p-p remain inside 0-5V',0<dc['0.45']<dc['0.95']<5 and abs(dc['0.7']-.7)<.0001)
    check('Nominal useful-band gain is within 1% of 100X at anchors',all(99<abs(mna(f,'noise'))<101 for f in [3,5,18,50,100]))
    check('At least 39dB relative rejection at 500Hz',20*math.log10(abs(mna(500,'noise'))/100)<-39)
    # Exhaustive component endpoints: 9 independent filter parts, aggregate Rf,
    # Rg and C1 -> 4096 corners. These are tolerance corners, not statistical guarantees.
    corner_refs=['R10','R11','R12','R13','C2','C3','C4','C5','C6','Rf','R6','C1']
    cfreq=np.array([3,50,100,160,200,500,1000.])
    corners=[]
    for signs in itertools.product((-1,1),repeat=len(corner_refs)):
        v=vals.copy()
        for ref,sgn in zip(corner_refs,signs):
            if ref=='Rf':
                for k in range(1,6):v[f'R{k}']*=1+sgn*.001
            else:v[ref]*=1+sgn*(.05 if ref=='C1' else .01 if ref[0]=='C' else .001)
        corners.append(np.abs(formula(cfreq,v)))
    ca=np.array(corners)
    corner_table=[{'frequency_hz':float(f),'min_gain':float(lo),'max_gain':float(hi)} for f,lo,hi in zip(cfreq,ca.min(axis=0),ca.max(axis=0))]
    # Linear settling calculation for capacitor recharge while the buffer is selected.
    t=np.linspace(0,25,2501);tau=vals['R6']*vals['C1'];tau_max=tau*1.001*1.05
    residue=.25*np.exp(-t/tau_max)
    # Leakage screening uses the WIMA >=30000 s RC specification at 20C.
    # It is not a guaranteed low-voltage leakage limit for every operating temperature.
    film_r=30000/vals['C1'];noise_dc=float(mna(0,'noise',dc_input=.7,cap_leak=film_r).real)
    budget={'one_relay_nominal_ma':1000*5/237,'fifty_relays_nominal_a':50*5/237,'fifty_relays_5pct_high_and_10pct_low_R_a':50*5.25/(237*.9),'opa_38_quads_typical_ma':152,'opa_38_quads_max_25C_ma':152*1.2,'opa_38_quads_max_over_temperature_ma':152*1.5,'ac_gain_asymptote':1+sum(vals[f'R{i}'] for i in range(1,6))/vals['R6'],'gain_leg_time_constant_s':tau,'gain_leg_highpass_hz':1/(2*math.pi*tau),'C1_room_temperature_spec_R_ohm':film_r,'noise_mode_dc_with_C1_leakage_screening_v':noise_dc,'buffer_dc_v':dc,'sensitivity_500mVpp_buffer_output_vpp':float(.5*abs(mna(3,'buffer'))),'ideal_cap_residue_after_20s_input_v':float(.25*math.exp(-20/tau_max))}
    # Plot the response, not a simulated sensor result.
    ff=np.logspace(-2,4,1600)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11})
    fig,(ax1,ax2)=plt.subplots(2,1,figsize=(10,8),gridspec_kw={'height_ratios':[2,1]},layout='constrained')
    hh=np.abs(formula(ff));ax1.semilogx(ff,20*np.log10(hh),color='#126b78',lw=2,label='Noise mode: calculated nominal')
    ax1.axhline(0,color='#777777',ls='--',label='1X direct follower')
    ax1.axvspan(3,100,color='#97c9c2',alpha=.25,label='3-100 Hz measurement band')
    ax1.scatter([3,50,100,500],20*np.log10(np.abs(formula(np.array([3,50,100,500])))),color='#126b78')
    ax1.set(xlim=(.01,1e4),ylim=(-55,45),ylabel='Voltage gain (dB)',title='Switchable 1X / 100X channel - schematic calculation')
    ax1.grid(True,which='both',alpha=.2);ax1.legend(loc='lower left')
    band=np.linspace(3,100,600);ax2.plot(band,np.abs(formula(band)),color='#126b78',lw=2)
    ax2.set(xlabel='Frequency (Hz)',ylabel='Voltage gain (V/V)',ylim=(98.5,101.5),title='Passband detail; tolerances and measured calibration remain separate')
    ax2.axhline(100,color='#777777',ls='--');ax2.grid(True,alpha=.2)
    fig.savefig(OUT/'calculated_response.png',dpi=180);plt.close(fig)
    with (OUT/'response.csv').open('w') as stream:
        stream.write('frequency_hz,buffer_gain,noise_gain_ideal,noise_gain_finite_A,noise_relative_100_db\n')
        for row in response:stream.write(','.join(str(row[k]) for k in response[0])+'\n')
    report={'status':'PASSED: native ERC, topology and stated linear calculations','erc_violations':len(violations),'erc_ignored_checks':erc['ignored_checks'],'erc_exclusions':0,'checks':checks,'response':response,'component_corner_count':len(corners),'component_corners':corner_table,'budget':budget,'model_limits':['Linear model: not manufacturer SPICE, no saturation/recovery model.','No claim of measured noise floor, dielectric absorption recovery, relay dry-contact noise or cable stability.','C1 has a candidate case/footprint, not a sourcing or mechanical qualification. SW1 is a wired panel switch. Passive supplier selections and signal-capacitor footprints require procurement review. No PCB/DRC/fabrication outputs.','DAQ loading modeled as datasheet 1 Mohm; ADC sampling kickback and cable parasitics require bench verification.'],'commands':commands}
    files=sorted(list(PROJ.glob('*.kicad_sch'))+list(PROJ.glob('*.kicad_pro'))+list(PROJ.glob('*lib-table'))+list(LIB.rglob('*.*')))
    report['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    (REP/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    text=['# Schematic validation','',report['status'],'',f'- Native KiCad ERC: **0 violations, 0 ignored checks, 0 exclusions**.',f'- Exported-connectivity and numerical checks: **{len(checks)} passed**.',f'- Component endpoint combinations evaluated: **{len(corners)}**.','- No PCB exists in this package; PCB DRC and layout validation are not applicable.','','## Calculated response','','| Hz | 1X mode | Noise gain | Relative to 100X |','|---:|---:|---:|---:|']
    text += [f"| {r['frequency_hz']:g} | {r['buffer_gain']:.6f} | {r['noise_gain_ideal']:.4f} | {r['noise_relative_100_db']:.3f} dB |" for r in response]
    text += ['','## Component tolerance endpoints','','R1-R6 and R10-R13: +/-0.1%; filter capacitors +/-1%; C1 +/-5%. The 100-ohm output load correction is nominal. Correlated resistor endpoints conservatively bound the sum Rf. These are component endpoints, not measured production limits.','','| Hz | Minimum gain | Maximum gain |','|---:|---:|---:|']
    text += [f"| {r['frequency_hz']:g} | {r['min_gain']:.4f} | {r['max_gain']:.4f} |" for r in corner_table]
    text += ['','## Model limits','']+['- '+x for x in report['model_limits']]
    (REP/'VALIDATION.md').write_text('\n'.join(text)+'\n')
    print(json.dumps({'checks_passed':len(checks),'erc':0,'ignored':0,'response':response,'component_corners':corner_table,'budget':budget},indent=2))

if __name__=='__main__':main()
