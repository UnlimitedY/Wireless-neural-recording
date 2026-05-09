#include <Wire.h>

namespace {

const uint8_t kMpr121Address = 0x5A;
const uint8_t kDefaultElectrodesUsed = 2;
const uint8_t kMaxElectrodes = 12;
const uint32_t kDefaultStreamIntervalMs = 100;

const uint8_t REG_TOUCH_STATUS_L = 0x00;
const uint8_t REG_FILTERED_0L = 0x04;
const uint8_t REG_BASELINE_0 = 0x1E;
const uint8_t REG_MHDR = 0x2B;
const uint8_t REG_NHDR = 0x2C;
const uint8_t REG_NCLR = 0x2D;
const uint8_t REG_FDLR = 0x2E;
const uint8_t REG_MHDF = 0x2F;
const uint8_t REG_NHDF = 0x30;
const uint8_t REG_NCLF = 0x31;
const uint8_t REG_FDLF = 0x32;
const uint8_t REG_NHDT = 0x33;
const uint8_t REG_NCLT = 0x34;
const uint8_t REG_FDLT = 0x35;
const uint8_t REG_TOUCH_THRESHOLD_0 = 0x41;
const uint8_t REG_RELEASE_THRESHOLD_0 = 0x42;
const uint8_t REG_DEBOUNCE = 0x5B;
const uint8_t REG_CONFIG1 = 0x5C;
const uint8_t REG_CONFIG2 = 0x5D;
const uint8_t REG_ECR = 0x5E;
const uint8_t REG_AUTOCONFIG0 = 0x7B;
const uint8_t REG_UPLIMIT = 0x7D;
const uint8_t REG_TARGETLIMIT = 0x7E;
const uint8_t REG_LOWLIMIT = 0x7F;
const uint8_t REG_SOFTRESET = 0x80;

enum RunMode : uint8_t {
  MODE_STOP = 1,
  MODE_RUN_LOCK = 2,
  MODE_RUN_UPDATE = 3,
};

struct Profile {
  const char *name;
  uint8_t touchThreshold;
  uint8_t releaseThreshold;
  uint8_t debounceTouch;
  uint8_t debounceRelease;
  uint8_t mhdR;
  uint8_t nhdR;
  uint8_t nclR;
  uint8_t fdlR;
  uint8_t mhdF;
  uint8_t nhdF;
  uint8_t nclF;
  uint8_t fdlF;
  uint8_t nhdT;
  uint8_t nclT;
  uint8_t fdlT;
  uint8_t config1;
  uint8_t config2;
};

const Profile kDefaultProfile = {
    "default",
    12,
    6,
    0,
    0,
    0x01,
    0x01,
    0x0E,
    0x00,
    0x01,
    0x05,
    0x01,
    0x00,
    0x00,
    0x00,
    0x00,
    0x10,
    0x20,
};

const Profile kAntiEmiProfile = {
    "anti_emi",
    24,
    12,
    2,
    2,
    0x01,
    0x01,
    0x0E,
    0x00,
    0x01,
    0x05,
    0x01,
    0x00,
    0x00,
    0x00,
    0x00,
    0x10,
    0x20,
};

const Profile kHighSensitivityProfile = {
    "high_sensitivity",
    6,
    3,
    0,
    0,
    0x01,
    0x01,
    0x0E,
    0x00,
    0x01,
    0x05,
    0x01,
    0x00,
    0x00,
    0x00,
    0x00,
    0x10,
    0x20,
};

RunMode g_mode = MODE_RUN_UPDATE;
Profile g_profile = kDefaultProfile;
uint8_t g_electrodesUsed = kDefaultElectrodesUsed;
uint8_t g_electrodesToPrint = kDefaultElectrodesUsed;
uint32_t g_streamIntervalMs = kDefaultStreamIntervalMs;
uint32_t g_lastStreamMs = 0;
char g_packetBuffer[96];

uint8_t readRegister8(uint8_t reg) {
  Wire.beginTransmission(kMpr121Address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return 0;
  }
  uint8_t count = Wire.requestFrom((int)kMpr121Address, 1);
  if (count < 1) {
    return 0;
  }
  return Wire.read();
}

uint16_t readRegister16(uint8_t reg) {
  Wire.beginTransmission(kMpr121Address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return 0;
  }
  uint8_t count = Wire.requestFrom((int)kMpr121Address, 2);
  if (count < 2) {
    return 0;
  }
  uint8_t lsb = Wire.read();
  uint8_t msb = Wire.read();
  return ((uint16_t)msb << 8) | lsb;
}

bool writeRegister8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kMpr121Address);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

