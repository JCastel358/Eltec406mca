"""Draw the Eltec USB-controlled battery switch and ADC interposer prototype."""
from pathlib import Path
import sys, math, json

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tmp/power_board_tools'))
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase.pdfmetrics import stringWidth
import pymupdf

OUT = ROOT / 'output/pdf'
OUT.mkdir(parents=True, exist_ok=True)
QA = ROOT / 'tmp/power_board_schematic/rendered'
QA.mkdir(parents=True, exist_ok=True)
PDF = OUT / 'Eltec_USB_Master_Power_Rev_A.pdf'
W, H = 842, 595
c = canvas.Canvas(str(PDF), pagesize=(W, H))
c.setTitle('Eltec USB master power - Rev A prototype schematic')
c.setAuthor('Eltec Test Rig - design assistance')
INK, BLUE, GREEN, MUTED, PALE = [HexColor(x) for x in ['#172333','#176283','#08775F','#536574','#EEF3F6']]
page = 0

def text(x,y,s,size=10,bold=False,color=INK):
    c.setFillColor(color); c.setFont('Helvetica-Bold' if bold else 'Helvetica',size)
    c.drawString(x,H-y,s)

def line(*points,color=INK,width=1.1,dash=None):
    c.setStrokeColor(color); c.setLineWidth(width); c.setDash(dash or [])
    p=c.beginPath(); p.moveTo(points[0][0],H-points[0][1])
    for x,y in points[1:]: p.lineTo(x,H-y)
    c.drawPath(p); c.setDash([])

def dot(x,y):
    c.setFillColor(INK); c.circle(x,H-y,2,stroke=0,fill=1)

def rect(x,y,w,h,fill=None,stroke=INK):
    c.setLineWidth(.8); c.setStrokeColor(stroke)
    c.setFillColor(fill or white); c.rect(x,H-y-h,w,h,fill=bool(fill),stroke=1)

def para(x,y,s,width=760,size=10,leading=15,color=INK):
    words=s.split(); row=''; yy=y
    for word in words:
        nxt=(row+' '+word).strip()
        if stringWidth(nxt,'Helvetica',size)>width and row:
            text(x,yy,row,size,color=color); yy+=leading; row=word
        else: row=nxt
    if row: text(x,yy,row,size,color=color); yy+=leading
    return yy

def start(title,sub):
    global page
    if page: c.showPage()
    page+=1
    rect(0,0,W,8,BLUE,BLUE)
    text(32,38,title,22,True)
    text(32,58,sub,10,color=MUTED)
    line((32,565),(810,565),color=HexColor('#B6C6CE'),width=.6)
    text(32,581,'ELTEC | USB master power | Rev A | 2026-09-08 | Prototype: bench qualification required',8,color=MUTED)
    text(775,581,f'{page} / 6',8,color=MUTED)

def box(x,y,w,h,title,subtitle=''):
    rect(x,y,w,h,PALE,BLUE); text(x+12,y+22,title,11,True)
    if subtitle: para(x+12,y+39,subtitle,w-24,9,12)

def rh(x1,x2,y,label):
    mid=(x1+x2)/2; a=mid-15; b=mid+15
    line((x1,y),(a,y)); rect(a,y-5,30,10); line((b,y),(x2,y))
    text(mid-25,y-12,label,8)

def rv(x,y1,y2,label,side=1):
    mid=(y1+y2)/2
    line((x,y1),(x,mid-12)); rect(x-4,mid-12,8,24); line((x,mid+12),(x,y2))
    text(x+9 if side==1 else x-67,mid+3,label,8)

def cap(x,y1,y2,label,polar=False):
    mid=(y1+y2)/2
    line((x,y1),(x,mid-3)); line((x-8,mid-3),(x+8,mid-3))
    line((x-8,mid+3),(x+8,mid+3)); line((x,mid+3),(x,y2))
    text(x+12,mid+3,label,8)
    if polar: text(x-17,mid-7,'+',9,True)

