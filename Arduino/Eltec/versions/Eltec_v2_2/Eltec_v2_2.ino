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
          (x10 range), 1000 Hz        PGA = 1 (+/-5 V full scale, v2.0),
          stream                      streamed at 1000 SPS. Input buffer OFF
                                      (v2.0), so the 405 M22's TP412 offset
                                      band of 0.8-3.0 V reads linearly (the
                                      old gain-2 buffered front end clipped
                                      at 2.5 V). AIN0/AIN1 are driven by
                                      low-impedance op-amp buffers, so the
                                      unbuffered input load is fine there.
    AIN1  battery via measured       ADS1256 AIN7, PGA = 1 (+/-5 V full scale).
          99.7k/99.6k divider,         CAVEAT (v2.0): with the input buffer
          +/-10 V range                OFF the unbuffered ADS1256 input loads
                                      the ~50k divider, so BAT? reads LOW and
                                      is only indicative. The current fixture
                                      (2026-08-12) does not use it anyway: the
                                      6.5 V battery drives the emitters ONLY
                                      and the 9 V sensor battery is not on
                                      AIN7 (planned: AIN6 with a >=4:1
                                      divider). NEVER feed the ADS1256 more
                                      than AVDD (+5 V)!
    (new) reference 406MCA sensor   ADS1256 AIN1, PGA = 1 (+/-5 V, v2.0).
                                      Permanently mounted in the fixture; its
                                      pk-pk response to the chopped emitter is
                                      trended over time to detect emitter aging
                                      (no absolute spec, it just has to stay
                                      constant).
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
    IDN?             -> ELTEC-ESP32-ADS1256,v2.2
                        (v2.2 = dual-channel interleaved streaming via
                         STREAM,START,BOTH, for the two-detector IR telescope
                         (direction of travel from the AIN0/AIN1 lag). Purely
                         additive: single-channel STREAM,START and
                         STREAM,START,REF are byte-for-byte unchanged, so the
                         406MCA and 405 M22 apps see no difference. Nothing
                         about the front end changed either, so a v2.2 board
                         is a drop-in replacement for a v2.1 one.
                         v2.1 = runtime-selectable ADS1256 front end via the
                         FE,... commands below, so the v1.9 (gain 2, buffer
                         ON) and v2.0 (gain 1, buffer OFF) configurations can
                         be A/B-compared on the same board without reflashing
                         - e.g. to check whether a noise reading depends on
                         the front end. Boots in the v2.0 configuration, and
                         opening the USB port resets the board, so hosts that
                         do not send FE commands (the 405 M22 app) always get
                         v2.0 behavior. Not persisted.
                         v2.0 = ADS1256 front end changed for the 405 M22
                         TP412 offset band: sensor channels AIN0/AIN1 run at
                         PGA gain 1 (+/-5 V, LSB 596 nV instead of 298 nV)
                         and the input buffer is OFF, so DC offsets to 3.0 V+
                         read linearly. WARNING: 406MCA v6/v6.1 rigs were
                         qualified on v1.9's gain-2 buffered front end - keep
                         them on v1.9 unless their noise floors/thresholds
                         are re-verified on v2.0. BAT? accuracy also drops
                         (unbuffered input loads the resistive divider); the
                         405 M22 host disables its battery gate.
                         v1.9 = runtime-selectable emitter PWM frequency via
                         PWM,FREQ,<hz> (0.1-20 Hz). Boot default stays 10 Hz,
                         so the existing 406MCA apps behave identically without
                         sending the new command; the 405 M22 tools send
                         PWM,FREQ,1 for that model's 1 Hz drive.
                         v1.8 = battery scaling uses the measured divider:
                         99.7 kOhm upper / 99.6 kOhm lower. The 100 nF filter
                         capacitor across the lower resistor does not change
                         the DC divider ratio. v1.4 = gate confirmed on
                         GPIO25/D25 - the perf board
                         is soldered to D25, earlier docs saying D26 were
                         wrong - plus GATE? pad readback and RTC-hold/DAC
                         release at boot. v1.5 = PIN,2 allowed: GPIO2 is the
                         onboard blue LED, so PIN,2 + GATE,ON is a meter-free
                         check that the whole gate-drive path works. v1.6 was
                         an interim DRDY-edge fix. v1.7 also verifies the ADC
                         configuration, respects command timing, and reports
                         stream overruns. Older builds could leave the ADS1256
                         at its 30 kSPS reset default while claiming 1 kSPS.
                         Bump this string on EVERY
                         flash-relevant change so stale firmware is detectable.)
    PIN,<n>          -> OK,PIN,<n>       (retarget gate pin at runtime;
                                          allowed: 2/12/13/14/25/26/27/32/33;
                                          2 = onboard LED, visual gate test)
    STATUS?          -> STATUS,pwm=<0|1>,streaming=<0|1>,vref=<V>,rate=<SPS>,pwm_hz=<Hz>,dual=<0|1>
                        (rate is the single-channel 1000 SPS; dual=1 means the
                         running stream is the interleaved BOTH mode, whose
                         real per-channel rate is ~424 SPS)
    FE?              -> FE,gain=<1|2>,buf=<0|1>,fs=<V>
                        (current sensor front end; fs = full-scale volts)
    FE,V20           -> OK,FE,gain=1,buf=0  (gain 1, buffer OFF - the v2.0
                                          405 M22 front end; boot default)
    FE,V19           -> OK,FE,gain=2,buf=1  (gain 2, buffer ON - the v1.9
                                          406MCA front end, for A/B noise
                                          comparison. CAVEAT: full scale
                                          drops to +/-2.5 V and the buffer
                                          is linear only to AVDD-2V = 3.0 V,
                                          so offsets above ~2.4 V clip -
                                          exactly why v2.0 exists. Applies
                                          to AIN7/BAT? too, like real v1.9.)
    FE,GAIN,<1|2>    -> OK,FE,...        (change only the PGA gain)
    FE,BUF,<0|1>     -> OK,FE,...        (change only the input buffer)
                        (all FE setters: rejected while streaming; the new
                         configuration is SELFCALed and read back before OK;
                         none of it is persisted - a reset returns to v2.0)
    PWM,ON           -> OK,PWM,ON        (starts the emitter drive at the
                                          current frequency; 10 Hz unless
                                          changed with PWM,FREQ)
    PWM,OFF          -> OK,PWM,OFF
    PWM,FREQ,<hz>    -> OK,PWM,FREQ,<hz> (set drive frequency, 0.1-20 Hz,
                                          50% duty. Takes effect immediately;
                                          if the PWM is running its phase is
                                          restarted. Not persisted - boots
                                          back to 10 Hz.)
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
    STREAM,START,BOTH-> STREAM,BEGIN,<per_ch_SPS>,BOTH   (v2.2: AIN0 AND AIN1
                                          interleaved, for the two-detector IR
                                          telescope. One line per PAIR:
                          P,<t0_us>,<v0>,<t1_us>,<v1>,<sync 0|1>
                                          t0/v0 = AIN0, t1/v1 = AIN1. The two
                                          halves are NOT simultaneous - they are
                                          one ADC settling time apart (~1.2 ms),
                                          which is why each carries its own
                                          timestamp: a host measuring the
                                          channel-to-channel lag must interpolate
                                          onto a common time base using t0/t1,
                                          not assume a shared sample instant.
                                          Raw codes are omitted to keep the line
                                          short - two channels at 500000 baud
                                          leaves less headroom, and USB-serial
                                          overflow is the rig's known failure
                                          mode. Per-channel rate is ~424 SPS,
                                          not 1000: cycling the mux restarts the
                                          conversion, so each sample costs a full
                                          settling time. Treat the announced rate
                                          as nominal and measure the real one
                                          from the timestamps.)
    STREAM,STOP      -> STREAM,END,<samples_sent>,<adc_overruns>
                        (adc_overruns must be zero for a valid measurement.
                         In BOTH mode samples_sent counts PAIRS, not
                         conversions.)
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
    Current wiring guide: ESP32_ADS1256_Wiring_v2_0.md in this folder
    (ESP32_ADS1256_Wiring_v1_7.md matches the v1.9 firmware still used on
    406MCA rigs). ESP32_ADS1256_Wiring.docx is historical (old 9 V/AIN1
    fixture); do not use it to wire this version.
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
// Boot default. Changeable at runtime with PWM,FREQ,<hz> (405 M22 uses 1 Hz).
static const float PWM_DEFAULT_FREQUENCY_HZ = 10.0f;  // DEFAULT_EMITTER_PWM_FREQUENCY_HZ
static const float PWM_MIN_FREQUENCY_HZ = 0.1f;
static const float PWM_MAX_FREQUENCY_HZ = 20.0f;
static const float SAMPLE_RATE_HZ = 1000.0f;   // DEFAULT_SAMPLE_RATE_HZ
// v2.2 dual-channel (STREAM,START,BOTH). The ADS1256 has ONE converter behind
// an input mux, so two channels are read by cycling the mux. Every mux change
// needs a SYNC, which restarts the conversion and costs a full settling time -
// 1.18 ms at DRATE 1000 SPS (datasheet "settling time", single-cycle), not the
// 1 ms conversion period. Two channels per round trip therefore yield
// ~424 SPS PER CHANNEL. Announced to the host as nominal only: the host must
// derive the real rate from the timestamps it receives.
static const float DUAL_SETTLE_MS = 1.18f;
static const float DUAL_SAMPLE_RATE_HZ = 1000.0f / (2.0f * DUAL_SETTLE_MS);
static const int OFFSET_READ_SAMPLES = 24;     // OFFSET_READ_SAMPLES
static const int OFFSET_READ_DELAY_MS = 3;     // OFFSET_READ_DELAY_S
static const int BATTERY_READ_SAMPLES = 12;    // BATTERY_READ_SAMPLES
static const int BATTERY_READ_DELAY_MS = 5;    // BATTERY_READ_DELAY_S
// Measured installed divider values. R_TOP runs from battery+ to the AIN7 tap;
// R_BOTTOM runs from the tap to ground. The 100 nF capacitor in parallel with
// R_BOTTOM filters noise but has no effect on the steady-state voltage ratio.
static const float BATTERY_DIVIDER_R_TOP_OHMS = 99700.0f;
static const float BATTERY_DIVIDER_R_BOTTOM_OHMS = 99600.0f;
static const float BATTERY_DIVIDER_RATIO =
    (BATTERY_DIVIDER_R_TOP_OHMS + BATTERY_DIVIDER_R_BOTTOM_OHMS) /
    BATTERY_DIVIDER_R_BOTTOM_OHMS;  // 2.001004016...