uint16_t baselineData(uint8_t electrode) {
  if (electrode >= kMaxElectrodes) {
    return 0;
  }
  return ((uint16_t)readRegister8(REG_BASELINE_0 + electrode)) << 2;
}

uint16_t filteredData(uint8_t electrode) {
  if (electrode >= kMaxElectrodes) {
    return 0;
  }
  return readRegister16(REG_FILTERED_0L + electrode * 2);
}

uint16_t touchStatus() { return readRegister16(REG_TOUCH_STATUS_L); }

const char *modeName(RunMode mode) {
  switch (mode) {
  case MODE_STOP:
    return "STOP";
  case MODE_RUN_LOCK:
    return "RUN_LOCK";
  case MODE_RUN_UPDATE:
    return "RUN_UPDATE";
  default:
    return "UNKNOWN";
  }
}

uint8_t ecrValueForMode(RunMode mode) {
  if (mode == MODE_STOP) {
    return 0x00;
  }
  if (mode == MODE_RUN_LOCK) {
    return (uint8_t)(0x40 | g_electrodesUsed);
  }
  return g_electrodesUsed;
}

bool applyMode(RunMode mode) {
  if (!writeRegister8(REG_ECR, ecrValueForMode(mode))) {
    return false;
  }
  g_mode = mode;
  return true;
}

bool applyThresholds(uint8_t touchThreshold, uint8_t releaseThreshold) {
  for (uint8_t electrode = 0; electrode < g_electrodesUsed; ++electrode) {
    if (!writeRegister8(REG_TOUCH_THRESHOLD_0 + electrode * 2, touchThreshold)) {
      return false;
    }
    if (!writeRegister8(REG_RELEASE_THRESHOLD_0 + electrode * 2,
                        releaseThreshold)) {
      return false;
    }
  }
  g_profile.touchThreshold = touchThreshold;
  g_profile.releaseThreshold = releaseThreshold;
  return true;
}

bool applyDebounce(uint8_t touchDebounce, uint8_t releaseDebounce) {
  touchDebounce = constrain(touchDebounce, 0, 7);
  releaseDebounce = constrain(releaseDebounce, 0, 7);
  uint8_t debounceValue = (uint8_t)((releaseDebounce << 4) | touchDebounce);
  if (!writeRegister8(REG_DEBOUNCE, debounceValue)) {
    return false;
  }
  g_profile.debounceTouch = touchDebounce;
  g_profile.debounceRelease = releaseDebounce;
  return true;
}

void printHex8(uint8_t value) {
  if (value < 0x10) {
    Serial.print("0");
  }
  Serial.print(value, HEX);
}

void printHex16(uint16_t value) {
  if (value < 0x1000) {
    Serial.print("0");
  }
  if (value < 0x0100) {
    Serial.print("0");
  }
  if (value < 0x0010) {
    Serial.print("0");
  }
  Serial.print(value, HEX);
}

bool applyProfile(const Profile &profile, RunMode targetMode) {
  if (!writeRegister8(REG_ECR, 0x00)) {
    return false;
  }

  if (!writeRegister8(REG_SOFTRESET, 0x63)) {
    return false;
  }
  delay(5);

  if (!writeRegister8(REG_ECR, 0x00)) {
    return false;
  }
  if (!writeRegister8(REG_MHDR, profile.mhdR) ||
      !writeRegister8(REG_NHDR, profile.nhdR) ||
      !writeRegister8(REG_NCLR, profile.nclR) ||
      !writeRegister8(REG_FDLR, profile.fdlR) ||
      !writeRegister8(REG_MHDF, profile.mhdF) ||
      !writeRegister8(REG_NHDF, profile.nhdF) ||
      !writeRegister8(REG_NCLF, profile.nclF) ||
      !writeRegister8(REG_FDLF, profile.fdlF) ||
      !writeRegister8(REG_NHDT, profile.nhdT) ||
      !writeRegister8(REG_NCLT, profile.nclT) ||
      !writeRegister8(REG_FDLT, profile.fdlT) ||
      !writeRegister8(REG_CONFIG1, profile.config1) ||
      !writeRegister8(REG_CONFIG2, profile.config2) ||
      !writeRegister8(REG_AUTOCONFIG0, 0x00)) {
    return false;
  }

  // These limits are only relevant if autoconfig is enabled, but keeping
  // them populated makes register dumps easier to interpret.
  if (!writeRegister8(REG_UPLIMIT, 200) || !writeRegister8(REG_TARGETLIMIT, 180) ||
      !writeRegister8(REG_LOWLIMIT, 130)) {
    return false;
  }

  if (!applyThresholds(profile.touchThreshold, profile.releaseThreshold)) {
    return false;
  }
  if (!applyDebounce(profile.debounceTouch, profile.debounceRelease)) {
    return false;
  }

  g_profile = profile;
  return applyMode(targetMode);
}