def gnd(x,y,label='GND'):
    line((x,y),(x,y+5)); line((x-7,y+5),(x+7,y+5)); line((x-4,y+8),(x+4,y+8)); line((x-1,y+11),(x+1,y+11))
    text(x+10,y+12,label,8)

def arrow(x1,y1,x2,y2,color=BLUE):
    line((x1,y1),(x2,y2),color=color)
    a=math.atan2(y2-y1,x2-x1)
    for d in [-.45,.45]: line((x2,y2),(x2-7*math.cos(a+d),y2-7*math.sin(a+d)),color=color)

def tab(x,y,widths,headers,rows,rowh=24,size=9):
    total=sum(widths); rect(x,y,total,rowh,BLUE,BLUE)
    px=x
    for h,w in zip(headers,widths): text(px+7,y+16,h,size,True,white); px+=w
    yy=y+rowh
    for i,row in enumerate(rows):
        rect(x,yy,total,rowh,PALE if i%2==0 else white,HexColor('#D5E0E6'))
        px=x
        for val,w in zip(row,widths): text(px+7,yy+16,str(val),size); px+=w
        yy+=rowh
    return yy

start('One USB switch; two battery supplies','Complete retrofit = battery switch board + protected ADC socket adapter. No firmware change is required.')
box(32,88,178,61,'Laptop / PC','Original USB data connection')
box(255,88,216,61,'RIITOP switched extension','Use its device-side switched power')
box(516,88,292,61,'ESP32 + ADS1256','USB still powers the existing digital circuitry')
arrow(210,119,255,119); arrow(471,119,516,119)
box(32,190,178,60,'Battery 1: 6.5 V','Emitter: maximum 200 mA stated by user')
box(255,190,216,60,'Q1 battery switch','Positive lead only; schematic on sheet 2')
box(516,190,292,60,'Existing emitter barrel jack','Keep the regulator and emitter PWM circuit')
arrow(210,220,255,220); arrow(471,220,516,220)
box(32,285,178,60,'Battery 2: 6.5 V','Detector + reference + buffer supply')
box(255,285,216,60,'Q2 battery switch','Positive lead only; schematic on sheet 2')
box(516,285,292,60,'Existing detector barrel jack','All detector-side loads remain behind Q2')
arrow(210,315,255,315); arrow(471,315,516,315)
text(32,384,'SOCKET ADAPTER: unplug the ADS1256 module and insert the adapter beneath it.',12,True)
para(32,405,'Intercept every AIN0-AIN7 contact. Keep the two sides of each analog contact separate and connect them through sheet 4. Pass the other header contacts straight through by verified function. AINCOM may pass through only if it is grounded; otherwise it needs its own protected channel.',766)
para(32,459,'Take V5 and GND for this circuit from the USB-powered ADC supply. V5 must be the actual ADC AVDD rail: use the module 5V header only after confirming they are the same net. Preserve the original USB cable, USB-C configuration wiring, and signal-ground connections.',766)
rect(32,508,776,40,PALE,BLUE)
para(43,524,'Build limits: 5.5-7.5 V battery range; 5 V USB only; analog signals 0-4.0 V while measuring. Confirm detector current and jack polarity. This is a prototype design, not a bench-tested retrofit.',752,9,12)

start('Battery switches - use both IRLB8748s','U1 = VO1263AB, DIP-8 photovoltaic driver. It is not an ordinary phototransistor optocoupler.')

