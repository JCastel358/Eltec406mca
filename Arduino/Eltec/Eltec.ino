/*
  Eltec 406MCA emitter-tester rig — ESP32 + ADS1256 firmware
  ==========================================================

  Replaces the LabJack T7-Pro in tech_app/v4_emitter. The Ubuntu host talks to
  this board over USB serial (500000 baud, ASCII lines) instead of the LJM
  library. Job-for-job mapping from the LabJack rig:

    LabJack T7                      ESP32 + ADS1256
    ------------------------------  ------------------------------------------
    DIO0  10 Hz / 50% PWM (gate)    PIN_PWM_GATE, software-timed 10 Hz square
    AIN0  buffered sensor, +/-1 V   ADS1256 AIN0 (single-ended vs AINCOM),
          (x10 range), 1000 Hz        PGA = 2 (+/-2.5 V full scale), streamed
          stream                      at 1000 SPS
    AIN1  battery via 100k/100k     ADS1256 AIN7, PGA = 1 (+/-5 V full scale).
          divider, +/-10 V range      Battery is a 6 V 4.5 Ah SLA powering the
                                      whole rig (sensors, buffer, emitter +
                                      MOSFET module): Vbat/2 ~= 3.0-3.2 V.
                                      CAVEAT: that is right at the buffered-
                                      input linear limit (AVDD - 2 V = 3.0 V),
                                      so BAT? compresses slightly near full
                                      charge; a ~4:1 divider (300k/100k, set
                                      BATTERY_DIVIDER_RATIO = 4.0) removes it.
                                      NEVER feed the ADS1256 more than AVDD
                                      (+5 V)!
    (new) reference 406MCA sensor   ADS1256 AIN1, PGA = 2 (+/-2.5 V). Permanently
                                      mounted in the fixture; its pk-pk response
                                      to the chopped emitter is trended over time
                                      to detect emitter aging (no absolute spec,
                                      it just has to stay constant).
    AIN2  PWM looped back as sync   Not needed as an analog channel: the ESP32
                                      generates the PWM itself, so the sync
                                      state is reported as a digital 0/1 with
                                      every streamed sample.

  Counts -> volts
  ---------------
  The ADS1256 returns a 24-bit two's-complement code. Full scale is
  +/- (2 * VREF / PGA), so:

      volts = code * (2.0 * VREF / PGA) / 8388607.0        // 2^23 - 1

  With the usual 2.5 V reference: PGA 1 -> +/-5 V, PGA 2 -> +/-2.5 V.
  Every streamed line carries BOTH the raw code and the converted volts so the
  host can verify the math.

  Serial protocol (each command and reply is one \n-terminated line)
  ------------------------------------------------------------------
    IDN?             -> ELTEC-ESP32-ADS1256,v1.5
                        (v1.4 = gate confirmed on GPIO25/D25 - the perf board
                         is soldered to D25, earlier docs saying D26 were
                         wrong - plus GATE? pad readback and RTC-hold/DAC
                         release at boot. v1.5 = PIN,2 allowed: GPIO2 is the
                         onboard blue LED, so PIN,2 + GATE,ON is a meter-free
                         check that the whole gate-drive path works. Bump
                         this string on EVERY flash-relevant change so stale
                         firmware is detectable over serial.)
    PIN,<n>          -> OK,PIN,<n>       (retarget gate pin at runtime;
                                          allowed: 2/12/13/14/25/26/27/32/33;
                                          2 = onboard LED, visual gate test)
    STATUS?          -> STATUS,pwm=<0|1>,streaming=<0|1>,vref=<V>,rate=<SPS>
    PWM,ON           -> OK,PWM,ON        (starts the 10 Hz emitter drive)
    PWM,OFF          -> OK,PWM,OFF
    GATE,ON          -> OK,GATE,ON      (hold gate steady HIGH - bring-up/debug)
    GATE,OFF         -> OK,GATE,OFF
    GATE?            -> GATE,pin=<n>,drive=<0|1>,read=<0|1>
                        (drive = level the firmware is commanding, read = the
                         actual pad level via digitalRead. drive=1/read=0
                         means the pin is being held low externally: short to
                         GND, overload, or a damaged output driver.)
    BAT?             -> BAT,<volts>      (median of 12 reads, scaled by the
                                          divider ratio back to the real Vbat)
    OFFSET?          -> OFFSET,<volts>   (median of 24 DUT-sensor reads ~3 ms
                                          apart, PWM state untouched — mirrors
                                          read_offset_voltage() in the app)
    REF?             -> REF,<volts>      (same median read on the reference
                                          sensor, AIN1)
    STREAM,START     -> STREAM,BEGIN,1000,SENSOR  then one line per sample:
                          D,<t_us>,<raw_code>,<volts>,<sync 0|1>
    STREAM,START,REF -> STREAM,BEGIN,1000,REF   (same format, but streams the
                                          reference sensor on AIN1 instead)
    STREAM,STOP      -> STREAM,END,<samples_sent>
    ERR,<message>    on any bad command or hardware fault

  Wiring (30-pin ESP32 DevKit <-> blue ADS1256 module with
  5V/GND/SCLK/DIN/DOUT/DRDY/CS/PDWN header + GND/AIN0..AIN7 pairs)
  ----------------------------------------------------------------
    D18 (GPIO18)  -> SCLK        D5 (GPIO5)  -> CS
    D23 (GPIO23)  -> DIN         D4 (GPIO4)  -> DRDY (input)
    D19 (GPIO19)  -> DOUT        3V3         -> PDWN (tie high)
    VIN (5V USB)  -> 5V          GND         -> GND
    D25 (GPIO25)  -> MOSFET driver module PWM/TRIG input (emitter drive,
                     direct wire - module accepts 3.3 V logic)
    DUT sensor buffer -> AIN0, reference sensor -> AIN1,
    battery divider tap -> AIN7;
    module GND pins common with ESP32 GND and the rig ground.
    Full wiring guide: ESP32_ADS1256_Wiring.docx in this folder.
*/