// ------------------------------------------------- ADS1256 setup ----------
static const float ADS_VREF = 2.5f;            // on-board reference of the module
// v2.0: gain 1 so the 405 M22's 0.8-3.0 V TP412 offset band (plus noise
// excursions above it) reads linearly on AIN0/AIN1. LSB doubles to 596 nV -
// still ~168 counts per 0.1 mV stability threshold.
// v2.1: these became runtime state so FE,V19 / FE,V20 can A/B-compare the
// two qualified front ends without reflashing. Boot defaults = v2.0; not
// persisted (any reset - including the DTR toggle when a host opens the
// port - returns to v2.0).
static uint8_t pgaSensor = 0;                  // code 0 -> gain 1 -> +/-5 V (AIN0 DUT + AIN1 ref)
static bool adsBufferOn = false;               // ADS1256 STATUS BUFEN (v1.9 = on, v2.0 = off)
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
static float pwmFrequencyHz = PWM_DEFAULT_FREQUENCY_HZ;
// 50% duty: half period in us (50 ms at 10 Hz, 500 ms at 1 Hz).
static uint32_t pwmHalfPeriodUs =
    (uint32_t)(500000.0f / PWM_DEFAULT_FREQUENCY_HZ);

static volatile bool streaming = false;
static uint32_t streamCount = 0;
static uint8_t streamMux = MUX_SENSOR;   // which channel STREAM,START points at
static uint8_t streamPga = 0;            // latched from pgaSensor at STREAM,START
// v2.2 dual-channel state. streamDualPending is the channel whose conversion
// the NEXT read will return - not the channel the mux currently points at. The
// two run one step apart because the mux is advanced BEFORE the finished
// conversion is read (see the datasheet's mux-cycling sequence in loop()).
static bool streamDual = false;
static uint8_t streamDualPending = MUX_SENSOR;
static bool streamPairHaveA = false;     // AIN0 half collected, waiting on AIN1
static uint32_t streamPairTimestampUs = 0;
static float streamPairVoltsA = 0.0f;
// DRDY is an active-low level. A GPIO interrupt latches each real falling edge
// so Serial.printf() cannot make loop() miss the short HIGH phase between ADC
// conversions. If a second conversion arrives before the previous one was
// consumed, record an overrun: the data register only retains the newest value,
// so the host must reject that capture rather than silently use missing data.
static volatile bool streamSampleReady = false;
static volatile uint32_t streamSampleTimestampUs = 0;
static volatile uint32_t streamDrdyOverruns = 0;
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
  // The longest command-to-command t11 used here is 24 CLKIN periods after
  // SYNC (~3.13 us at 7.68 MHz). Keep CS low for 4 us so a following command
  // cannot arrive early.
  delayMicroseconds(4);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

