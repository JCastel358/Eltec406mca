"""Build a native, editable, three-page KiCad reference schematic.

No PCB is created. Symbols are project-owned with manufacturer pin numbering.
Run this file with ordinary Python; validate.py uses KiCad as an independent reader.
"""
from pathlib import Path
import csv
import hashlib
import json
import math
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT / 'project'
LIB = ROOT / 'libraries'
NAME = 'switchable_gain'
LIBNAME = 'Eltec_Gain'
NS = uuid.UUID('c0f08aad-c9fc-4b96-92fd-0069d5dd3a90')
symbols, parts, sheets = {}, {}, {}

def uid(s): return str(uuid.uuid5(NS, str(s)))
def q(s): return json.dumps(str(s), ensure_ascii=False)
def fmt(v): return f'{float(v):.6f}'.rstrip('0').rstrip('.') or '0'
def ef(size=1.27, hide=False, justify=''):
    return f'(effects (font (size {size} {size}))' + (' (hide yes)' if hide else '') + (f' (justify {justify})' if justify else '') + ')'
def prop(n,v,x=0,y=0,hide=False,size=1.27):
    return f'(property {q(n)} {q(v)} (at {fmt(x)} {fmt(y)} 0) {ef(size,hide)})'
def line(points, width=.254):
    return '(polyline (pts '+ ' '.join(f'(xy {fmt(x)} {fmt(y)})' for x,y in points)+f') (stroke (width {width}) (type default)) (fill (type none)))'
def rect(x1,y1,x2,y2):
    return f'(rectangle (start {x1} {y1}) (end {x2} {y2}) (stroke (width .254) (type default)) (fill (type none)))'
def pin(n,label,kind,x,y,a,length=2.54):
    return f'(pin {kind} line (at {x} {y} {a}) (length {length}) (name {q(label)} {ef(1.016)}) (number {q(n)} {ef(1.016)}))'
def define(name, reference, units, desc=''):
    # Each unit is (graphics, [(number, name, type, x, y, angle, optional length)]).
    body=[]; maps={}
    for unit,(graphics,pins) in units.items():
        body.append(f'(symbol {q(name+"_"+str(unit)+"_1")} {graphics} '+ ' '.join(pin(*p) for p in pins)+')')
        maps[unit]={str(p[0]):(p[3],p[4]) for p in pins}
    raw=f'(symbol {q(name)} (pin_names (offset .508)) (in_bom yes) (on_board yes) {prop("Reference",reference)} {prop("Value",name)} {prop("Description",desc,hide=True)} '+''.join(body)+')'
    symbols[name]={'raw':raw,'maps':maps}