def battery(y,ch,ledp,ledn,pvp,pvn):
    q=f'Q{ch}'; ro=f'R{ch}'; rgs=f'R{ch+2}'; rb=f'R{ch+4}'; dz=f'D{ch}'
    text(32,y+1,f'CHANNEL {ch}  /  '+('EMITTER (original jack J2)' if ch==1 else 'DETECTORS + BUFFERS (original jack J1)'),12,True,BLUE)
    text(32,y+34,'V5'); rh(61,168,y+30,f'{ro} 270 ohm')
    rect(168,y+13,143,83,PALE,BLUE)
    text(177,y+29,f'U1 channel {ch}',10,True)
    text(177,y+48,f'{ledp} LED anode (+)',8)
    text(177,y+70,f'{ledn} LED cathode (-)',8)
    # LED terminals: draw the connections to the actual labeled terminal heights.
    line((168,y+30),(164,y+30),(164,y+45),(168,y+45))
    line((168,y+67),(144,y+67),(144,y+97)); gnd(144,y+97)
    text(253,y+48,f'{pvp} PV+',8)
    text(253,y+70,f'{pvn} PV-',8)
    text(355,y+27,f'Battery {ch} +',9,True)
    line((355,y+42),(420,y+42))
    rh(420,480,y+42,f'F{ch} 500 mA')
    # Q1/Q2 symbol deliberately exposes exact gate/drain/source pin functions.
    rect(488,y+8,126,76)
    text(499,y+25,q+' IRLB8748',10,True)
    text(499,y+45,'2 D',9); text(578,y+45,'S 3',9)
    text(539,y+71,'G 1',9)
    line((480,y+42),(488,y+42))
    line((614,y+42),(775,y+42))
    text(630,y+27,'To '+('emitter' if ch==1 else 'detector')+' jack +',9,True)
    line((311,y+45),(344,y+45),(344,y+119),(551,y+119),(551,y+84))
    line((311,y+67),(329,y+67),(329,y+156),(673,y+156),(673,y+42)); dot(673,y+42)
    rv(607,y+119,y+156,f'{rgs} 2.2 Mohm')
    line((551,y+119),(607,y+119)); dot(551,y+119); dot(607,y+156)
    # Low-current zener clamp across gate and source; explicit cathode/ anode markings.
    line((551,y+119),(551,y+130)); rect(538,y+130,26,13)
    text(542,y+140,'12V',7); line((551,y+143),(551,y+156)); dot(551,y+156)
    text(418,y+136,f'{dz} BZX55C12',8); text(418,y+148,'band/K to G; A to S',8)
    rv(734,y+42,y+173,f'{rb} 10k',side=1); dot(734,y+42)
    line((359,y+173),(775,y+173)); text(355,y+190,f'Battery {ch} -  ------------ '+('emitter' if ch==1 else 'detector')+' jack - (continuous wire)',9)
    dot(734,y+173)
    text(32,y+150,'PV outputs float with',9,color=MUTED)
    text(32,y+164,'their MOSFET source.',9,color=MUTED)
    text(32,y+178,'Do not join PV- to GND.',9,True)

battery(88,1,2,1,7,8)
battery(315,2,4,3,5,6)
text(32,531,'IRLB8748: front marking facing you, leads down: 1=G, 2=D, 3=S. The metal tab is D; keep the two tabs separate.',9,True)
text(32,546,'Internal body diode: anode at S (load), cathode at D (battery). U1 LED anodes are pins 2 and 4; cathodes are 1 and 3.',9,True)