static void adsWriteReg(uint8_t reg, uint8_t value) {
  SPI.beginTransaction(ADS_SPI);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_WREG | reg);
  SPI.transfer(0x00);           // write a single register
  SPI.transfer(value);
  // WREG t10: keep /CS low for at least 8 CLKIN periods (~1.04 us).
  delayMicroseconds(2);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

static uint8_t adsReadReg(uint8_t reg) {
  SPI.beginTransaction(ADS_SPI);
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_RREG | reg);
  SPI.transfer(0x00);           // read a single register
  delayMicroseconds(7);         // RREG t6: 50 CLKIN periods (~6.51 us)
  uint8_t value = SPI.transfer(0);
  delayMicroseconds(2);         // RREG t10
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  return value;
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
  // ADS1256 t10 requires /CS to remain low for at least 8 CLKIN periods after
  // the final data SCLK. At 7.68 MHz that is ~1.04 us; use 2 us of margin.
  delayMicroseconds(2);
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
static bool adsSelectChannel(uint8_t mux, uint8_t pgaCode) {
  adsWriteReg(REG_MUX, mux);
  adsWriteReg(REG_ADCON, pgaCode & 0x07);  // clock-out off, sensor detect off
  // Auto-calibration is deliberately disabled in STATUS: issuing it explicitly
  // here makes PGA changes deterministic and lets us wait for completion.
  adsCommand(CMD_SELFCAL);
  if (!waitDRDY(500)) return false;
  adsCommand(CMD_SYNC);
  adsCommand(CMD_WAKEUP);
  if (!waitDRDY()) return false;
  adsReadData();                           // discard the first settling sample
  return true;
}