def symbols_init():
    rp=[('1','','passive',-5.08,0,0),('2','','passive',5.08,0,180)]
    define('R','R',{1:(rect(-2.54,1.016,2.54,-1.016),rp)})
    cap=line([(-2.54,0),(-.635,0)])+line([(.635,0),(2.54,0)])+line([(-.635,-2.54),(-.635,2.54)])+line([(.635,-2.54),(.635,2.54)])
    define('C','C',{1:(cap,rp)})
    diode=line([(-2.54,0),(-1.27,0)])+line([(1.27,0),(2.54,0)])+line([(-1.27,-1.27),(-1.27,1.27)])+line([(-1.27,0),(1.27,-1.27),(1.27,1.27),(-1.27,0)])
    define('D','D',{1:(diode,[('1','K','passive',-5.08,0,0),('2','A','passive',5.08,0,180)])})
    tri=line([(-5.08,-5.08),(-5.08,5.08),(5.08,0),(-5.08,-5.08)])
    units={}
    for u,(plus,minus,out) in enumerate([(3,2,1),(5,6,7),(10,9,8),(12,13,14)],1):
        units[u]=(tri,[(str(plus),'+','input',-7.62,2.54,0),(str(minus),'-','input',-7.62,-2.54,0),(str(out),'','output',7.62,0,180)])
    units[5]=(rect(-2.54,2.54,2.54,-2.54),[('4','V+','power_in',0,7.62,270,5.08),('11','V-','power_in',0,-7.62,90,5.08)])
    define('OPA4192','U',units,'OPA4192 SOIC-14, TI SBOS620E pp.4-5 pin map')
    contact=line([(-5.08,0),(-3.81,0),(3.81,2.54)])+line([(3.81,2.54),(5.08,2.54)])+line([(3.81,-2.54),(5.08,-2.54)])
    reverse=line([(5.08,0),(3.81,0),(-3.81,2.54)])+line([(-3.81,2.54),(-5.08,2.54)])+line([(-3.81,-2.54),(-5.08,-2.54)])
    define('G6K_2P_Y','K',{
        1:(contact,[('3','COM','passive',-7.62,0,0),('2','NC','passive',7.62,2.54,180),('4','NO','passive',7.62,-2.54,180)]),
        2:(reverse,[('6','COM','passive',7.62,0,180),('7','NC','passive',-7.62,2.54,0),('5','NO','passive',-7.62,-2.54,0)]),
        3:(rect(-2.54,3.81,2.54,-3.81),[('1','+','passive',0,7.62,270,3.81),('8','-','passive',0,-7.62,90,3.81)])},
        'Omron G6K-2P-Y DC5 non-latching; OFF joins 3-2 and 6-7; ON joins 3-4 and 6-5; coil +1/-8')
    mos=line([(-5.08,0),(-2.54,0)])+line([(-2.54,-3.81),(-2.54,3.81)])+line([(-1.27,-3.81),(-1.27,3.81)])+line([(-1.27,2.54),(2.54,2.54),(2.54,5.08)])+line([(-1.27,-2.54),(2.54,-2.54),(2.54,-5.08)])+line([(1.27,0),(-1.27,0),(0,-.635),(0,.635),(-1.27,0)])
    define('AO3400A','Q',{1:(mos,[('1','G','input',-7.62,0,0),('2','S','passive',2.54,-7.62,90),('3','D','passive',2.54,7.62,270)])},'AOS AO3400A SOT-23: G1 S2 D3')
    define('SW_SPST','SW',{1:(line([(-2.54,0),(2.54,2.54)]),rp)},'Panel or bench SPST switch; OPEN=BUFFER, CLOSED=NOISE')
    for n in (2,3,4):
        for side in ('L','R'):
            x=-5.08 if side=='L' else 5.08
            pins=[(str(i+1),str(i+1),'passive',x,-i*2.54,0 if side=='L' else 180) for i in range(n)]
            define(f'Conn{n}{side}','J',{1:(rect(-2.54,1.27,2.54,-(n-1)*2.54-1.27),pins)})
    define('PWR_FLAG','#FLG',{1:(line([(0,0),(-1.27,1.27),(0,2.54),(1.27,1.27),(0,0)]),[('1','pwr','power_out',0,0,90,0)])})

