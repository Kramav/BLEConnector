// =====================================================================
//  OpenLog Artemis -> BLE accelerometer streamer
//
//  Streams ICM-20948 accelerometer X/Y/Z at 281.25 Hz over BLE.
//  No SD card. No menu. Lossless within the ring buffer's depth.
//
//  Board: OpenLog Artemis V10  (FQBN SparkFun:apollo3:sfe_artemis_atp)
//  Build: see Dockerfile.accel -- ArduinoBLE MUST be 1.1.3
//
//  Wire format (20-byte notification on the data characteristic):
//      uint16  seq            little-endian packet counter
//      int16   ax, ay, az     sample n
//      int16   ax, ay, az     sample n+1
//      int16   ax, ay, az     sample n+2
//
//  Status (14-byte notification, once per second):
//      uint32  totalSamples
//      uint32  droppedSamples
//      uint16  highWater      peak ring occupancy, clamped to 0xFFFF
//      uint16  odrCentiHz     output data rate x100
//      uint8   fullScaleG     2 / 4 / 8 / 16
//      uint8   flags          bit0 = ring overflowed this session
// =====================================================================

#include <ArduinoBLE.h>
#include "ICM_20948.h"

// ---------------- OLA V10 pin map ----------------
// From OpenLog_Artemis.ino at HARDWARE_VERSION_MAJOR 1 (the red board).
// The X04 (original black) board differs -- read them out of that file.
const byte PIN_IMU_CHIP_SELECT     = 44;   // same on both board revisions
const byte PIN_IMU_POWER           = 27;   // V10; the X04 uses 22 -- see below
const byte PIN_MICROSD_CHIP_SELECT = 23;   // V10; the X04 uses 10
const byte PIN_MICROSD_POWER       = 15;
const byte PIN_QWIIC_POWER         = 18;
const byte PIN_SPI_CIPO            = 6;
const byte PIN_STAT_LED            = 19;
const byte PIN_PWR_LED             = 29;

// Power-rail polarity, read out of stock OLA firmware's lowerPower.ino
// (imuPowerOn/Off, microSDPowerOn/Off, qwiicPowerOn/Off) for
// HARDWARE_VERSION_MAJOR 1. Note the Qwiic rail is the odd one out: HIGH
// enables it on V10 and disables it on the X04, so this is not guesswork you
// can carry between boards.
#define IMU_POWER_ON_LEVEL       HIGH
#define MICROSD_POWER_OFF_LEVEL  HIGH   // LOW powers the card
#define QWIIC_POWER_OFF_LEVEL    LOW    // V10: HIGH powers the Qwiic bus

// The microSD shares the SPI bus with the IMU. Leaving its rail off is the
// point of this build, but if the IMU will not init, set this to 0 to power
// the card like stock firmware does -- that rules out an unpowered card
// loading the bus. Costs current; only useful as a diagnostic.
#define MICROSD_POWER_OFF        1

// ---------------- Configuration ----------------
// ODR = 1125 / (1 + ACCEL_SMPLRT_DIV).  3 -> 281.25 Hz.  See guide section 2.
#define ACCEL_SMPLRT_DIV    3
#define ODR_CENTI_HZ        28125       // 281.25 Hz x100, reported to host
#define ACCEL_FS_G          4           // gpm4 -> 8192 LSB/g

#define SAMPLES_PER_PACKET  3           // 2 + 3*6 = 20 bytes exactly
#define PACKET_BYTES        (2 + SAMPLES_PER_PACKET * 6)
#define RING_SAMPLES        8192        // 8192 * 6 B = 48 KB ~= 29 s
#define STATUS_INTERVAL_MS  1000

// Poll the BLE stack explicitly each pass. central.connected() polls
// internally, but an extra poll costs almost nothing and removes a whole
// class of stalls where the link drops a second or two after connecting.
// See the section 5 warning in the guide.
#define EXPLICIT_BLE_POLL   1

// Print diagnostics over USB serial at 115200. On by default while the build
// is being brought up on hardware: it costs a little loop time but turns a
// blink code into an actual reason. Set to 0 once streaming is working.
#define DEBUG_SERIAL        1

// ---------------- BLE UUIDs (custom family) ----------------
#define UUID_SERVICE "f1b7a2c0-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_DATA    "f1b7a2c1-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_STATUS  "f1b7a2c2-9e4d-4a1f-8c3b-5d6e7f801234"

#define DEVICE_NAME  "OLA-ACCEL"

BLEService accelService(UUID_SERVICE);

