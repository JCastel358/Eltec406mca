"""Derive a separate revision firmware; never edit the existing rig firmware."""
from pathlib import Path
import difflib
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parents[1] / 'Arduino/Eltec/Eltec.ino'
DEST = ROOT / 'firmware/Eltec_USB_Master/Eltec_USB_Master.ino'
original = SOURCE.read_text(encoding='utf-8')
code = original


def replace(before, after):
    global code
    count = code.count(before)
    if count != 1:
        raise ValueError(f'Expected one source anchor, found{count}: {before[:100]}')
    code = code.replace(before, after)


replace('static const int PIN_DRDY = 4;', '''static const int PIN_DRDY = 4;
// USB-master R1 carrier only: ESP-powered status gate, LOW means ready.
// GPIO35 is input-only and is never assigned to an emitter output.
static const int PIN_POWER_NOT_READY = 35;
static volatile bool powerFaultSeen = true;
static volatile uint32_t carrierPowerEpoch = 0;

static bool carrierPowerReady() {
  return digitalRead(PIN_POWER_NOT_READY) == LOW;
}

static void IRAM_ATTR onCarrierPowerChange() {
  // Latch the delivered rising edge even if a short pulse has already ended.
  powerFaultSeen = true;
  carrierPowerEpoch++;
}''')
replace('static uint32_t streamCount = 0;', '''static uint32_t streamCount = 0;
static bool streamPowerFault = false;
static uint32_t streamPowerEpoch = 0;''')
replace('''  while (digitalRead(PIN_DRDY) == HIGH) {
    if (millis() - start > timeoutMs) return false;
  }
  return true;
}''', '''  while (digitalRead(PIN_DRDY) == HIGH) {
    if (powerFaultSeen || !carrierPowerReady()) return false;
    if (millis() - start > timeoutMs) return false;
  }
  return !powerFaultSeen && carrierPowerReady();
}''')
replace('''  gotFirstCommand = true;
  if (strcmp(cmd, "IDN?") == 0) {''', '''  gotFirstCommand = true;
  // Commands that need live analog hardware cannot operate on stale settings.
  const bool needsAdc = strncmp(cmd, "FE,", 3) == 0 ||
      strcmp(cmd, "OFFSET?") == 0 || strcmp(cmd, "REF?") == 0 ||
      strncmp(cmd, "STREAM,START", 12) == 0 ||
      strcmp(cmd, "PWM,ON") == 0 || strcmp(cmd, "GATE,ON") == 0;
  if (needsAdc && (!adsOk || powerFaultSeen || !carrierPowerReady())) {
    Serial.println("ERR,carrier power or ADC not ready; retry after ready");
    return;
  }
  if (strcmp(cmd, "IDN?") == 0) {''')
replace('Serial.println("ELTEC-ESP32-ADS1256,v3.2");', 'Serial.println("ELTEC-ESP32-ADS1256,v3.3");')
replace('''    if (adsApplyFrontEnd()) {
      Serial.printf("OK,FE,gain=%d,buf=%d\\n", 1 << pgaSensor,''', '''    if (adsApplyFrontEnd() && !powerFaultSeen && carrierPowerReady()) {
      Serial.printf("OK,FE,gain=%d,buf=%d\\n", 1 << pgaSensor,''')
replace('''      adsApplyFrontEnd();
      Serial.println("ERR,front-end apply/verify failed (previous config restored)");''', '''      adsOk = false;  // fresh verified initialization restores previous settings
      Serial.println("ERR,front-end apply failed; ADC reinitialization required");''')
replace('''    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("OFFSET,%.5f\\n", v);''', '''    if (isnan(v) || powerFaultSeen || !carrierPowerReady())
      Serial.println("ERR,offset invalid or ADS1256 timeout");
    else Serial.printf("OFFSET,%.5f\\n", v);''')
replace('''  return (samples & 1) ? buf[samples / 2]
                       : 0.5f * (buf[samples / 2 - 1] + buf[samples / 2]);''', '''  // Include the final SPI transfer, optional delay and sorting in validity.
  if (powerFaultSeen || !carrierPowerReady()) return NAN;
  return (samples & 1) ? buf[samples / 2]
                       : 0.5f * (buf[samples / 2 - 1] + buf[samples / 2]);''')
replace('''  } else if (strcmp(cmd, "BAT?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_BATTERY, PGA_BATTERY,
                              BATTERY_READ_SAMPLES, BATTERY_READ_DELAY_MS);
    adsSelectChannel(MUX_SENSOR, pgaSensor);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("BAT,%.4f\\n", v * BATTERY_DIVIDER_RATIO);
''', '''  } else if (strcmp(cmd, "BAT?") == 0) {
    Serial.println("ERR,battery measurement not fitted on USB-master R1");
''')
replace('''    adsSelectChannel(MUX_SENSOR, pgaSensor);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("REF,%.5f\\n", v);''', '''    bool restored = adsSelectChannel(MUX_SENSOR, pgaSensor);
    if (!restored || powerFaultSeen || !carrierPowerReady()) {
      adsOk = false;
      Serial.println("ERR,reference read invalid or ADC restore failed");
    } else if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("REF,%.5f\\n", v);''')
replace('''    streamCount = 0;
    noInterrupts();
    streamSampleReady = false;
    streamDrdyOverruns = 0;
    streaming = true;
    interrupts();''', '''    streamCount = 0;
    noInterrupts();
    streamSampleReady = false;
    streamDrdyOverruns = 0;
    streamPowerFault = powerFaultSeen || !carrierPowerReady();
    streamPowerEpoch = carrierPowerEpoch;
    streaming = !streamPowerFault;
    interrupts();
    if (streamPowerFault || powerFaultSeen || streamPowerEpoch != carrierPowerEpoch) {
      streaming = false;
      streamPowerFault = true;
      Serial.println("ERR,carrier power changed while starting capture");
      return;
    }''')