class Sheet:
    def __init__(self,name,title,page):
        self.name,self.title,self.page=name,title,page
        self.items=[];self.used=set();self.inst=[];self.count=0
        sheets[name]=self
    def key(self,s):
        self.count+=1;return uid(f'{self.name}:{self.count}:{s}')
    def wire(self,*pts):
        pts=[(round(x,6),round(y,6)) for x,y in pts]
        for a,b in zip(pts,pts[1:]):
            if a==b: continue
            assert a[0]==b[0] or a[1]==b[1], (a,b)
            self.items.append(f'(wire (pts (xy {fmt(a[0])} {fmt(a[1])}) (xy {fmt(b[0])} {fmt(b[1])})) (stroke (width 0) (type default)) (uuid {q(self.key("w"))}))')
    def dot(self,p):
        self.items.append(f'(junction (at {fmt(p[0])} {fmt(p[1])}) (diameter 0) (color 0 0 0 0) (uuid {q(self.key("j"))}))')
    def label(self,name,p,angle=0,global_=False):
        if global_:
            self.items.append(f'(global_label {q(name)} (shape bidirectional) (at {fmt(p[0])} {fmt(p[1])} {angle}) {ef(1.016,justify="left" if angle==0 else "right")} (uuid {q(self.key(name))}) {prop("Intersheetrefs","${INTERSHEET_REFS}",*p,hide=True)})')
        else:
            self.items.append(f'(label {q(name)} (at {fmt(p[0])} {fmt(p[1])} {angle}) {ef(1.016,justify="left bottom")} (uuid {q(self.key(name))}))')
    def stub(self,name,p,d=7.62,global_=True):
        end=(round(p[0]+d,6),p[1]);self.wire(p,end);self.label(name,end,0 if d>0 else 180,global_)
    def nc(self,p):
        self.items.append(f'(no_connect (at {fmt(p[0])} {fmt(p[1])}) (uuid {q(self.key("nc"))}))')
    def text(self,txt,x,y,size=1.27):
        self.items.append(f'(text {q(txt)} (at {x} {y} 0) {ef(size,justify="left top")} (uuid {q(self.key(txt))}))')
    def add(self,ref,sym,value,x,y,unit=1,angle=0,footprint='',mpn='',datasheet='',desc='',show_value=True):
        self.used.add(sym); ident=uid(f'{ref}:{unit}')
        rawmap=symbols[sym]['maps'][unit];c=math.cos(math.radians(angle));s=math.sin(math.radians(angle))
        pinmap={k:(round(x+px*c+py*s,6),round(y-px*s-py*c,6)) for k,(px,py) in rawmap.items()}
        fields=[prop('Reference',ref,x,y-8.89,size=1.27),prop('Value',value,x,y+8.89,hide=not show_value,size=1.016),prop('Footprint',footprint,x,y,True),prop('Datasheet',datasheet,x,y,True),prop('MPN',mpn,x,y,True),prop('Description',desc,x,y,True)]
        if sym.startswith('Conn'):
            fields[1]=prop('Value',value,x,y+(int(sym[4])-1)*2.54+6.35,hide=not show_value,size=1.016)
        if sym in ('R','C','D','SW_SPST'):
            if angle in (90,270):
                fields[0]=prop('Reference',ref,x+8.89,y-1.27,size=1.016)
                fields[1]=prop('Value',value,x+11.43,y+1.27,size=1.016)
            else:
                fields[0]=prop('Reference',ref,x,y-5.08,size=1.016)
                fields[1]=prop('Value',value,x,y-2.54,size=1.016)
        if ref.startswith('#'):
            fields=[prop('Reference',ref,x,y,True),prop('Value','PWR_FLAG',x,y,True)]
        path='/'+uid('root')+('' if self.page==1 else '/'+uid('sheet:'+self.name))
        self.items.append(f'(symbol (lib_id {q(LIBNAME+":"+sym)}) (at {x} {y} {angle}) (unit {unit}) (in_bom {"no" if ref.startswith("#") else "yes"}) (on_board {"no" if ref.startswith("#") else "yes"}) (dnp no) (uuid {q(ident)}) '+ ' '.join(fields)+' '+ ' '.join(f'(pin {q(k)} (uuid {q(uid(ident+k))}))' for k in rawmap)+f' (instances (project {q(NAME)} (path {q(path)} (reference {q(ref)}) (unit {unit})))))')
        if not ref.startswith('#'):
            if ref not in parts:parts[ref]={'ref':ref,'symbol':sym,'value':value,'footprint':footprint,'mpn':mpn,'datasheet':datasheet,'description':desc,'units':[]}
            parts[ref]['units'].append({'unit':unit,'sheet':self.name,'pins':pinmap})
        return pinmap
    def write(self):
        root=uid('root') if self.page==1 else uid('page:'+self.name)
        lib='(lib_symbols '+''.join(symbols[n]['raw'].replace(f'(symbol {q(n)}',f'(symbol {q(LIBNAME+":"+n)}',1) for n in sorted(self.used))+')'
        text=f'(kicad_sch (version 20250114) (generator "eltec_gain_reference") (uuid {q(root)}) (paper "A3") (title_block (title {q(self.title)}) (date "2026-09-14") (rev "R1 PROTO") (company "Eltec - engineering reference") (comment 1 "SCHEMATIC ONLY - NOT A FABRICATION RELEASE"))\n'+lib+'\n'+'\n'.join(self.items)+'\n)\n'
        (PROJ/(self.name+'.kicad_sch')).write_text(text,encoding='utf-8')