start('ADC supply supervisor and isolation control','Build this on the socket adapter, close to the ADC module. V5 = monitored ADC AVDD, nominally 5 V.')
text(37,96,'V5',10,True); line((64,92),(782,92))
cap(78,92,179,'C1 100uF / 10V',True); dot(78,92); gnd(78,179)
rect(231,191,253,143,PALE,BLUE)
text(246,213,'U2 TPS3808G01DBVR',12,True)
text(246,231,'SOT-23-6 on pin-numbered adapter',9)
text(244,259,'5 SENSE',9); text(244,287,'2 GND',9)
text(369,259,'RESET (active low) 1',9)
text(282,320,'3 MR     4 CT     6 VDD',9)
line((151,92),(151,137)); rv(151,137,221,'R7 100k 0.1%')
line((151,221),(151,256),(231,256)); dot(151,256)
rv(151,256,339,'R8 10k 0.1%',side=-1); gnd(151,339)
line((231,284),(203,284),(203,344)); gnd(203,344)
line((305,334),(305,359),(262,359)); text(235,375,'V5 (MR held high)',8)
rv(357,334,415,'R29 100k'); text(332,432,'V5 (CT delay)',8)
line((420,334),(420,360),(465,360)); text(469,364,'V5',9,True)
cap(420,360,414,'C2 100nF'); dot(420,360); gnd(420,414)
rv(527,92,213,'R9 10k'); line((484,256),(527,256),(527,213)); dot(527,256)
rh(527,645,256,'R10 4.7k')
line((645,256),(661,256)); line((661,237),(661,277))
line((661,247),(690,229),(690,206)); line((661,267),(690,287),(690,329))
arrow(679,279,690,287,INK)
text(700,249,'Q3 2N3904',10,True); text(700,264,'3=C, 2=B, 1=E',9)
gnd(690,329)
rv(625,283,329,'R11 100k',side=-1); line((625,283),(625,256)); dot(625,256)
line((625,329),(690,329)); dot(690,329)
rv(690,92,181,'R12 2.2k'); line((690,181),(690,206),(791,206)); dot(690,206)
text(701,192,'ISOLATE_H',10,True,BLUE)
text(592,367,'ISOLATE_H -> U3-U6 pins 1, 8, 9, 16',10,True)
para(490,393,'HIGH = disconnect signals and ground ADC inputs. LOW = connect signals. Q3 supplies the logic inversion; do not connect the supervisor directly to all switch inputs.',307,10,14)
para(32,493,'R7/R8 set the falling threshold near 4.46 V. R29 selects about 300 ms delay on power-up (180-420 ms per datasheet). C1 is a starting timing reserve, not a validated hold-up guarantee: check actual AVDD collapse and USB startup inrush on sheet 6.',770,10,14)

start('ADC socket adapter - two inputs per ADG4613','Build this circuit FOUR times: U3 (AIN0/1), U4 (AIN2/3), U5 (AIN4/5), U6 (AIN6/7).')
rect(32,79,775,42,PALE,BLUE)
text(43,95,'Each IC: pin 13 = V5; pins 4 and 5 = GND; pin 12 = no connection.',10,True)
text(43,111,'Pins 1, 8, 9 and 16 = ISOLATE_H. Fit one 100 nF capacitor C3-C6 from pin 13 to pin 5.',10)

def asw(x,y,snum,dnum,name,closed):
    c.setFillColor(white); c.setStrokeColor(INK)
    c.circle(x,H-y,3,stroke=1,fill=1); c.circle(x+75,H-y,3,stroke=1,fill=1)
    line((x+3,y),(x+70,y if closed else y-18))
    text(x-10,y+17,snum,8); text(x+62,y+17,dnum,8)
    text(x+9,y-28,name,9,True)

def channel(y,which):
    even=which==0
    ss,dd=('S2 / 14','D2 / 15') if even else ('S3 / 11','D3 / 10')
    gs,gd=('S1 / 3','D1 / 2') if even else ('S4 / 6','D4 / 7')
    tag='even' if even else 'odd'
    text(32,y-8,f'PCB AIN {tag}',10,True); line((131,y),(307,y))
    asw(307,y,ss,dd,'SIGNAL: ON when LOW',False)
    line((382,y),(716,y)); text(662,y-12,f'ADC AIN {tag}',10,True)
    line((543,y),(543,y+69),(382,y+69)); dot(543,y)
    asw(307,y+69,gs,gd,'GROUND: ON when HIGH',True)
    line((103,y+69),(182,y+69)); rh(182,270,y+69,'220 ohm'); line((270,y+69),(307,y+69)); gnd(103,y+69)
    rv(627,y,y+83,'33k'); dot(627,y); gnd(627,y+83)
    text(671,y+40,'33k is on the',9,color=MUTED); text(671,y+54,'ADC side only.',9,color=MUTED)