bool reinitializeCurrentProfile(RunMode targetMode) {
  Profile profile = g_profile;
  return applyProfile(profile, targetMode);
}

void printHelp() {
  Serial.println();
  Serial.println("===== MPR121 baseline/filtered debug =====");
  Serial.println("Core modes:");
  Serial.println("  1  -> stop mode");
  Serial.println("  2  -> running mode, baseline locked");
  Serial.println("  3  -> running mode, baseline updating");
  Serial.println("Profiles for wireless-charge interference tests:");
  Serial.println("  4  -> default profile");
  Serial.println("  5  -> anti-EMI profile (higher threshold + debounce)");
  Serial.println("  6  -> high-sensitivity profile (lower threshold)");
  Serial.println("Utility commands:");
  Serial.println("  7  -> relearn baseline now (running-update)");
  Serial.println("  8  -> dump key registers");
  Serial.println("  9  -> toggle print 2 electrodes / 12 electrodes");
  Serial.println("  P  -> print help");
  Serial.println("  S  -> print one data frame immediately");
  Serial.println("  I,<ms>                -> set stream interval");
  Serial.println("  T,<touch>,<release>   -> set thresholds");
  Serial.println("  D,<touchDb>,<relDb>   -> set debounce 0..7");
  Serial.println("  B,<ms>                -> relearn for ms, then lock baseline");
  Serial.println("  G,<reg>               -> read 8-bit register (supports 0xNN)");
  Serial.println("  W,<reg>,<val>         -> write 8-bit register (supports 0xNN)");
  Serial.println("CSV stream:");
  Serial.println("  DATA,ms,mode,touchHex,eCount,b0,f0,d0,b1,f1,d1,...");
  Serial.println("==========================================");
  Serial.println();
}

void printAck(const char *message) {
  Serial.print("INFO,");
  Serial.println(message);
}

void printRegisterDump() {
  Serial.print("REG,ECR,0x");
  printHex8(readRegister8(REG_ECR));
  Serial.println();
  Serial.print("REG,DEBOUNCE,0x");
  printHex8(readRegister8(REG_DEBOUNCE));
  Serial.println();
  Serial.print("REG,CONFIG1,0x");
  printHex8(readRegister8(REG_CONFIG1));
  Serial.println();
  Serial.print("REG,CONFIG2,0x");
  printHex8(readRegister8(REG_CONFIG2));
  Serial.println();
  Serial.print("REG,AUTOCONFIG0,0x");
  printHex8(readRegister8(REG_AUTOCONFIG0));
  Serial.println();
  Serial.print("REG,UPLIMIT,");
  Serial.println(readRegister8(REG_UPLIMIT));
  Serial.print("REG,TARGETLIMIT,");
  Serial.println(readRegister8(REG_TARGETLIMIT));
  Serial.print("REG,LOWLIMIT,");
  Serial.println(readRegister8(REG_LOWLIMIT));
  for (uint8_t electrode = 0; electrode < g_electrodesUsed; ++electrode) {
    Serial.print("REG,E");
    Serial.print(electrode);
    Serial.print(",TH,");
    Serial.print(readRegister8(REG_TOUCH_THRESHOLD_0 + electrode * 2));
    Serial.print(",REL,");
    Serial.println(readRegister8(REG_RELEASE_THRESHOLD_0 + electrode * 2));
  }
}

void printStatusLine() {
  uint16_t touched = touchStatus();
  Serial.print("DATA,");
  Serial.print(millis());
  Serial.print(",");
  Serial.print(modeName(g_mode));
  Serial.print(",0x");
  printHex16(touched);
  Serial.print(",");
  Serial.print(g_electrodesToPrint);

  for (uint8_t electrode = 0; electrode < g_electrodesToPrint; ++electrode) {
    uint16_t baseline = baselineData(electrode);
    uint16_t filtered = filteredData(electrode);
    int delta = (int)filtered - (int)baseline;
    Serial.print(",");
    Serial.print(baseline);
    Serial.print(",");
    Serial.print(filtered);
    Serial.print(",");
    Serial.print(delta);
  }
  Serial.println();
}