RFP='Resistor_SMD:R_0805_2012Metric';CFP='Capacitor_SMD:C_0805_2012Metric'
UFP='Package_SO:SOIC-14_3.9x8.7mm_P1.27mm'
UDS='https://www.ti.com/lit/ds/symlink/opa4192.pdf'
KFP='Relay_THT:Relay_DPDT_Omron_G6K-2P-Y'
KDS='https://components.omron.com/us-en/system/files/2026-05/datasheet_pdf/K106-E1.pdf'

def resistor(sh,ref,val,x,y,angle=0,desc=''):
    return sh.add(ref,'R',val,x,y,angle=angle,footprint=RFP,desc=desc)
def capacitor(sh,ref,val,x,y,angle=0,desc=''):
    return sh.add(ref,'C',val,x,y,angle=angle,footprint=CFP,desc=desc)
def amp(sh,unit,x,y):return sh.add('U1','OPA4192','OPA4192IDR',x,y,unit=unit,footprint=UFP,mpn='OPA4192IDR',datasheet=UDS)
def relay(sh,unit,x,y):return sh.add('K1','G6K_2P_Y','G6K-2P-Y DC5',x,y,unit=unit,footprint=KFP,mpn='G6K-2P-Y DC5',datasheet=KDS)

def build():
    for d in (PROJ,LIB,ROOT/'reports',ROOT/'exports'):d.mkdir(parents=True,exist_ok=True)
    symbols_init()
    a=Sheet(NAME,'1X / 100X switching amplifier',1)
    a.text('1X OFFSET + SENSITIVITY / 100X FILTERED NOISE',20.32,17.78,2.032)
    a.text('One OPA4192 section replaces one existing follower. Same detector bias/loading, +5 V supply and DAQ channel.',20.32,27.94)
    a.text('K1 is drawn UNPOWERED: gain resistor bypassed; unfiltered follower output selected.',20.32,35.56)
    jin=a.add('J1','Conn2R','DETECTOR',30.48,71.12,footprint='Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',desc='Pin 1 detector output; pin 2 analog ground. Existing detector source resistor remains in the sensor box.')
    ua=amp(a,1,111.76,73.66)
    a.wire(jin['1'],ua['3']);a.label('DETECTOR_IN',(60.96,71.12))
    a.wire(jin['2'],(35.56,86.36));a.stub('AGND',(35.56,86.36))
    a.wire(ua['2'],(88.9,76.2),(88.9,109.22));a.label('FB',(88.9,104.14))
    a.wire(ua['1'],(177.8,73.66));a.stub('GAIN_OUT',(177.8,73.66))
    a.wire((177.8,73.66),(177.8,109.22));a.dot((177.8,73.66))
    previous=(88.9,109.22)
    for i,x in enumerate([104.14,119.38,134.62,149.86,165.1],1):
        r=resistor(a,f'R{i}','2M00 0.1%',x,109.22,desc='Low-noise thin-film. Five series resistors total 10.000 Mohm.')
        a.wire(previous,r['1']);previous=r['2']
    a.wire(previous,(177.8,109.22))
    a.text('Rf = R1 + R2 + R3 + R4 + R5 = 10.000 Mohm',96.52,119.38,1.016)
    ka=relay(a,1,147.32,144.78)
    a.wire((88.9,109.22),(88.9,144.78),ka['3']);a.dot((88.9,109.22));a.dot((88.9,144.78))
    a.stub('GAIN_OUT',ka['2'],12.7);a.nc(ka['4'])
    rg=resistor(a,'R6','101k 0.1%',88.9,160.02,270,desc='AC gain leg; noise gain = 1 + 10M/101k = 100.0099.')
    a.wire((88.9,144.78),rg['1'])
    cg=a.add('C1','C','10u PP film 5%',88.9,185.42,angle=270,footprint='Capacitor_THT:C_Rect_L31.5mm_W17.0mm_P27.50mm_MKS4',mpn='MKP4D051006G00JSSD',datasheet='https://www.wima.de/en/our-product-range/metallized-capacitors/mkp-4/',desc='WIMA MKP4 polypropylene candidate, 10uF 100V 5%, L31.5 x W17 x H29 mm, pitch27.5mm. Stock MKS4 footprint name denotes matching case geometry only: buy polypropylene MKP4. Availability and fit require review; no electrolytic/X7R substitute.')
    a.wire(rg['2'],cg['1']);a.label('GAIN_CAP',(88.9,175.26))
    a.wire(cg['2'],(88.9,198.12));a.stub('AGND',(88.9,198.12))
    a.text('C1 charges through R6 while 1X is selected.\nNominal R6*C1 = 1.01 s. High-pass transition ~0.158 Hz.\nDC gain tends to 1; measure offset in 1X to avoid leakage error.',139.7,177.8,1.016)
    kb=relay(a,2,292.1,88.9)
    a.stub('GAIN_OUT',kb['7'],-15.24);a.stub('FILTER_OUT',kb['5'],-15.24)
    rout=resistor(a,'R7','100R 1%',322.58,88.9,desc='Keep the existing output isolation resistor once, after selection.')
    a.wire(kb['6'],rout['1'])
    jout=a.add('J2','Conn2L','TO SAME DAQ CH',375.92,88.9,footprint='Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',desc='Pin 1 ADC single-ended input, 0-5V; pin 2 AGND. This replaces one original buffer output.')
    a.wire(rout['2'],jout['1']);a.label('DAQ_OUT',(345.44,88.9))
    a.wire(jout['2'],(370.84,104.14));a.stub('AGND',(370.84,104.14),-10.16)
    a.text('MODE                  K1       SIGNAL TO DAQ\nBUFFER / SENSITIVITY  OFF      ~1X, direct follower\nNOISE                 ON       ~100X AC, 4-pole low-pass',238.76,121.92,1.016)
    a.text('0.7 V offset + 500 mV p-p -> about 0.45 to 0.95 V at 1X.\nNever run sensitivity illumination in NOISE mode.\nRelay contacts move at different times: discard all transitions.',238.76,147.32,1.016)
    a.text('Switching: stimulus OFF -> settle in 1X -> select NOISE ->\nwait for electrical/thermal settling -> record noise.\nReturn to 1X before restoring the sensitivity stimulus.',238.76,177.8,1.016)
    for name,title,x in [('noise_filter','Two unity-gain low-pass sections',30.48),('power_control','Power, manual/DAQ mode control',220.98)]:
        y=223.52
        a.items.append(f'(sheet (at {x} {y}) (size 165.1 25.4) (stroke (width .254) (type default)) (fill (color 0 0 0 0)) (uuid {q(uid("sheet:"+name))}) {prop("Sheetname",title,x+82.55,y-2.54)} {prop("Sheetfile",name+".kicad_sch",x+82.55,y+27.94,size=1.016)} (instances (project {q(NAME)} (path {q("/"+uid("root"))} (page {q(2 if name=="noise_filter" else 3)})))))')

    f=Sheet('noise_filter','100X noise filter / 160 Hz low-pass',2)
    f.text('HARDWARE LOW-PASS: PASSES THE NOISE SIGNAL THROUGH 100 Hz',20.32,17.78,2.032)
    f.text('Two unity-gain Sallen-Key sections. Lower-Q section first; filter is bypassed at the DAQ in 1X mode.',20.32,27.94)
    for ref1,ref2,rval,x0,ux,unit,cfrefs,cgref,cgval,in_net,out_net in [
        ('R10','R11','9k15 0.1%',50.8,124.46,2,[('C2','100n 1%'),('C3','18n 1%')],'C4','100n 1%','GAIN_OUT','LP1_OUT'),
        ('R12','R13','17k4 0.1%',203.2,276.86,3,[('C5','150n 1%')],'C6','22n 1%','LP1_OUT','FILTER_OUT')]:
        u=amp(f,unit,ux,83.82)
        r1=resistor(f,ref1,rval,x0,81.28);r2=resistor(f,ref2,rval,x0+38.1,81.28)
        an=(x0+15.24,81.28);bn=(ux-20.32,81.28);on=(ux+30.48,83.82)
        f.stub(in_net,r1['1'],-15.24,);f.wire(r1['2'],an,r2['1']);f.wire(r2['2'],bn,u[str(5 if unit==2 else 10)])
        f.wire(u[str(7 if unit==2 else 8)],on);f.stub(out_net,on,12.7)
        inv=u[str(6 if unit==2 else 9)];loopx=ux-12.7
        f.wire(inv,(loopx,86.36),(loopx,99.06),(ux+20.32,99.06),(ux+20.32,83.82));f.dot((ux+20.32,83.82))
        c=capacitor(f,cgref,cgval,bn[0],111.76,270,desc='C0G/NP0 or selected polypropylene, 1%. Ground leg controls damping.')
        f.wire(bn,c['1']);f.dot(bn);f.wire(c['2'],(bn[0],129.54));f.stub('AGND',(bn[0],129.54))
        topy=48.26
        for j,(ref,val) in enumerate(cfrefs):
            cy=topy+j*15.24
            c=capacitor(f,ref,val,x0+38.1,cy,desc='C0G/NP0 or selected polypropylene, 1%. Feedback capacitor goes from first RC node to op-amp OUTPUT.')
            f.wire((an[0],cy),c['1']);f.wire(c['2'],(on[0],cy))
        f.wire(an,(an[0],topy));f.wire(on,(on[0],topy));f.dot(an);f.dot(on)
        if len(cfrefs)>1:
            f.dot((an[0],topy+15.24));f.dot((on[0],topy+15.24))
    f.text('SECTION 1: R10=R11=9.15k, Cf=C2+C3=118n, Cg=C4=100n\nf0 = 160.125 Hz; Q = 0.54314\nSECTION 2: R12=R13=17.4k, Cf=C5=150n, Cg=C6=22n\nf0 = 159.226 Hz; Q = 1.30558',20.32,154.94,1.27)
    f.text('Nominal response (ideal amplifiers, before 100R/DAQ loading):\n3 Hz: 99.87X     50 Hz: 100.18X     100 Hz: 99.35X\n200 Hz: 37.65X   500 Hz: 1.04X     1 kHz: 0.065X\nValues approximate a fourth-order Butterworth response.',20.32,190.5,1.27)
    f.text('Build requirements:\n- No X7R/Y5V in the gain or filter signal capacitors.\n- Keep C2/C3 and C5 return paths at their own op-amp outputs.\n- Keep detector/feedback wiring away from relay coil and digital returns.\n- Confirm capacitive-load stability with the actual DAQ cable.\n- Calibrate each channel; calculated response is not a noise-floor test.',220.98,154.94,1.27)
    f.text('For 50 channels: 150 active amplifier sections = 38 OPA4192 quads total.\nExisting 13 quads can supply the 50 gain stages; 25 more quads supply\nthe 100 filter stages. This drawing is a one-channel reference, not a\ncompleted 50-channel PCB or harness drawing.',220.98,215.9,1.016)

    p=Sheet('power_control','Power and relay control / manual default',3)
    p.text('REGULATED ANALOG SUPPLY / SEPARATE RELAY SUPPLY / MODE CONTROL',20.32,17.78,2.032)
    p.text('Prototype uses one complete OPA4192. Units A-C on sheets 1-2; unit D parked below.',20.32,27.94)
    jp=p.add('J3','Conn4R','REGULATED POWER',30.48,48.26,footprint='Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical',desc='1 +5V analog, 2 AGND, 3 +5V relay, 4 PGND. 5V rails supplied externally.')
    for num,net in [('1','+5VA'),('2','AGND'),('3','+5V_COIL'),('4','PGND')]:p.stub(net,jp[num],10.16)
    for i,(net,x,y) in enumerate([('+5VA',68.58,43.18),('AGND',68.58,53.34),('+5V_COIL',68.58,63.5),('PGND',68.58,73.66)],1):
        p.add(f'#FLG0{i}','PWR_FLAG','PWR_FLAG',x,y);p.stub(net,(x,y))
    ue=amp(p,5,114.3,55.88)
    p.wire(ue['4'],(114.3,43.18));p.stub('+5VA',(114.3,43.18))
    p.wire(ue['11'],(114.3,71.12));p.stub('AGND',(114.3,71.12))
    for ref,val,x,y,rail,ground in [('C10','100n X7R',147.32,55.88,'+5VA','AGND'),('C11','10u X7R',185.42,55.88,'+5VA','AGND'),('C12','100n X7R',147.32,101.6,'+5V_COIL','PGND'),('C13','10u X7R',185.42,101.6,'+5V_COIL','PGND')]:
        c=capacitor(p,ref,val,x,y,270,desc='Supply decoupling only. Not in signal path.')
        p.stub(rail,c['1']);p.stub(ground,c['2'])
    rt=resistor(p,'R30','0R STAR',45.72,101.6,desc='Single analog/coil ground star. One star per assembled system; do not repeat ground ties around the array.')
    p.stub('AGND',rt['1'],-10.16);p.stub('PGND',rt['2'],10.16)
    p.text('Route coil return separately to this star.\nThe existing LT1763 analog rail must NOT power 50 relay coils.',20.32,119.38,1.016)
    kc=relay(p,3,307.34,60.96)
    p.wire(kc['1'],(307.34,43.18));p.stub('+5V_COIL',(307.34,43.18),10.16)
    qm=p.add('Q1','AO3400A','AO3400A',304.8,99.06,footprint='Package_TO_SOT_SMD:SOT-23',mpn='AO3400A',datasheet='https://www.aosmd.com/res/datasheets/AO3400A.pdf')
    p.wire(kc['8'],qm['3']);p.label('COIL_LOW',(307.34,81.28))
    p.wire(qm['2'],(307.34,119.38));p.stub('PGND',(307.34,119.38),10.16)
    d=p.add('D1','D','1N4148W',345.44,60.96,angle=270,footprint='Diode_SMD:D_SOD-123',mpn='1N4148W',desc='Flyback clamp; cathode to +5V_COIL, anode to Q1 drain. Allow actual release time with diode.')
    p.wire(d['1'],(345.44,43.18),(307.34,43.18));p.dot((307.34,43.18))
    p.wire(d['2'],(345.44,81.28),(307.34,81.28));p.dot((307.34,81.28))
    gr=resistor(p,'R21','1k 1%',274.32,99.06);p.stub('MODE_REQ',gr['1'],-10.16);p.wire(gr['2'],qm['1'])
    pd=resistor(p,'R22','100k 1%',284.48,129.54,270);p.wire((284.48,99.06),pd['1']);p.dot((284.48,99.06));p.stub('PGND',pd['2'])
    sw=p.add('SW1','SW_SPST','OPEN=1X / CLOSED=NOISE',50.8,160.02,footprint='',desc='Panel SPST manual mode switch. Start OPEN. Wire to the illustrated two terminals.')
    p.stub('+5V_COIL',sw['1'],-10.16);p.stub('MANUAL_REQ',sw['2'],10.16)
    mp=resistor(p,'R20','100k 1%',50.8,185.42,270);p.stub('MANUAL_REQ',mp['1']);p.stub('PGND',mp['2'])
    sel=p.add('JP1','Conn3R','1-2 MANUAL / 2-3 AUTO',121.92,160.02,footprint='Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical',desc='Fit shunt 1-2 for manual bench use. Remove before placing shunt 2-3. Never bridge all three.')
    for num,net in [('1','MANUAL_REQ'),('2','MODE_REQ'),('3','AUTO_NOISE_REQ')]:p.stub(net,sel[num],10.16)
    auto=p.add('J4','Conn2R','DAQ CONTROL (OPTION)',121.92,195.58,footprint='Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',desc='1 static 0/3.3V or 0/5V NOISE request, 2 control ground. No PWM.')
    p.stub('AUTO_NOISE_REQ',auto['1']);p.stub('PGND',auto['2'])
    p.text('Manual default: JP1 shunt 1-2; SW1 OPEN. No shunt also de-energizes K1.\nAUTO: initialize DAQ DIO LOW before fitting 2-3 / enabling relay power.\nDAQ DIO lines may power up pulled HIGH. Auto default is NOT guaranteed\nby R22 while a connected DAQ actively pulls/drives the command HIGH.',20.32,220.98,1.016)
    # Park the unused amplifier safely above the lower rail.
    ud=amp(p,4,322.58,190.5)
    p.stub('PARK_1V25',ud['12'],-10.16)
    p.wire(ud['13'],(309.88,193.04),(309.88,208.28),(337.82,208.28),(337.82,190.5),ud['14'])
    p.label('SPARE_OUT',(337.82,190.5))
    rtop=resistor(p,'R31','30k 1%',251.46,177.8,270);rbot=resistor(p,'R32','10k 1%',251.46,203.2,270)
    p.stub('+5VA',rtop['1']);p.wire(rtop['2'],rbot['1']);p.stub('AGND',rbot['2']);p.stub('PARK_1V25',(251.46,190.5));p.dot((251.46,190.5))
    p.text('K1 coil: about 21.1 mA at 5 V per channel.\n50 coils: ~1.06 A nominal, ~1.24 A at +5% rail / -10% resistance.\nUse a separate regulated 5 V, >=1.5 A relay supply for 50 channels.\nAnalog: ~150 mA typical for 150 active sections; budget full quads.\nNo coil PWM; keep relay heat and coil wiring away from R1-R6/C1.',220.98,228.6,1.016)

    for sh in sheets.values():sh.write()
    (LIB/'gain.kicad_sym').write_text('(kicad_symbol_lib (version 20250114) (generator "eltec_gain_reference")\n'+'\n'.join(x['raw'] for x in symbols.values())+'\n)\n',encoding='utf-8')
    (PROJ/'sym-lib-table').write_text('(sym_lib_table (version 7) (lib (name "Eltec_Gain") (type "KiCad") (uri "${KIPRJMOD}/../libraries/gain.kicad_sym") (options "") (descr "Project-owned symbols; manufacturer pin maps")))\n')
    (PROJ/(NAME+'.kicad_pro')).write_text(json.dumps({'meta':{'filename':NAME+'.kicad_pro','version':1},'erc':{'erc_exclusions':[],'pin_map':[],'rule_severities':{'single_global_label':'warning','four_way_junction':'warning','simulation_model_issue':'warning','footprint_filter':'warning'}}},indent=2)+'\n')
    (ROOT/'reports/design_manifest.json').write_text(json.dumps(parts,indent=2)+'\n')
    with (ROOT/'exports/BOM.csv').open('w',newline='',encoding='utf-8-sig') as out:
        writer=csv.DictWriter(out,fieldnames=['ref','value','symbol','mpn','footprint','datasheet','description']);writer.writeheader()
        for part in parts.values():writer.writerow({k:part[k] for k in writer.fieldnames})
    # Preserve library graphics and footprints in the deliverable so it opens offline.
    fps=LIB/'gain.pretty';fps.mkdir(exist_ok=True)
    for part in parts.values():
        if not part['footprint']:continue
        lib,fn=part['footprint'].split(':')
        src=Path('C:/Program Files/KiCad/10.0/share/kicad/footprints')/(lib+'.pretty')/(fn+'.kicad_mod')
        if not src.exists():raise FileNotFoundError(src)
        shutil.copyfile(src,fps/src.name)
    # Footprints retain stock library identifiers; add local aliases for portability.
    libs=sorted({p['footprint'].split(':')[0] for p in parts.values() if p['footprint']})
    (PROJ/'fp-lib-table').write_text('(fp_lib_table (version 7) '+''.join(f'(lib (name {q(l)}) (type "KiCad") (uri "${{KIPRJMOD}}/../libraries/gain.pretty") (options "") (descr "Bundled stock footprint subset"))' for l in libs)+')\n')
    print('Created three-sheet KiCad reference, local symbols/footprints, BOM and manifest.')

if __name__=='__main__':build()