#include <SPI.h>
#include "driver/gpio.h"  // gpio_hold_dis / gpio_reset_pin (GPIO25 is RTC/DAC-capable)

// ---------------------------------------------------------------- pins ----
static const int PIN_CS = 5;
static const int PIN_DRDY = 4;
// Default emitter-gate pin. The perf-board wire is soldered to D25 (GPIO25) -
// confirmed 2026-07-13; earlier notes saying D26 were wrong. Changeable at
// runtime with the PIN,<n> serial command (bring-up aid). Not persisted.
static int pinGate = 25;
// SCLK=18 / MISO=19 / MOSI=23 are the ESP32 VSPI defaults used by SPI.begin().

// ------------------------------------------------- rig constants ----------
// Mirror the Python app (eltec_406mca_emitter_tester.py / eltec_406mca_tester.py)
static const float PWM_FREQUENCY_HZ = 10.0f;   // DEFAULT_EMITTER_PWM_FREQUENCY_HZ
static const float SAMPLE_RATE_HZ = 1000.0f;   // DEFAULT_SAMPLE_RATE_HZ
static const int OFFSET_READ_SAMPLES = 24;     // OFFSET_READ_SAMPLES
static const int OFFSET_READ_DELAY_MS = 3;     // OFFSET_READ_DELAY_S
static const int BATTERY_READ_SAMPLES = 12;    // BATTERY_READ_SAMPLES
static const int BATTERY_READ_DELAY_MS = 5;    // BATTERY_READ_DELAY_S
static const float BATTERY_DIVIDER_RATIO = 2.0f;  // 100k/100k divider

// ------------------------------------------------- ADS1256 setup ----------
static const float ADS_VREF = 2.5f;            // on-board reference of the module
static const uint8_t PGA_SENSOR = 1;           // code 1 -> gain 2 -> +/-2.5 V (AIN0 DUT + AIN1 ref)
static const uint8_t PGA_BATTERY = 0;          // code 0 -> gain 1 -> +/-5 V   (AIN7)
static const uint8_t MUX_SENSOR = 0x08;        // AINP = AIN0, AINN = AINCOM (DUT sensor)
static const uint8_t MUX_REF = 0x18;           // AINP = AIN1, AINN = AINCOM (reference sensor)
static const uint8_t MUX_BATTERY = 0x78;       // AINP = AIN7, AINN = AINCOM (battery divider)
static const uint8_t DRATE_1000SPS = 0xA1;     // datasheet code for 1000 SPS