channel(171,0); channel(330,1)
text(32,455,'Do not leave a straight-through header pin or wire bypassing either signal switch.',11,True,BLUE)
para(32,477,'The drawing shows ISOLATE_H = HIGH. At shutdown the 220-ohm ground paths discharge ADC-side capacitors. When power reaches zero, all ADG4613 switches open; the 33k resistors hold the ADC inputs near ground against leakage.',765,10,14)
para(32,525,'Use ADG4613BRUZ on TSSOP-16-to-DIP adapters, not ADG4612. Keep analog wiring short and away from emitter power leads. No added capacitors on analog inputs; verify existing module input capacitance is at most 1 uF per input.',765,9,12)

start('Parts and pin-to-pin wiring','Resistors: 1/4 W, 1% unless stated otherwise. Q1/Q2 IRLB8748 and perfboard are already available.')
rows=[
('U1','1','VO1263AB, DIP-8','Dual photovoltaic driver; DIP socket recommended'),
('U2','1','TPS3808G01DBVR','Adjustable supervisor + SOT-23-6 breakout'),
('U3-U6','4','ADG4613BRUZ','Four TSSOP-16 chips + four numbered DIP adapters'),
('Q1,Q2 / Q3','2 / 1','IRLB8748 / onsemi 2N3904','Existing power MOSFETs / add one TO-92 NPN'),
('D1,D2','2','BZX55C12, 12 V zener','Cathode stripe to G; anode to S'),
('R1,R2 / R3,R4','2 / 2','270 ohm 1% / 2.2 Mohm','LED current / gate discharge'),
('R5,R6,R9 / R10','3 / 1','10k / 4.7k','Output bleeders + reset pull-up / Q3 base'),
('R7 / R8','1 / 1','100k 0.1% / 10k 0.1%','Voltage-sense divider'),
('R11,R29 / R12','2 / 1','100k / 2.2k','Base pull-down + CT timing / ISOLATE_H pull-up'),
('R13-R20 / R21-R28','8 / 8','220 ohm / 33k','Ground discharge paths / ADC input pull-downs'),
('C1 / C2-C6','1 / 5','100 uF 10 V / 100 nF 16 V','C1 electrolytic, - to GND; ceramic decouplers'),
('F1,F2','2','500 mA DC fuses + holders','Initial choice; confirm both branch currents/inrush'),
('Connections','set','Male/female headers; 22-24 AWG','Match actual ADC footprint and barrel dimensions'),
]
tab(32,80,[142,38,218,378],['References','Qty','Value / part','Build note'],rows,rowh=23,size=8.5)
text(32,417,'Exact ADC routing (repeat the sheet 4 circuit for each row):',11,True)
tab(32,430,[95,168,238,275],['IC','PCB-side signals','ADC-side signal + grounding junctions','Ground resistor / pull-down'],[
('U3','AIN0 -> 14; AIN1 -> 11','AIN0 -> 15+2; AIN1 -> 10+7','R13/R21 for AIN0; R14/R22 for AIN1'),
('U4','AIN2 -> 14; AIN3 -> 11','AIN2 -> 15+2; AIN3 -> 10+7','R15/R23 for AIN2; R16/R24 for AIN3'),
('U5','AIN4 -> 14; AIN5 -> 11','AIN4 -> 15+2; AIN5 -> 10+7','R17/R25 for AIN4; R18/R26 for AIN5'),
('U6','AIN6 -> 14; AIN7 -> 11','AIN6 -> 15+2; AIN7 -> 10+7','R19/R27 for AIN6; R20/R28 for AIN7'),
],rowh=23,size=8.5)