replace('''    uint32_t overruns = streamDrdyOverruns;
    interrupts();
    Serial.printf("STREAM,END,%lu,%lu\\n", (unsigned long)streamCount,''', '''    uint32_t overruns = streamDrdyOverruns;
    streamPowerFault = streamPowerFault || powerFaultSeen ||
        streamPowerEpoch != carrierPowerEpoch || !carrierPowerReady();
    interrupts();
    // Emit the error BEFORE END: backends stop draining at the END record.
    if (streamPowerFault) {
      Serial.println("ERR,carrier power lost; capture invalid");
      if (overruns != UINT32_MAX) overruns++;
    }
    Serial.printf("STREAM,END,%lu,%lu\\n", (unsigned long)streamCount,''')
replace('// ------------------------------------------------- Arduino ----------------', '''// Stop the current operation on any captured supply fault. Hardware already
// isolates analog/SPI and emitter power independently of this software.
static void carrierPowerService() {
  static uint32_t readySinceMs = 0;
  static uint32_t nextInitMs = 0;
  if (powerFaultSeen || !carrierPowerReady()) {
    const bool wasOperating = adsOk || streaming || pwmOn || gateLevel;
    noInterrupts();
    if (streaming) streamPowerFault = true;
    streaming = false;
    streamSampleReady = false;
    interrupts();
    pwmSet(false);
    adsOk = false;
    readySinceMs = 0;
    if (wasOperating) Serial.println("ERR,carrier power lost; capture invalid");
    if (!carrierPowerReady()) return;
    // Clear the sticky event only before a fresh stable-power qualification.
    noInterrupts();
    powerFaultSeen = !carrierPowerReady();
    interrupts();
    if (powerFaultSeen) return;
  }
  if (adsOk) return;
  const uint32_t now = millis();
  if (readySinceMs == 0) readySinceMs = now ? now : 1;
  if (now - readySinceMs < 10 || (int32_t)(now - nextInitMs) < 0) return;
  nextInitMs = now + 1000;
  // Hardware already supplies its own qualification delay. This retry handles
  // ESP reset, late battery insertion and a completed sequence after boot.
  adsOk = adsInit() && adsSelectChannel(MUX_SENSOR, pgaSensor) &&
      !powerFaultSeen && carrierPowerReady();
  if (adsOk && !gotFirstCommand) Serial.println("READY,ELTEC-ESP32-ADS1256");
}

// ------------------------------------------------- Arduino ----------------''')
replace('''  gpio_deep_sleep_hold_dis();   // make sure no pad is latched from a past hold
  gateAttach(pinGate);''', '''  gpio_deep_sleep_hold_dis();   // make sure no pad is latched from a past hold
  pinMode(PIN_POWER_NOT_READY, INPUT);  // carrier logic drivesGPIO35
  attachInterrupt(digitalPinToInterrupt(PIN_POWER_NOT_READY), onCarrierPowerChange, RISING);
  gateAttach(pinGate);''')
replace('''  adsOk = adsInit();
  if (adsOk) {
    adsOk = adsSelectChannel(MUX_SENSOR, pgaSensor);
    if (adsOk) Serial.println("READY,ELTEC-ESP32-ADS1256");
    else Serial.println("ERR,ADS1256 channel select/calibration timeout");
  } else {
    Serial.println("ERR,ADS1256 init/register verification failed");
  }
  nextHelloMs = millis() + 2000;''', '''  // Bounded boot wait keeps normal host handshakes behind initialization.
  // Missing battery never hangs the serial interface indefinitely.
  const uint32_t bootStart = millis();
  while (!adsOk && millis() - bootStart < 1500) {
    carrierPowerService();
    delay(1);
  }
  if (!adsOk) Serial.println("ERR,carrier power or ADC not ready");
  nextHelloMs = millis() + 2000;''')
replace('''void loop() {
  pwmService();
  serialService();''', '''void loop() {
  carrierPowerService();
  pwmService();
  serialService();
  carrierPowerService();''')
replace('''      int32_t raw = adsReadData();
      float volts''', '''      int32_t raw = adsReadData();
      if (powerFaultSeen || !carrierPowerReady()) {
        carrierPowerService();
        return;
      }
      float volts''')
code = '''/*
  USB-MASTER R1 REVISION FIRMWARE v3.3 - engineering candidate
  This separate derived sketch is for the new carrier with GPIO35 power status.
  The historical wiring comments below describe the source rig; use the new
  carrier schematic. AIN6/7 are grounded, BAT? is unavailable, PDWN is controlled
  by hardware, and the six detector inputs use held-supply protected buffers.
  Electrical shutdown protection does not depend on this firmware running.
  Do not flash an old carrier with this sketch: its GPIO35 is not connected.
*/
''' + code
DEST.parent.mkdir(parents=True,exist_ok=True)
DEST.write_text(code,encoding='utf-8',newline='\n')
(ROOT/'firmware/source_to_r1.patch').write_text(''.join(difflib.unified_diff(
    original.splitlines(True),code.splitlines(True),
    fromfile='Arduino/Eltec/Eltec.ino',tofile='firmware/Eltec_USB_Master/Eltec_USB_Master.ino')),encoding='utf-8')
(ROOT/'reports/firmware_derivation.json').write_text(json.dumps(dict(
    source=str(SOURCE),source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    output=str(DEST.relative_to(ROOT)),output_sha256=hashlib.sha256(DEST.read_bytes()).hexdigest(),
    source_modified=False,compiled=False,flashed=False),indent=2)+'\n',encoding='utf-8')
print(DEST)