// ADS1256 command bytes
static const uint8_t CMD_WAKEUP = 0x00, CMD_RDATA = 0x01, CMD_RREG = 0x10,
                     CMD_WREG = 0x50, CMD_SELFCAL = 0xF0, CMD_SYNC = 0xFC,
                     CMD_RESET = 0xFE;
// Registers
static const uint8_t REG_STATUS = 0x00, REG_MUX = 0x01, REG_ADCON = 0x02,
                     REG_DRATE = 0x03;

static const SPISettings ADS_SPI(1500000, MSBFIRST, SPI_MODE1);

// ------------------------------------------------- state ------------------
static bool pwmOn = false;
static bool pwmLevel = false;
static uint32_t pwmNextToggleUs = 0;
static const uint32_t PWM_HALF_PERIOD_US =
    (uint32_t)(500000.0f / PWM_FREQUENCY_HZ);  // 50 ms at 10 Hz -> 50% duty

static bool streaming = false;
static uint32_t streamCount = 0;
static uint8_t streamMux = MUX_SENSOR;   // which channel STREAM,START points at
static uint8_t streamPga = PGA_SENSOR;
static char lineBuf[48];
static uint8_t lineLen = 0;

// Boot heartbeat: repeat the READY/ERR banner every 2 s until the host sends
// its first command, so a serial monitor opened late still shows signs of life.
static bool adsOk = false;
static bool gotFirstCommand = false;
static uint32_t nextHelloMs = 0;

// ------------------------------------------------- ADS1256 driver ---------
static bool waitDRDY(uint32_t timeoutMs = 200) {
  uint32_t start = millis();
  while (digitalRead(PIN_DRDY) == HIGH) {
    if (millis() - start > timeoutMs) return false;
  }
  return true;
}

static void adsCommand(uint8_t cmd) {
  SPI.beginTransaction(ADS_SPI);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(cmd);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

static void adsWriteReg(uint8_t reg, uint8_t value) {
  SPI.beginTransaction(ADS_SPI);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_WREG | reg);
  SPI.transfer(0x00);           // write a single register
  SPI.transfer(value);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

// Read one 24-bit conversion. Call only after DRDY has gone low.
static int32_t adsReadData() {
  SPI.beginTransaction(ADS_SPI);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_RDATA);
  delayMicroseconds(7);         // t6: 50 CLKIN periods at 7.68 MHz
  int32_t raw = ((int32_t)SPI.transfer(0) << 16) |
                ((int32_t)SPI.transfer(0) << 8) |
                 (int32_t)SPI.transfer(0);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  if (raw & 0x00800000L) raw |= 0xFF000000L;  // sign-extend 24 -> 32 bits
  return raw;
}

// The one line the whole rig hangs on: ADS1256 code -> volts at the input pin.
static float countsToVolts(int32_t raw, uint8_t pgaCode) {
  float gain = (float)(1 << pgaCode);              // 0->1, 1->2, 2->4 ... 6->64
  return (float)raw * (2.0f * ADS_VREF / gain) / 8388607.0f;
}

// Point the mux + PGA at a channel and let the digital filter settle.
static void adsSelectChannel(uint8_t mux, uint8_t pgaCode) {
  adsWriteReg(REG_MUX, mux);
  adsWriteReg(REG_ADCON, pgaCode & 0x07);  // clock-out off, sensor detect off
  adsCommand(CMD_SYNC);
  adsCommand(CMD_WAKEUP);
  waitDRDY();
  adsReadData();                           // discard the first settling sample
}

static bool adsInit() {
  adsCommand(CMD_RESET);
  delay(5);
  if (!waitDRDY(500)) return false;
  adsWriteReg(REG_STATUS, 0x06);           // MSB first, auto-cal on, buffer on
  adsWriteReg(REG_DRATE, DRATE_1000SPS);
  adsWriteReg(REG_ADCON, PGA_SENSOR);
  adsWriteReg(REG_MUX, MUX_SENSOR);
  adsCommand(CMD_SELFCAL);
  if (!waitDRDY(500)) return false;
  return true;
}

