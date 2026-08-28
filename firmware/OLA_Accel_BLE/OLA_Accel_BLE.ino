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
const byte PIN_IMU_CHIP_SELECT     = 44;
const byte PIN_IMU_POWER           = 27;
const byte PIN_MICROSD_CHIP_SELECT = 23;
const byte PIN_MICROSD_POWER       = 15;
const byte PIN_QWIIC_POWER         = 18;
const byte PIN_STAT_LED            = 19;
const byte PIN_PWR_LED             = 29;

// Power-rail polarity. These match stock OLA firmware usage, but verify
// them on your board: getting IMU power backwards leaves every reading at
// zero, which is what test 6 in the guide catches.
#define IMU_POWER_ON_LEVEL       HIGH
#define MICROSD_POWER_OFF_LEVEL  HIGH
#define QWIIC_POWER_OFF_LEVEL    HIGH

// ---------------- Configuration ----------------
// ODR = 1125 / (1 + ACCEL_SMPLRT_DIV).  3 -> 281.25 Hz.  See guide section 2.
#define ACCEL_SMPLRT_DIV    3
#define ODR_CENTI_HZ        28125       // 281.25 Hz x100, reported to host
#define ACCEL_FS_G          4           // gpm4 -> 8192 LSB/g

#define SAMPLES_PER_PACKET  3           // 2 + 3*6 = 20 bytes exactly
#define PACKET_BYTES        (2 + SAMPLES_PER_PACKET * 6)
#define RING_SAMPLES        8192        // 8192 * 6 B = 48 KB ~= 29 s
#define STATUS_INTERVAL_MS  1000

// Set to 1 if the link stalls or drops a second or two after connecting --
// on some ports central.connected() does not service the stack on its own.
// See the section 5 warning in the guide.
#define EXPLICIT_BLE_POLL   0

// Set to 1 to print a status line per second over USB serial. Off by
// default: serial writes can stall the streaming loop.
#define DEBUG_SERIAL        0

// ---------------- BLE UUIDs (custom family) ----------------
#define UUID_SERVICE "f1b7a2c0-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_DATA    "f1b7a2c1-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_STATUS  "f1b7a2c2-9e4d-4a1f-8c3b-5d6e7f801234"

#define DEVICE_NAME  "OLA-ACCEL"

BLEService        accelService(UUID_SERVICE);
BLECharacteristic dataChar  (UUID_DATA,   BLENotify,            PACKET_BYTES);
BLECharacteristic statusChar(UUID_STATUS, BLERead | BLENotify,  14);

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
void fatalBlink(int code);

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

  pinMode(PIN_PWR_LED,  OUTPUT); digitalWrite(PIN_PWR_LED,  LOW);
  pinMode(PIN_STAT_LED, OUTPUT); digitalWrite(PIN_STAT_LED, LOW);

  // Power the IMU; keep microSD and Qwiic rails off -- we use neither.
  pinMode(PIN_IMU_POWER,     OUTPUT); digitalWrite(PIN_IMU_POWER,     IMU_POWER_ON_LEVEL);
  pinMode(PIN_MICROSD_POWER, OUTPUT); digitalWrite(PIN_MICROSD_POWER, MICROSD_POWER_OFF_LEVEL);
  pinMode(PIN_QWIIC_POWER,   OUTPUT); digitalWrite(PIN_QWIIC_POWER,   QWIIC_POWER_OFF_LEVEL);

  // The microSD shares the SPI bus with the IMU. Its rail is off, so leave
  // its chip select high-impedance rather than driving a high level into an
  // unpowered part.
  pinMode(PIN_MICROSD_CHIP_SELECT, INPUT);

  delay(100);                       // let the IMU rail settle before SPI

  SPI.begin();

  bool ok = false;
  for (int attempt = 0; attempt < 5 && !ok; attempt++) {
    myICM.begin(PIN_IMU_CHIP_SELECT, SPI, 4000000);
    ok = (myICM.status == ICM_20948_Stat_Ok);
    if (!ok) delay(200);
  }
  if (!ok) fatalBlink(FAULT_IMU_NOT_FOUND);

  if (configureIMU() != ICM_20948_Stat_Ok) fatalBlink(FAULT_IMU_CONFIG);

  if (!BLE.begin()) fatalBlink(FAULT_BLE_STACK);

  BLE.setLocalName(DEVICE_NAME);
  BLE.setDeviceName(DEVICE_NAME);
  BLE.setAdvertisedService(accelService);
  accelService.addCharacteristic(dataChar);
  accelService.addCharacteristic(statusChar);
  BLE.addService(accelService);
  BLE.advertise();

#if DEBUG_SERIAL
  Serial.println("advertising as " DEVICE_NAME);
#endif
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

  while (central.connected()) {
#if EXPLICIT_BLE_POLL
    BLE.poll();
#endif
    pumpIMU();
    pumpBLE();
    if (millis() - lastStatus >= STATUS_INTERVAL_MS) {
      sendStatus();
      lastStatus = millis();
    }
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
void fatalBlink(int code) {
  while (true) {
    for (int i = 0; i < code; i++) {
      digitalWrite(PIN_STAT_LED, HIGH); delay(150);
      digitalWrite(PIN_STAT_LED, LOW);  delay(150);
    }
    delay(1200);
  }
}