bool parseOneLong(const char *buf, long &a) {
  const char *p = strchr(buf, ',');
  if (p == nullptr) {
    return false;
  }
  ++p;
  char *endPtr = nullptr;
  a = strtol(p, &endPtr, 0);
  return endPtr != p;
}

bool parseTwoLongs(const char *buf, long &a, long &b) {
  const char *p = strchr(buf, ',');
  if (p == nullptr) {
    return false;
  }
  ++p;
  char *endPtr = nullptr;
  a = strtol(p, &endPtr, 0);
  if (endPtr == p || *endPtr != ',') {
    return false;
  }
  p = endPtr + 1;
  b = strtol(p, &endPtr, 0);
  return endPtr != p;
}

void relearnThenLock(uint32_t durationMs) {
  if (!reinitializeCurrentProfile(MODE_RUN_UPDATE)) {
    printAck("failed to reinitialize profile before relearn");
    return;
  }

  Serial.print("INFO,relearning baseline for ");
  Serial.print(durationMs);
  Serial.println(" ms");

  uint32_t startMs = millis();
  uint32_t lastPrintMs = 0;
  while (millis() - startMs < durationMs) {
    uint32_t nowMs = millis();
    if (nowMs - lastPrintMs >= g_streamIntervalMs) {
      printStatusLine();
      lastPrintMs = nowMs;
    }
  }

  if (applyMode(MODE_RUN_LOCK)) {
    printAck("baseline relearn complete, switched to RUN_LOCK");
  } else {
    printAck("baseline relearn finished, but RUN_LOCK failed");
  }
}