// Median-of-N single-channel read (offset + battery checks). Blocks; not used
// while streaming.
static float readMedianVolts(uint8_t mux, uint8_t pgaCode, int samples, int delayMs) {
  static float buf[32];
  if (samples > 32) samples = 32;
  adsSelectChannel(mux, pgaCode);
  for (int i = 0; i < samples; i++) {
    if (!waitDRDY()) return NAN;
    buf[i] = countsToVolts(adsReadData(), pgaCode);
    if (delayMs > 0) delay(delayMs);
  }
  for (int i = 1; i < samples; i++) {      // insertion sort
    float v = buf[i];
    int j = i - 1;
    while (j >= 0 && buf[j] > v) { buf[j + 1] = buf[j]; j--; }
    buf[j + 1] = v;
  }
  return (samples & 1) ? buf[samples / 2]
                       : 0.5f * (buf[samples / 2 - 1] + buf[samples / 2]);
}

// ------------------------------------------------- emitter PWM ------------
// Actual level being driven onto the gate pin, kept in sync with every write
// so GATE? can compare commanded vs. measured pad state.
static bool gateLevel = false;

static void gateWrite(bool level) {
  gateLevel = level;
  digitalWrite(pinGate, level ? HIGH : LOW);
}

// Claim a pin for the gate drive. GPIO25/26/27/32/33 are RTC-capable: an RTC
// hold latch survives soft resets and silently overrides digitalWrite, and
// GPIO25/26 double as DAC outputs — release/detach all of that before use.
static void gateAttach(int pin) {
  gpio_hold_dis((gpio_num_t)pin);
  gpio_reset_pin((gpio_num_t)pin);   // back to plain GPIO-matrix digital pad
  pinGate = pin;
  pinMode(pinGate, OUTPUT);
  gateWrite(false);
}

// Software-timed square wave: at 10 Hz the loop() turnaround (<<1 ms) gives
// far less than 1% period jitter, and the drive level doubles as the sync bit.
static void pwmService() {
  if (!pwmOn) return;
  uint32_t now = micros();
  if ((int32_t)(now - pwmNextToggleUs) >= 0) {
    pwmLevel = !pwmLevel;
    gateWrite(pwmLevel);
    pwmNextToggleUs += PWM_HALF_PERIOD_US;
  }
}

static void pwmSet(bool on) {
  pwmOn = on;
  pwmLevel = false;
  gateWrite(false);
  if (on) pwmNextToggleUs = micros() + PWM_HALF_PERIOD_US;
}