// STATUS register value for the current front end: MSB first, auto-cal OFF,
// BUFEN per adsBufferOn. (ACAL stays off: the old 0x06 enabled ACAL and then
// wrote more registers while calibration was still busy, so DRATE could
// remain at its 30 kSPS reset value.)
static uint8_t adsStatusRegValue() { return adsBufferOn ? 0x02 : 0x00; }

static bool adsInit() {
  adsCommand(CMD_RESET);
  delay(5);
  if (!waitDRDY(500)) return false;
  // Boot default front end is v2.0: gain 1, input buffer OFF. The buffer's
  // AVDD-2 V (3.0 V) linear ceiling blocked the 405 M22's 0.8-3.0 V offset
  // band; the sensor channels are op-amp buffered externally, so the
  // unbuffered switched-cap input load is fine there. FE,V19 switches back
  // to the gain-2 buffered v1.9 front end at runtime (A/B comparison).
  adsWriteReg(REG_STATUS, adsStatusRegValue());
  adsWriteReg(REG_DRATE, DRATE_1000SPS);
  adsWriteReg(REG_ADCON, pgaSensor);
  adsWriteReg(REG_MUX, MUX_SENSOR);
  adsCommand(CMD_SELFCAL);
  if (!waitDRDY(500)) return false;
  // Never stream on assumed settings: read back every register that controls
  // channel, PGA, buffering, or sample rate.
  uint8_t status = adsReadReg(REG_STATUS);
  uint8_t mux = adsReadReg(REG_MUX);
  uint8_t adcon = adsReadReg(REG_ADCON);
  uint8_t drate = adsReadReg(REG_DRATE);
  // BUFEN (bit 1) must match the requested front end; ACAL (bit 2) always 0.
  return (status & 0x06) == adsStatusRegValue() && mux == MUX_SENSOR &&
         (adcon & 0x67) == pgaSensor && drate == DRATE_1000SPS;
}