// BLERead alongside BLENotify, matching the working example in
// OpenLog_Artemis issue #66 (BLERead | BLEWrite | BLENotify). A notify-only
// characteristic is legal, but a readable one is what is known to work on
// this core, and it lets a central read the last value without subscribing.
BLECharacteristic dataChar  (UUID_DATA,   BLERead | BLENotify, PACKET_BYTES);
BLECharacteristic statusChar(UUID_STATUS, BLERead | BLENotify, 14);

ICM_20948_SPI myICM;

// ---------------- Ring buffer ----------------
struct Sample { int16_t x, y, z; };
static Sample   ring[RING_SAMPLES];
static uint32_t head = 0, tail = 0;

static uint32_t totalSamples   = 0;
static uint32_t droppedSamples = 0;
static uint32_t highWater      = 0;
static uint16_t seq            = 0;
static uint8_t  flags          = 0;      // bit0 = ring overflowed this session

// Fault codes blinked on PIN_STAT_LED, forever.
#define FAULT_IMU_NOT_FOUND   2
#define FAULT_IMU_CONFIG      3
#define FAULT_BLE_STACK       4

// The .ino preprocessor generates prototypes for us, but being explicit
// keeps this file valid C++ if you ever rename it to .cpp.
ICM_20948_Status_e configureIMU();
void pumpIMU();
void pumpBLE();
void sendStatus();
void fatalBlink(int code, const char *why);
void configureOutput(byte pin);
void enableCIPOpullUp();
void primeSpiPullUp();
void imuPowerOn();
void imuPowerOff();
bool beginIMU();

// ---------------------------------------------------------------------
// On Apollo3, pinMode() alone does not reliably re-configure a pad that was
// previously set up for another function -- stock OLA firmware pairs every
// pinMode with this call, commented "Make sure the pin does actually get
// re-configured". Omitting it is why a power rail can look driven in code
// and read dead on the board.
void configureOutput(byte pin) {
  pinMode(pin, OUTPUT);
  pin_config(PinName(pin), g_AM_HAL_GPIO_OUTPUT);
}

// A 1.5K pull-up on the shared SPI CIPO line. Without it the line floats
// between transfers and the IMU's WHO_AM_I read comes back as garbage, which
// presents exactly as "IMU not found". From OpenLog_Artemis.ino's
// enableCIPOpullUp(), updated there for Apollo3 core 2.1.0.
void enableCIPOpullUp() {
  am_hal_gpio_pincfg_t cipoPinCfg = g_AM_BSP_GPIO_IOM0_MISO;
  cipoPinCfg.ePullup = AM_HAL_GPIO_PIN_PULLUP_1_5K;
  pin_config(PinName(PIN_SPI_CIPO), cipoPinCfg);
}

// ...and the part that is impossible to guess: in the Apollo3 mbed core the
// FIRST SPI transaction after the pull-up is configured silently switches it
// back off. So enabling it once at startup achieves nothing -- myICM.begin()
// issues the first transaction, loses the pull-up, and reads a floating CIPO.
// The fix is a throwaway transaction followed by re-enabling the pull-up.
//
// Straight out of Firmware/Test Sketches/OLA_IMU_Basics in the OLA repo; see
// also github.com/sparkfun/OpenLog_Artemis/issues/66. Call this before every
// begin() attempt, not just once: each attempt spends transactions of its own.
void primeSpiPullUp() {
  enableCIPOpullUp();
#if defined(ARDUINO_ARCH_MBED)
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  SPI.endTransaction();
  enableCIPOpullUp();
#endif
}

// Only two pins differ between the OpenLog Artemis V10 (red) and X04 (black):
// the IMU power rail and the microSD chip select. Everything this sketch
// touches -- the IMU chip select, the LEDs, the SPI bus, the Qwiic and
// microSD power pins -- is identical on both. So rather than make you know
// which board you have, beginIMU() tries both rails and keeps the one that
// answers. The cost of trying the wrong one is driving an unassigned pad.
const byte IMU_POWER_PIN_CANDIDATES[] = {27, 22};   // V10 first, then X04
const byte MICROSD_CS_CANDIDATES[]    = {23, 10};

byte     activeImuPowerPin = 27;   // whichever rail actually worked
uint32_t activeSpiHz       = 4000000;

void imuPowerOn() {
  configureOutput(activeImuPowerPin);
  digitalWrite(activeImuPowerPin, IMU_POWER_ON_LEVEL);
}

void imuPowerOff() {
  configureOutput(activeImuPowerPin);
  digitalWrite(activeImuPowerPin, !IMU_POWER_ON_LEVEL);
}