// ------------------------------------------------- commands ---------------
static void handleCommand(char *cmd) {
  gotFirstCommand = true;
  if (strcmp(cmd, "IDN?") == 0) {
    Serial.println("ELTEC-ESP32-ADS1256,v1.5");

  } else if (strcmp(cmd, "STATUS?") == 0) {
    Serial.printf("STATUS,pwm=%d,streaming=%d,vref=%.3f,rate=%d\n",
                  pwmOn ? 1 : 0, streaming ? 1 : 0, ADS_VREF,
                  (int)SAMPLE_RATE_HZ);

  } else if (strcmp(cmd, "PWM,ON") == 0) {
    pwmSet(true);
    Serial.println("OK,PWM,ON");

  } else if (strcmp(cmd, "PWM,OFF") == 0) {
    pwmSet(false);
    Serial.println("OK,PWM,OFF");

  // Hardware bring-up helpers: hold the emitter gate steady so the drive path
  // can be checked with a multimeter / by eye (no 10 Hz shimmer to squint at).
  } else if (strcmp(cmd, "GATE,ON") == 0) {
    pwmOn = false;
    gateWrite(true);
    Serial.println("OK,GATE,ON");

  } else if (strcmp(cmd, "GATE,OFF") == 0) {
    pwmOn = false;
    gateWrite(false);
    Serial.println("OK,GATE,OFF");

  // GATE?: compare commanded vs. actual pad level. arduino-esp32 defines
  // OUTPUT with the input buffer enabled, so digitalRead returns the real
  // pad state: drive=1/read=0 => the pin is being held low externally
  // (short to GND, overload, or a blown output driver).
  } else if (strcmp(cmd, "GATE?") == 0) {
    Serial.printf("GATE,pin=%d,drive=%d,read=%d\n",
                  pinGate, gateLevel ? 1 : 0,
                  digitalRead(pinGate) == HIGH ? 1 : 0);

  // PIN,<n>: retarget the gate drive at runtime (bring-up aid). Only pins that
  // are safe outputs and not used by SPI/DRDY/CS are allowed. Not persisted.
  } else if (strncmp(cmd, "PIN,", 4) == 0) {
    int n = atoi(cmd + 4);
    // 2 = onboard blue LED (strapping pin, but safe as an output after boot):
    // PIN,2 + GATE,ON lets the gate path be verified by eye, no meter needed.
    static const int allowed[] = {2, 12, 13, 14, 25, 26, 27, 32, 33};
    bool ok = false;
    for (unsigned i = 0; i < sizeof(allowed) / sizeof(allowed[0]); i++)
      if (allowed[i] == n) ok = true;
    if (!ok) {
      Serial.printf("ERR,pin %d not allowed (use 2/12/13/14/25/26/27/32/33)\n", n);
    } else {
      pwmOn = false;
      gateWrite(false);                    // release the old pin, drive low
      gateAttach(n);
      Serial.printf("OK,PIN,%d\n", n);
    }

  } else if (strcmp(cmd, "BAT?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_BATTERY, PGA_BATTERY,
                              BATTERY_READ_SAMPLES, BATTERY_READ_DELAY_MS);
    adsSelectChannel(MUX_SENSOR, PGA_SENSOR);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("BAT,%.4f\n", v * BATTERY_DIVIDER_RATIO);

  } else if (strcmp(cmd, "OFFSET?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_SENSOR, PGA_SENSOR,
                              OFFSET_READ_SAMPLES, OFFSET_READ_DELAY_MS);
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("OFFSET,%.5f\n", v);

  } else if (strcmp(cmd, "REF?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_REF, PGA_SENSOR,
                              OFFSET_READ_SAMPLES, OFFSET_READ_DELAY_MS);
    adsSelectChannel(MUX_SENSOR, PGA_SENSOR);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("REF,%.5f\n", v);

  } else if (strcmp(cmd, "STREAM,START") == 0 ||
             strcmp(cmd, "STREAM,START,REF") == 0) {
    bool refChannel = (strcmp(cmd, "STREAM,START,REF") == 0);
    streamMux = refChannel ? MUX_REF : MUX_SENSOR;
    streamPga = PGA_SENSOR;                     // both sensors use gain 2
    adsSelectChannel(streamMux, streamPga);
    streaming = true;
    streamCount = 0;
    Serial.printf("STREAM,BEGIN,%d,%s\n", (int)SAMPLE_RATE_HZ,
                  refChannel ? "REF" : "SENSOR");

  } else if (strcmp(cmd, "STREAM,STOP") == 0) {
    streaming = false;
    Serial.printf("STREAM,END,%lu\n", (unsigned long)streamCount);

  } else if (cmd[0] != '\0') {
    Serial.printf("ERR,unknown command: %s\n", cmd);
  }
}

static void serialService() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        lineLen = 0;
        handleCommand(lineBuf);
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

// ------------------------------------------------- Arduino ----------------
void setup() {
  gpio_deep_sleep_hold_dis();   // make sure no pad is latched from a past hold
  gateAttach(pinGate);
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_DRDY, INPUT);

  Serial.begin(500000);
  SPI.begin();

  adsOk = adsInit();
  if (adsOk) {
    adsSelectChannel(MUX_SENSOR, PGA_SENSOR);
    Serial.println("READY,ELTEC-ESP32-ADS1256");
  } else {
    Serial.println("ERR,ADS1256 not responding (check wiring/DRDY)");
  }
  nextHelloMs = millis() + 2000;
}

void loop() {
  pwmService();
  serialService();

  if (!gotFirstCommand && !streaming && (int32_t)(millis() - nextHelloMs) >= 0) {
    if (adsOk) Serial.println("READY,ELTEC-ESP32-ADS1256");
    else Serial.println("ERR,ADS1256 not responding (check wiring/DRDY)");
    nextHelloMs = millis() + 2000;
  }

  // Streaming: the ADS1256 clocks itself at exactly 1000 SPS; every DRDY low
  // edge is one fresh conversion of the selected channel (AIN0 DUT or AIN1
  // reference). Sync is our own PWM drive level.
  if (streaming && digitalRead(PIN_DRDY) == LOW) {
    int32_t raw = adsReadData();
    float volts = countsToVolts(raw, streamPga);
    Serial.printf("D,%lu,%ld,%.6f,%d\n",
                  (unsigned long)micros(), (long)raw, volts,
                  (pwmOn && pwmLevel) ? 1 : 0);
    streamCount++;
  }
}