start('Assembly checks and first power-up','Verify with current-limited supplies first; qualify the assembled board before technicians use it.')
y=88
steps=[
('1. Map the module before soldering.', 'With all sources disconnected, confirm jack polarity and every header contact. V5 must follow the actual ADC AVDD; if the module has a regulator/filter between header 5V and AVDD, do not treat them as interchangeable. Confirm AINCOM is grounded and input capacitance is at most 1 uF per channel. Keep battery returns as originally wired; do not add a new ground tie across emitter isolation.'),
('2. Test the battery board with dummy loads.', 'Use one 6.5 V current-limited source and a 33-ohm, 5 W resistor per branch (about 197 mA). Apply 5 V to V5/GND. Check each gate relative to its own SOURCE: at least 4.5 V ON, less than 0.5 V after OFF settles. Confirm switched outputs rise to the battery voltage and decay near zero after OFF. D and S must not be reversed. Keep the MOSFET drain tabs separate.'),
('3. Test the socket adapter without the ADC.', 'Apply 5 V at V5; after the delay, ISOLATE_H should be below 0.4 V and each signal path should conduct. Below approximately 4.46 V it should go HIGH and each output should connect to ground through its 220-ohm path. With V5 absent and 4.0 V applied to each PCB-side input, each ADC-side input should remain below 0.2 V.'),
('4. Check shutdown with an oscilloscope.', 'With the actual module and PC, capture AVDD, ISOLATE_H and each ADC input for USB OFF, unplugging, rapid toggles and brownouts. Require -0.3 V < AIN < AVDD + 0.3 V throughout; use a 0.2 V upper-margin target. Inputs must discharge before AVDD reaches zero. C1 sizing assumes no more than 500 mA discharges V5 and no more than 1 uF at each ADC input; verify both. Supervisor assertion time is typical, not a guaranteed system timing bound.'),
('5. Recheck startup and measurement quality.', 'Confirm stable AVDD of 4.75-5.25 V and successful USB enumeration with the added 100 uF; check startup inrush and repeated starts on the actual PC. Verify every normal signal stays within 0-4.0 V. The switches and 33k loads change the analog path: compare offset, sensitivity, reference response and noise against the original setup, then recalibrate before production. Wait for the usual detector settling before testing.'),
]
for title,body in steps:
    text(32,y,title,10,True); y=para(32,y+15,body,776,9,12)+9
text(32,477,'Design basis and limits',10,True)
para(32,493,'Both batteries = 6.5 V and emitter maximum = 200 mA (user). Detector current, actual module circuitry and power waveforms remain unmeasured. The single-MOSFET battery paths do not block reverse current from an independently powered load and provide no reverse-battery protection. The adapter does not clamp an excessive detector voltage during normal ON operation.',776,8.5,11)
sources=[
('IRLB8748', 'https://www.infineon.com/part/IRLB8748'),
('VO1263', 'https://www.vishay.com/doc/?84639'),
('TPS3808', 'https://www.ti.com/lit/ds/symlink/tps3808.pdf'),
('ADG4613', 'https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf'),
('2N3904', 'https://www.onsemi.com/download/data-sheet/pdf/2n3903-d.pdf'),
('BZX55', 'https://www.vishay.com/docs/85604/bzx55.pdf'),
]
xx=32
for label,url in sources:
    ww=stringWidth(label,'Helvetica',8)+20
    text(xx,548,label,8,color=BLUE)
    c.linkURL(url,(xx,H-552,xx+ww-12,H-538),relative=0,thickness=0)
    xx+=ww
c.save()

doc=pymupdf.open(PDF)
assert len(doc)==6
for i,p in enumerate(doc):
    p.get_pixmap(matrix=pymupdf.Matrix(1.8,1.8),alpha=False).save(QA/f'page-{i+1}.png')
alltext='\n'.join(p.get_text() for p in doc)
for required in ['IRLB8748','VO1263AB','TPS3808G01DBVR','ADG4613BRUZ','ISOLATE_H','AIN7']:
    assert required in alltext,required
(QA/'extracted.txt').write_text(alltext,encoding='utf-8')
print(json.dumps({'pdf':str(PDF),'pages':len(doc),'rendered':str(QA)},indent=2))