static inline uint32_t ringCount() {
  return (head >= tail) ? (head - tail) : (RING_SAMPLES - tail + head);
}

// =====================================================================
void setup() {
#if DEBUG_SERIAL
  Serial.begin(115200);
  delay(1000);
  Serial.println("OLA-ACCEL starting");
#endif

  configureOutput(PIN_PWR_LED);  digitalWrite(PIN_PWR_LED,  LOW);
  configureOutput(PIN_STAT_LED); digitalWrite(PIN_STAT_LED, LOW);

  // ---- BLE first ----
  // Order matters, and this is the order the known-working sketch in
  // OpenLog_Artemis issue #66 uses: the whole BLE block runs before SPI and
  // the IMU are touched. BLE.begin() brings up the Cordio stack and starts
  // its RTOS thread; starting it after a second of 4 MHz SPI traffic and
  // IMU power cycling is the one structural difference that remained
  // between this sketch and the example that works on this hardware.
  if (!BLE.begin())
    fatalBlink(FAULT_BLE_STACK, "BLE.begin() failed -- check ArduinoBLE is 1.1.3");

  BLE.setLocalName(DEVICE_NAME);        // the example sets only the local
                                        // name, not setDeviceName()
  BLE.setAdvertisedService(accelService);
  accelService.addCharacteristic(dataChar);
  accelService.addCharacteristic(statusChar);
  BLE.addService(accelService);
  BLE.advertise();

#if DEBUG_SERIAL
  Serial.println("advertising as " DEVICE_NAME);
#endif

  // Keep the Qwiic rail off -- we use no Qwiic devices.
  configureOutput(PIN_QWIIC_POWER);
  digitalWrite(PIN_QWIIC_POWER, QWIIC_POWER_OFF_LEVEL);

  configureOutput(PIN_MICROSD_POWER);
  digitalWrite(PIN_MICROSD_POWER,
               MICROSD_POWER_OFF ? MICROSD_POWER_OFF_LEVEL : !MICROSD_POWER_OFF_LEVEL);

  // Every SPI device deselected before anything drives the bus. Both microSD
  // chip-select candidates, since we do not yet know the board revision.
  for (byte i = 0; i < sizeof(MICROSD_CS_CANDIDATES); i++) {
    configureOutput(MICROSD_CS_CANDIDATES[i]);
    digitalWrite(MICROSD_CS_CANDIDATES[i], HIGH);
  }
  configureOutput(PIN_IMU_CHIP_SELECT);
  digitalWrite(PIN_IMU_CHIP_SELECT, HIGH);

  SPI.begin();
  delay(2);                         // stock notes SPI needs a moment here
  primeSpiPullUp();                 // must come after SPI.begin()

  if (!beginIMU()) fatalBlink(FAULT_IMU_NOT_FOUND,
      "ICM-20948 did not respond on either board revision's power rail");

  if (configureIMU() != ICM_20948_Stat_Ok)
    fatalBlink(FAULT_IMU_CONFIG, "IMU answered but rejected its configuration");

#if DEBUG_SERIAL
  Serial.println("IMU configured -- ready");
#endif
}

// ---------------------------------------------------------------------
// Bring the ICM-20948 up, power-cycling it on each attempt.
//
// The power cycle is not optional: stock firmware resets the ICM this way
// every time, because a warm restart -- a reflash, or a watchdog reset --
// can leave the part in a state where begin() fails. The datasheet gives
// 11 ms typical and 100 ms maximum for start-up, so the first attempt waits
// 25 ms and later ones wait the full 100 ms.
bool beginIMU() {
  const uint32_t clocks[] = {4000000, 1000000};   // 4 MHz is stock; 1 MHz is
                                                  // the fallback for a
                                                  // marginal bus
  for (byte p = 0; p < sizeof(IMU_POWER_PIN_CANDIDATES); p++) {
    activeImuPowerPin = IMU_POWER_PIN_CANDIDATES[p];

    for (byte c = 0; c < 2; c++) {
      for (int attempt = 0; attempt < 2; attempt++) {
        imuPowerOff();
        delay(10);
        imuPowerOn();
        delay(attempt == 0 ? 25 : 100);

        digitalWrite(PIN_IMU_CHIP_SELECT, HIGH);   // be sure it is deselected
        primeSpiPullUp();           // the previous attempt consumed it

        activeSpiHz = clocks[c];
        myICM.begin(PIN_IMU_CHIP_SELECT, SPI, activeSpiHz);
        if (myICM.status == ICM_20948_Stat_Ok) {
          delay(25);                // stock waits again before configuring
#if DEBUG_SERIAL
          Serial.print("IMU found: power pin "); Serial.print(activeImuPowerPin);
          Serial.print(" ("); Serial.print(activeImuPowerPin == 27 ? "V10" : "X04");
          Serial.print("), SPI "); Serial.print(activeSpiHz / 1000000);
          Serial.println(" MHz");
#endif
          return true;
        }

#if DEBUG_SERIAL
        Serial.print("beginIMU: power pin "); Serial.print(activeImuPowerPin);
        Serial.print(", "); Serial.print(activeSpiHz / 1000000);
        Serial.print(" MHz, attempt "); Serial.print(attempt);
        Serial.print(" -> status "); Serial.println(myICM.status);
#endif
      }
    }

    imuPowerOff();                  // leave this candidate rail off
  }
  return false;
}