// v2.1: apply the current pgaSensor/adsBufferOn front end and verify it took.
// SELFCAL runs via adsSelectChannel so offset/gain calibration matches the
// new configuration before anything is measured on it.
static bool adsApplyFrontEnd() {
  adsWriteReg(REG_STATUS, adsStatusRegValue());
  if (!adsSelectChannel(MUX_SENSOR, pgaSensor)) return false;
  uint8_t status = adsReadReg(REG_STATUS);
  uint8_t adcon = adsReadReg(REG_ADCON);
  return (status & 0x06) == adsStatusRegValue() &&
         (adcon & 0x67) == pgaSensor;
}

// Median-of-N single-channel read (offset + battery checks). Blocks; not used
// while streaming.
static float readMedianVolts(uint8_t mux, uint8_t pgaCode, int samples, int delayMs) {
  static float buf[32];
  if (samples > 32) samples = 32;
  if (!adsSelectChannel(mux, pgaCode)) return NAN;
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

// Software-timed square wave: the loop() turnaround (<<1 ms) gives far less
// than 1% period jitter at any allowed frequency (0.1-20 Hz), and the drive
// level doubles as the sync bit.
static void pwmService() {
  if (!pwmOn) return;
  uint32_t now = micros();
  if ((int32_t)(now - pwmNextToggleUs) >= 0) {
    pwmLevel = !pwmLevel;
    gateWrite(pwmLevel);
    pwmNextToggleUs += pwmHalfPeriodUs;
  }
}

static void pwmSet(bool on) {
  pwmOn = on;
  pwmLevel = false;
  gateWrite(false);
  if (on) pwmNextToggleUs = micros() + pwmHalfPeriodUs;
}

// Change the drive frequency. If the PWM is running, restart its phase so the
// first full cycle after the change is clean (no torn half-period).
static void pwmSetFrequency(float hz) {
  pwmFrequencyHz = hz;
  pwmHalfPeriodUs = (uint32_t)(500000.0f / hz);
  if (pwmOn) pwmSet(true);
}

// Latch conversion-ready events independently of the serial-output latency.
// micros() is ISR-safe on ESP32 and timestamps the ADC edge rather than the
// later moment when loop() gets around to formatting the record.
static void IRAM_ATTR onAdsDrdyFalling() {
  if (!streaming) return;
  if (streamSampleReady) streamDrdyOverruns++;
  streamSampleTimestampUs = micros();
  streamSampleReady = true;
}

// ------------------------------------------------- commands ---------------
static void handleCommand(char *cmd) {
  gotFirstCommand = true;
  if (strcmp(cmd, "IDN?") == 0) {
    Serial.println("ELTEC-ESP32-ADS1256,v2.2");

  } else if (strcmp(cmd, "STATUS?") == 0) {
    // rate stays the single-channel 1000 SPS; dual=1 means the active stream is
    // the interleaved BOTH mode, whose real per-channel rate is ~424 SPS.
    Serial.printf("STATUS,pwm=%d,streaming=%d,vref=%.3f,rate=%d,pwm_hz=%.3f,"
                  "dual=%d\n",
                  pwmOn ? 1 : 0, streaming ? 1 : 0, ADS_VREF,
                  (int)SAMPLE_RATE_HZ, pwmFrequencyHz, streamDual ? 1 : 0);

  // FE? / FE,...: v2.1 runtime front-end selection (A/B noise comparison
  // between the v1.9 and v2.0 qualified configurations - see header).
  } else if (strcmp(cmd, "FE?") == 0) {
    Serial.printf("FE,gain=%d,buf=%d,fs=%.3f\n", 1 << pgaSensor,
                  adsBufferOn ? 1 : 0,
                  2.0f * ADS_VREF / (float)(1 << pgaSensor));

  } else if (strncmp(cmd, "FE,", 3) == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    uint8_t newPga = pgaSensor;
    bool newBuf = adsBufferOn;
    if (strcmp(cmd, "FE,V20") == 0) {            // v2.0: gain 1, buffer off
      newPga = 0; newBuf = false;
    } else if (strcmp(cmd, "FE,V19") == 0) {     // v1.9: gain 2, buffer on
      newPga = 1; newBuf = true;
    } else if (strcmp(cmd, "FE,GAIN,1") == 0) {
      newPga = 0;
    } else if (strcmp(cmd, "FE,GAIN,2") == 0) {
      newPga = 1;
    } else if (strcmp(cmd, "FE,BUF,0") == 0) {
      newBuf = false;
    } else if (strcmp(cmd, "FE,BUF,1") == 0) {
      newBuf = true;
    } else {
      Serial.printf("ERR,bad FE command: %s (use FE,V19 / FE,V20 / "
                    "FE,GAIN,<1|2> / FE,BUF,<0|1>)\n", cmd);
      return;
    }
    uint8_t oldPga = pgaSensor;
    bool oldBuf = adsBufferOn;
    pgaSensor = newPga;
    adsBufferOn = newBuf;
    if (adsApplyFrontEnd()) {
      Serial.printf("OK,FE,gain=%d,buf=%d\n", 1 << pgaSensor,
                    adsBufferOn ? 1 : 0);
    } else {
      // Verification failed: put the previous configuration back so the
      // reported state always matches the silicon.
      pgaSensor = oldPga;
      adsBufferOn = oldBuf;
      adsApplyFrontEnd();
      Serial.println("ERR,front-end apply/verify failed (previous config restored)");
    }

  } else if (strcmp(cmd, "PWM,ON") == 0) {
    pwmSet(true);
    Serial.println("OK,PWM,ON");

  } else if (strcmp(cmd, "PWM,OFF") == 0) {
    pwmSet(false);
    Serial.println("OK,PWM,OFF");

  // PWM,FREQ,<hz>: runtime drive frequency (405 M22 = 1 Hz, 406MCA = 10 Hz).
  // Not persisted; the board boots back to the 10 Hz default.
  } else if (strncmp(cmd, "PWM,FREQ,", 9) == 0) {
    float hz = atof(cmd + 9);
    if (!(hz >= PWM_MIN_FREQUENCY_HZ && hz <= PWM_MAX_FREQUENCY_HZ)) {
      Serial.printf("ERR,frequency %.3f out of range (%.1f-%.1f Hz)\n",
                    hz, PWM_MIN_FREQUENCY_HZ, PWM_MAX_FREQUENCY_HZ);
    } else {
      pwmSetFrequency(hz);
      Serial.printf("OK,PWM,FREQ,%.3f\n", hz);
    }

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
    adsSelectChannel(MUX_SENSOR, pgaSensor);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("BAT,%.4f\n", v * BATTERY_DIVIDER_RATIO);

  } else if (strcmp(cmd, "OFFSET?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_SENSOR, pgaSensor,
                              OFFSET_READ_SAMPLES, OFFSET_READ_DELAY_MS);
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("OFFSET,%.5f\n", v);

  } else if (strcmp(cmd, "REF?") == 0) {
    if (streaming) { Serial.println("ERR,stop stream first"); return; }
    float v = readMedianVolts(MUX_REF, pgaSensor,
                              OFFSET_READ_SAMPLES, OFFSET_READ_DELAY_MS);
    adsSelectChannel(MUX_SENSOR, pgaSensor);   // leave mux ready for streaming
    if (isnan(v)) Serial.println("ERR,ADS1256 timeout");
    else Serial.printf("REF,%.5f\n", v);

  } else if (strcmp(cmd, "STREAM,START") == 0 ||
             strcmp(cmd, "STREAM,START,REF") == 0 ||
             strcmp(cmd, "STREAM,START,BOTH") == 0) {
    bool refChannel = (strcmp(cmd, "STREAM,START,REF") == 0);
    bool bothChannels = (strcmp(cmd, "STREAM,START,BOTH") == 0);
    // BOTH always begins on AIN0, so the first pair half is the DUT/right
    // detector and the emitted pair order matches the line format.
    streamMux = refChannel ? MUX_REF : MUX_SENSOR;
    streamPga = pgaSensor;                     // latch the active front end's gain
    if (!adsSelectChannel(streamMux, streamPga)) {
      Serial.println("ERR,ADS1256 channel select/calibration timeout");
      return;
    }
    streamCount = 0;
    streamDual = bothChannels;
    streamDualPending = MUX_SENSOR;            // AIN0 is converting right now
    streamPairHaveA = false;
    noInterrupts();
    streamSampleReady = false;
    streamDrdyOverruns = 0;
    streaming = true;
    interrupts();
    if (bothChannels) {
      Serial.printf("STREAM,BEGIN,%d,BOTH\n", (int)(DUAL_SAMPLE_RATE_HZ + 0.5f));
    } else {
      Serial.printf("STREAM,BEGIN,%d,%s\n", (int)SAMPLE_RATE_HZ,
                    refChannel ? "REF" : "SENSOR");
    }

  } else if (strcmp(cmd, "STREAM,STOP") == 0) {
    noInterrupts();
    streaming = false;
    streamSampleReady = false;
    uint32_t overruns = streamDrdyOverruns;
    interrupts();
    bool wasDual = streamDual;
    streamDual = false;
    streamPairHaveA = false;
    Serial.printf("STREAM,END,%lu,%lu\n", (unsigned long)streamCount,
                  (unsigned long)overruns);
    // Leave the mux where every other command expects to find it. Without this
    // a BOTH stream could stop with AIN1 selected and the next OFFSET?/stream
    // would start on the wrong channel.
    if (wasDual) adsSelectChannel(MUX_SENSOR, pgaSensor);

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
  attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onAdsDrdyFalling, FALLING);

  Serial.begin(500000);
  SPI.begin();

  adsOk = adsInit();
  if (adsOk) {
    adsOk = adsSelectChannel(MUX_SENSOR, pgaSensor);
    if (adsOk) Serial.println("READY,ELTEC-ESP32-ADS1256");
    else Serial.println("ERR,ADS1256 channel select/calibration timeout");
  } else {
    Serial.println("ERR,ADS1256 init/register verification failed");
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

  // Streaming: consume each interrupt-latched ADS1256 falling edge once. The
  // short critical section prevents an edge between testing and clearing the
  // ready flag from being lost; SPI and serial work remain outside it.
  if (streaming) {
    bool sampleReady;
    uint32_t sampleTimestampUs;
    noInterrupts();
    sampleReady = streamSampleReady;
    sampleTimestampUs = streamSampleTimestampUs;
    streamSampleReady = false;
    interrupts();
    if (sampleReady && !streamDual) {
      int32_t raw = adsReadData();
      float volts = countsToVolts(raw, streamPga);
      Serial.printf("D,%lu,%ld,%.6f,%d\n",
                    (unsigned long)sampleTimestampUs, (long)raw, volts,
                    (pwmOn && pwmLevel) ? 1 : 0);
      streamCount++;
    } else if (sampleReady) {
      // Dual channel (v2.2). READ FIRST, switch the mux after.
      //
      // The obvious optimisation is the other order - point the mux at the next
      // channel and SYNC/WAKEUP first, so its ~1.2 ms settling overlaps the SPI
      // read of the conversion that just finished. That is measurably WRONG on
      // this board: verified 2026-08-17 against the same physical input, AIN0
      // is pristine in single-channel mode (sample-to-sample step max 6 mV, no
      // full-scale reads in 8000 samples) but with SYNC before RDATA it showed
      // 4.8 V single-sample jumps and hit +full-scale 7 times in 8474 pairs.
      // SYNC restarts the converter, and the output register is evidently not
      // safe to read across that. Reading before touching anything costs ~50 us
      // of settling overlap per pair - about 2% of throughput - and removes the
      // corruption entirely.
      float volts = countsToVolts(adsReadData(), streamPga);
      uint8_t nextMux =
          (streamDualPending == MUX_SENSOR) ? MUX_REF : MUX_SENSOR;
      adsWriteReg(REG_MUX, nextMux);
      adsCommand(CMD_SYNC);
      adsCommand(CMD_WAKEUP);
      if (streamDualPending == MUX_SENSOR) {
        streamPairTimestampUs = sampleTimestampUs;   // AIN0 half - hold it
        streamPairVoltsA = volts;
        streamPairHaveA = true;
      } else if (streamPairHaveA) {
        // AIN1 half completes the pair. Both timestamps go out: the halves are
        // one settling time apart and the host needs that to measure the
        // channel-to-channel lag without a built-in bias. The sync bit is
        // sampled once, at emission.
        Serial.printf("P,%lu,%.6f,%lu,%.6f,%d\n",
                      (unsigned long)streamPairTimestampUs, streamPairVoltsA,
                      (unsigned long)sampleTimestampUs, volts,
                      (pwmOn && pwmLevel) ? 1 : 0);
        streamCount++;
        streamPairHaveA = false;
      }
      streamDualPending = nextMux;
    }
  }
}