void handleCommand(const char *command) {
  if (command[0] == '\0') {
    return;
  }

  switch (command[0]) {
  case '1':
    if (applyMode(MODE_STOP)) {
      printAck("mode -> STOP");
    } else {
      printAck("failed to enter STOP");
    }
    return;
  case '2':
    if (applyMode(MODE_RUN_LOCK)) {
      printAck("mode -> RUN_LOCK");
    } else {
      printAck("failed to enter RUN_LOCK");
    }
    return;
  case '3':
    if (applyMode(MODE_RUN_UPDATE)) {
      printAck("mode -> RUN_UPDATE");
    } else {
      printAck("failed to enter RUN_UPDATE");
    }
    return;
  case '4':
    if (applyProfile(kDefaultProfile, g_mode)) {
      printAck("profile -> default");
    } else {
      printAck("failed to apply default profile");
    }
    return;
  case '5':
    if (applyProfile(kAntiEmiProfile, g_mode)) {
      printAck("profile -> anti_emi");
    } else {
      printAck("failed to apply anti_emi profile");
    }
    return;
  case '6':
    if (applyProfile(kHighSensitivityProfile, g_mode)) {
      printAck("profile -> high_sensitivity");
    } else {
      printAck("failed to apply high_sensitivity profile");
    }
    return;
  case '7':
    if (reinitializeCurrentProfile(MODE_RUN_UPDATE)) {
      printAck("profile reinitialized, now in RUN_UPDATE");
    } else {
      printAck("failed to start baseline relearn");
    }
    return;
  case '8':
    printRegisterDump();
    printStatusLine();
    return;
  case '9':
    g_electrodesToPrint =
        (g_electrodesToPrint == kDefaultElectrodesUsed) ? kMaxElectrodes
                                                        : kDefaultElectrodesUsed;
    Serial.print("INFO,electrodes_to_print -> ");
    Serial.println(g_electrodesToPrint);
    return;
  case 'P':
  case 'p':
    printHelp();
    return;
  case 'S':
  case 's':
    printStatusLine();
    return;
  case 'I':
  case 'i': {
    long intervalMs = 0;
    if (!parseOneLong(command, intervalMs)) {
      printAck("usage: I,<ms>");
      return;
    }
    g_streamIntervalMs = (uint32_t)constrain(intervalMs, 10L, 5000L);
    Serial.print("INFO,stream_interval_ms -> ");
    Serial.println(g_streamIntervalMs);
    return;
  }
  case 'T':
  case 't': {
    long touchThreshold = 0;
    long releaseThreshold = 0;
    if (!parseTwoLongs(command, touchThreshold, releaseThreshold)) {
      printAck("usage: T,<touch>,<release>");
      return;
    }
    if (applyThresholds((uint8_t)constrain(touchThreshold, 0L, 255L),
                        (uint8_t)constrain(releaseThreshold, 0L, 255L))) {
      Serial.print("INFO,thresholds -> ");
      Serial.print(g_profile.touchThreshold);
      Serial.print(",");
      Serial.println(g_profile.releaseThreshold);
    } else {
      printAck("failed to set thresholds");
    }
    return;
  }
  case 'D':
  case 'd': {
    long touchDebounce = 0;
    long releaseDebounce = 0;
    if (!parseTwoLongs(command, touchDebounce, releaseDebounce)) {
      printAck("usage: D,<touchDb>,<relDb>");
      return;
    }
    if (applyDebounce((uint8_t)touchDebounce, (uint8_t)releaseDebounce)) {
      Serial.print("INFO,debounce -> ");
      Serial.print(g_profile.debounceTouch);
      Serial.print(",");
      Serial.println(g_profile.debounceRelease);
    } else {
      printAck("failed to set debounce");
    }
    return;
  }
  case 'B':
  case 'b': {
    long durationMs = 0;
    if (!parseOneLong(command, durationMs)) {
      printAck("usage: B,<ms>");
      return;
    }
    relearnThenLock((uint32_t)constrain(durationMs, 50L, 60000L));
    return;
  }
  case 'G':
  case 'g': {
    long reg = 0;
    if (!parseOneLong(command, reg)) {
      printAck("usage: G,<reg>");
      return;
    }
    reg = constrain(reg, 0L, 255L);
    Serial.print("REG,0x");
    printHex8((uint8_t)reg);
    Serial.print(",0x");
    uint8_t value = readRegister8((uint8_t)reg);
    printHex8(value);
    Serial.println();
    return;
  }
  case 'W':
  case 'w': {
    long reg = 0;
    long value = 0;
    if (!parseTwoLongs(command, reg, value)) {
      printAck("usage: W,<reg>,<val>");
      return;
    }
    reg = constrain(reg, 0L, 255L);
    value = constrain(value, 0L, 255L);
    if (writeRegister8((uint8_t)reg, (uint8_t)value)) {
      Serial.print("INFO,wrote reg 0x");
      printHex8((uint8_t)reg);
      Serial.print(" = 0x");
      printHex8((uint8_t)value);
      Serial.println();
      if ((uint8_t)reg == REG_ECR) {
        uint8_t ecr = (uint8_t)value;
        uint8_t electrodeBits = ecr & 0x0F;
        if (electrodeBits >= 1 && electrodeBits <= kMaxElectrodes) {
          g_electrodesUsed = electrodeBits;
          if (g_electrodesToPrint == kDefaultElectrodesUsed) {
            g_electrodesToPrint = g_electrodesUsed;
          }
        }
        if (ecr == 0x00) {
          g_mode = MODE_STOP;
        } else if ((ecr & 0x40) != 0) {
          g_mode = MODE_RUN_LOCK;
        } else {
          g_mode = MODE_RUN_UPDATE;
        }
      }
    } else {
      printAck("register write failed");
    }
    return;
  }
  default:
    printAck("unknown command, send P for help");
    return;
  }
}

void setupImpl() {
  delay(1000);
  Serial.begin(115200);
  while (!Serial && millis() < 4000) {
  }

  Wire.begin();
  delay(50);

  printAck("boot");
  if (applyProfile(kDefaultProfile, MODE_RUN_UPDATE)) {
    printAck("default profile applied");
  } else {
    printAck("failed to initialize MPR121, check wiring/address");
  }
  printHelp();
  printRegisterDump();
  printStatusLine();
}

void loopImpl() {
  if (Serial.available() > 0) {
    size_t len =
        Serial.readBytesUntil('\n', g_packetBuffer, sizeof(g_packetBuffer) - 1);
    g_packetBuffer[len] = '\0';
    if (len > 0 && g_packetBuffer[len - 1] == '\r') {
      g_packetBuffer[len - 1] = '\0';
    }
    handleCommand(g_packetBuffer);
  }

  uint32_t nowMs = millis();
  if (nowMs - g_lastStreamMs >= g_streamIntervalMs) {
    printStatusLine();
    g_lastStreamMs = nowMs;
  }
}

} // namespace

void setup() { setupImpl(); }

void loop() { loopImpl(); }