// ---------------------------------------------------------------------
ICM_20948_Status_e configureIMU() {
  myICM.swReset();   delay(250);
  myICM.sleep(false);
  myICM.lowPower(false);

  // Continuous (not duty-cycled) sampling of the accelerometer only.
  myICM.setSampleMode(ICM_20948_Internal_Acc, ICM_20948_Sample_Mode_Continuous);

  ICM_20948_fss_t fss;
  fss.a = gpm4;                       // +/-4 g -> 8192 LSB/g
  myICM.setFullScale(ICM_20948_Internal_Acc, fss);

  // Anti-alias filter. 136 Hz noise BW < 140.6 Hz Nyquist at 281.25 Hz.
  ICM_20948_dlpcfg_t dlp;
  dlp.a = acc_d111bw4_n136bw;
  myICM.setDLPFcfg(ICM_20948_Internal_Acc, dlp);
  myICM.enableDLPF(ICM_20948_Internal_Acc, true);

  ICM_20948_smplrt_t rate;
  rate.a = ACCEL_SMPLRT_DIV;          // ODR = 1125/(1+3) = 281.25 Hz
  myICM.setSampleRate(ICM_20948_Internal_Acc, rate);

  return myICM.status;
}

// ---------------------------------------------------------------------
// NEVER let this loop spin without yielding.
//
// ArduinoBLE's Apollo3 port does not run the Bluetooth stack in your loop --
// HCICordioTransport.cpp starts an mbed RTOS thread (bleLoopThread) that owns
// the Cordio stack. On this core delay() is rtos::ThisThread::sleep_for(),
// which yields the CPU; a bare busy-loop never does. Starve that thread and
// the link layer still connects -- interrupts keep running -- but GATT stops:
// service discovery times out, and the CCCD write that makes subscribed()
// true never gets processed. The board looks connected and streams nothing.
//
// Every delay() below is load-bearing. Removing them reproduces a bug that
// presents as a host-side timeout, which is the wrong place to look.
void loop() {
  BLEDevice central = BLE.central();

  if (!central) {
    // Slow heartbeat so an idle board is visibly alive and clearly not
    // stuck in fatalBlink().
    uint32_t phase = millis() % 2000;
    digitalWrite(PIN_STAT_LED, phase < 40 ? HIGH : LOW);
    return;
  }

  // A connection starts a fresh session: counters and buffer reset, so the
  // host always sees seq starting near zero with a known sample origin.
  head = tail = 0;
  totalSamples = droppedSamples = highWater = 0;
  seq = 0; flags = 0;
  myICM.getAGMT();                    // flush any stale reading
  digitalWrite(PIN_STAT_LED, HIGH);

#if DEBUG_SERIAL
  Serial.print("connected to "); Serial.println(central.address());
#endif

  uint32_t lastStatus = millis();
  bool streaming = false;

  while (central.connected()) {
#if EXPLICIT_BLE_POLL
    BLE.poll();
#endif

    // Do not read the IMU or transmit anything until the central has actually
    // subscribed. A central spends the first seconds after connecting on
    // service discovery -- a burst of ATT requests it expects answered
    // promptly. Streaming into a characteristic nobody has subscribed to
    // during that window accomplishes nothing and can starve discovery badly
    // enough that the central gives up and reports a connection timeout.
    if (!streaming) {
      if (!dataChar.subscribed()) {
        delay(1);                     // MUST yield -- see the note above loop()
        continue;
      }

      streaming = true;
      head = tail = 0;
      totalSamples = droppedSamples = highWater = 0;
      seq = 0; flags = 0;
      myICM.getAGMT();                // discard anything sampled while waiting
      lastStatus = millis();
#if DEBUG_SERIAL
      Serial.println("subscribed -- streaming");
#endif
    }

    pumpIMU();
    pumpBLE();
    if (millis() - lastStatus >= STATUS_INTERVAL_MS) {
      sendStatus();
      lastStatus = millis();
    }

    // Yield every pass. At 281.25 Hz a 1 ms cadence is ~3.5x faster than
    // samples arrive, and pumpBLE() drains as many packets as the stack will
    // take per pass, so this costs no throughput.
    delay(1);
  }

  digitalWrite(PIN_STAT_LED, LOW);

#if DEBUG_SERIAL
  Serial.println("disconnected");
#endif

  // Harmless if the stack already restarted advertising on its own; makes
  // sure a second session is always possible without a power cycle.
  BLE.advertise();
}

// ---------------------------------------------------------------------
// Drain every sample the IMU has ready into the ring buffer.
void pumpIMU() {
  while (myICM.dataReady()) {
    myICM.getAGMT();

    uint32_t next = (head + 1) % RING_SAMPLES;
    if (next == tail) {              // ring full -- this is the ONLY loss path
      droppedSamples++;
      flags |= 0x01;
      return;                        // stop reading; let pumpBLE catch up
    }
    ring[head].x = myICM.agmt.acc.axes.x;
    ring[head].y = myICM.agmt.acc.axes.y;
    ring[head].z = myICM.agmt.acc.axes.z;
    head = next;
    totalSamples++;

    uint32_t n = ringCount();
    if (n > highWater) highWater = n;
  }
}

// ---------------------------------------------------------------------
// Emit as many 20-byte packets as the BLE stack will accept right now.
void pumpBLE() {
  uint8_t pkt[PACKET_BYTES];

  while (ringCount() >= SAMPLES_PER_PACKET) {
    pkt[0] = (uint8_t)(seq & 0xFF);
    pkt[1] = (uint8_t)(seq >> 8);

    uint32_t t = tail;
    for (int i = 0; i < SAMPLES_PER_PACKET; i++) {
      int o = 2 + i * 6;
      pkt[o + 0] = (uint8_t)(ring[t].x & 0xFF);
      pkt[o + 1] = (uint8_t)(ring[t].x >> 8);
      pkt[o + 2] = (uint8_t)(ring[t].y & 0xFF);
      pkt[o + 3] = (uint8_t)(ring[t].y >> 8);
      pkt[o + 4] = (uint8_t)(ring[t].z & 0xFF);
      pkt[o + 5] = (uint8_t)(ring[t].z >> 8);
      t = (t + 1) % RING_SAMPLES;
    }

    // Backpressure: a false return means the TX queue is full. Leave the
    // samples in the ring and try again next pass -- do NOT advance tail.
    // Deleting this early return turns a lossless streamer into a lossy one
    // that looks identical until you run test 4.
    if (!dataChar.writeValue(pkt, PACKET_BYTES)) return;

    tail = t;
    seq++;
  }
}

// ---------------------------------------------------------------------
void sendStatus() {
  uint8_t s[14];
  memcpy(&s[0],  &totalSamples,   4);
  memcpy(&s[4],  &droppedSamples, 4);
  uint16_t hw  = (uint16_t)(highWater > 0xFFFF ? 0xFFFF : highWater);
  memcpy(&s[8],  &hw, 2);
  uint16_t odr = ODR_CENTI_HZ;
  memcpy(&s[10], &odr, 2);
  s[12] = ACCEL_FS_G;
  s[13] = flags;
  statusChar.writeValue(s, 14);

#if DEBUG_SERIAL
  Serial.print("sampled=");  Serial.print(totalSamples);
  Serial.print(" dropped="); Serial.print(droppedSamples);
  Serial.print(" peak=");    Serial.print(highWater);
  Serial.print(" seq=");     Serial.println(seq);
#endif
}

// ---------------------------------------------------------------------
// Blink a fault code forever. Count the blinks between long pauses.
//   2 = IMU not found, 3 = IMU config failed, 4 = BLE stack failed.
//
// The reason is re-printed on every cycle rather than once at boot, so a
// serial monitor opened at any point shows why -- no need to catch the first
// second after reset.
void fatalBlink(int code, const char *why) {
  while (true) {
#if DEBUG_SERIAL
    Serial.print("FAULT "); Serial.print(code);
    Serial.print(": "); Serial.println(why);
#else
    (void)why;
#endif
    for (int i = 0; i < code; i++) {
      digitalWrite(PIN_STAT_LED, HIGH); delay(150);
      digitalWrite(PIN_STAT_LED, LOW);  delay(150);
    }
    delay(1200);
  }
}
