#include <Keyboard.h>
#include <SD.h>
#include <SPI.h> // SD card
#include <math.h>
#include <string.h>

#include "gpSMART_Habits.h" // For State Machine

/* Connection: Teensy <<===Serial===>> PC */

/****************************************************************************************************/
/********************************************** Public
 * *********************************************/
/****************************************************************************************************/

/********** SD card **********/
const byte chipSelect =
    BUILTIN_SDCARD; // Teensy 3.5 & 3.6 & 4.1 on-board SD card
String string_tmp;

/********** Port Definition **********/
const byte switchPin = 4; // ToggerSwitch pin to start/pause experiment
const byte ledPin = 13;   // LED pin

// Events: record all the events happened during one loop
typedef struct {
  int events_num = 0;
  unsigned long events_time[20] = {};
  byte events_id[20] = {}; /* 1: restart; 2: free reward; */
  int events_value[20] = {0};
} Events;
Events Ev;
extern char outBuffer[2048];

/********** Other public **********/
static void FLASHLED(uint16_t duration_ms) {
  digitalWrite(ledPin, HIGH);
  delay(duration_ms);
  digitalWrite(ledPin, LOW);
}

static bool parseThreeInts(const char *buf, int &a, int &b, int &c) {
  const char *p = buf + 1;
  while (*p == ',' || *p == ' ') {
    p++;
  }
  return sscanf(p, "%d,%d,%d", &a, &b, &c) == 3;
}

static bool parseTwoInts(const char *buf, int &a, int &b) {
  const char *p = buf + 1;
  while (*p == ',' || *p == ' ') {
    p++;
  }
  return sscanf(p, "%d,%d", &a, &b) == 2;
}

static bool parseOneInt(const char *buf, int &a) {
  const char *p = buf + 1;
  while (*p == ',' || *p == ' ') {
    p++;
  }
  return sscanf(p, "%d", &a) == 1;
}

extern gpSMART smart;
byte CapLicksEnabledState = 0;
const uint8_t CapI2CAddress = 0x5A;
const uint8_t CapElectrodesUsed = 2;
const uint8_t CapRegFiltered0L = 0x04;
const uint8_t CapRegBaseline0 = 0x1E;
const uint8_t CapRegECR = 0x5E;
const uint8_t CapNormalRunMode = 0x00;
const uint8_t CapLockedRunMode = 0x40;

static bool capReadRegister8(uint8_t reg, uint8_t *value) {
  Wire.beginTransmission(CapI2CAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  uint8_t nRead = Wire.requestFrom((int)CapI2CAddress, 1);
  if (nRead < 1) {
    return false;
  }
  *value = Wire.read();
  return true;
}

static uint16_t capReadRegister16(uint8_t reg) {
  Wire.beginTransmission(CapI2CAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return 0;
  }
  uint8_t nRead = Wire.requestFrom((int)CapI2CAddress, 2);
  if (nRead < 2) {
    return 0;
  }
  uint8_t lsb = Wire.read();
  uint8_t msb = Wire.read();
  return ((uint16_t)msb << 8) | lsb;
}

static bool capWriteRegister8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(CapI2CAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool capSetNormalRunMode() {
  return capWriteRegister8(CapRegECR, CapNormalRunMode | CapElectrodesUsed);
}

static bool capSetBaselineLockedRunMode() {
  return capWriteRegister8(CapRegECR, CapLockedRunMode | CapElectrodesUsed);
}

static bool capSetStopMode() { return capWriteRegister8(CapRegECR, 0x00); }

static uint16_t LastValidCapBaseline[CapElectrodesUsed] = {0};
static byte HasLastValidCapBaseline[CapElectrodesUsed] = {0};

static uint16_t capBaselineData(uint8_t electrode, byte &fallbackUsed) {
  fallbackUsed = 0;
  if (electrode >= CapElectrodesUsed) {
    return 0;
  }
  uint8_t baselineRaw = 0;
  // baseline=0 物理上不可能，读到0说明I2C采样错误，最多重试3次
  for (int attempt = 0; attempt < 3; attempt++) {
    bool readOk = capReadRegister8(CapRegBaseline0 + electrode, &baselineRaw);
    if (readOk && baselineRaw != 0) {
      uint16_t baseline = ((uint16_t)baselineRaw) << 2;
      LastValidCapBaseline[electrode] = baseline;
      HasLastValidCapBaseline[electrode] = 1;
      return baseline;
    }
  }
  // 3次都失败或读到0，使用上次的有效值
  fallbackUsed = 1;
  if (HasLastValidCapBaseline[electrode] == 1) {
    return LastValidCapBaseline[electrode];
  }
  return 0;
}

static uint16_t capFilteredData(uint8_t electrode) {
  if (electrode >= CapElectrodesUsed) {
    return 0;
  }
  return capReadRegister16(CapRegFiltered0L + electrode * 2);
}

// 利用这个做自动的重新cap的初始化，当人工发现delta值太大的时候
static bool capReinitSafely(uint8_t numElectrode) {
  smart.reinitCap(numElectrode);
  delay(5000);
  smart.setLicksDetectionEnabled(0);
  capSetLicksEnabledState(0, 0);
  capSendTelemetry();
  // capSetStopMode(); // 为了保证baseline 拟合到一个正确的值，这里不能被stop掉
  return true;
}

// send the current detect state to the host
static void capSendDetectState() {
  char capBuffer[24];
  sprintf(capBuffer, "CD,%d", CapLicksEnabledState);
  Serial.println(capBuffer);
}
static void capSetLicksEnabledState(byte enabled, byte forceReport) {
  byte nextState = enabled ? 1 : 0;
  if (CapLicksEnabledState != nextState || forceReport) {
    CapLicksEnabledState = nextState;
    capSendDetectState();
  } else {
    CapLicksEnabledState = nextState;
  }
}

// send the current telemetry data to the host
static void capSendTelemetry() {
  byte baselineFallback0 = 0;
  byte baselineFallback1 = 0;
  uint16_t baseline0 = capBaselineData(0, baselineFallback0);
  uint16_t baseline1 = capBaselineData(1, baselineFallback1);
  uint16_t filtered0 = capFilteredData(0);
  uint16_t filtered1 = capFilteredData(1);
  int delta0 = int(filtered0) - int(baseline0);
  int delta1 = int(filtered1) - int(baseline1);
  byte baselineFallbackFlag = (baselineFallback0 || baselineFallback1) ? 1 : 0;
  char capBuffer[128];
  unsigned int trialNum = SMARTCurrentTrialNum;
  sprintf(capBuffer, "CB,%u,%u,%u,%u,%u,%d,%d,%u", trialNum, baseline0,
          baseline1, filtered0, filtered1, delta0, delta1,
          baselineFallbackFlag);
  Serial.println(capBuffer);
}

/********** gpSMART **********/
gpSMART smart;
extern TrialResult trial_res;
extern volatile bool
    smartFinished; // Has the system exited the matrix (final state)?
extern volatile bool smartRunning; // 1 if state matrix is running
extern byte smartFlag[5];
extern unsigned int SMARTCurrentTrialNum;
// noise
const byte noisePin = 3;
byte LowBit;

/********** Trial related **********/
#define RECORD_TRIALS 500 // record recent 500 trials history
const byte easy_perf_trials =
    100; // Calculate performance for recent 100 easy trials
const byte easy_perf_side_trials =
    50; // Calculate performance for recent 50 easy trials on each side
const int EasyPerf50SwitchThreshold = 75;
const unsigned int RetentionWindowDefaultTrials = 100;
const unsigned int RetentionWindowDefaultHits = 10;
const int RetentionPerfSwitchThreshold = 75;
const float SideAntiBiasMinProb = 0.2f;
const float SideAntiBiasMaxProb = 0.8f;
const unsigned long Protocol0MinDurationSec = 7UL * 24UL * 60UL * 60UL;
const unsigned long BlockRuleMinDurationSec = 3UL * 24UL * 60UL * 60UL;
// Define  RewardFlag struct.
typedef struct {
  byte flag_L_water;
  byte flag_R_water;
  byte flag_M_water;
  unsigned int past_trials;
} RewardFlag;

typedef struct {
  // Public
  unsigned int currTrialNum = 0;       // current trial number
  byte currProtocolIndex = 0;          // index of Protocol
  unsigned int currProtocolTrials = 0; // number of trials in current protocol
  float currProtocolPerf = 0;          // performance: 0-100%
  byte TrialPresentMode = 0; // 0"pattern",1"random",2"antiBias",3"fixed"
  byte ProtocolIndexHistory[RECORD_TRIALS] = {};
  byte TrialTypeHistory[RECORD_TRIALS] =
      {};                                 // 0 undef; 1 left; 2 right; 3 middle;
  byte Stimu1History[RECORD_TRIALS] = {}; // 0 undef; 1 left; 2 right; 3 middle;
  byte Stimu2History[RECORD_TRIALS] = {}; // 0 undef; 1 left; 2 right; 3 middle;
  byte OutcomeHistory[RECORD_TRIALS] =
      {}; // 0 no-response; 1 correct; 2 error; 3 others
  byte SampleTypeHistory[RECORD_TRIALS] = {}; // 2 easy valid stimulus
  byte EarlyLickHistory[RECORD_TRIALS] =
      {}; // 0-no earlylick; 1-earlylick; 2-undef
  unsigned int totalRewardNum = 0;
  unsigned int retention_counter = 0;
  byte reward_left = 30;
  byte reward_right = 30;
  byte reward_middle = 30;
  byte high_light_intensity = 100;
  byte low_light_intensity = 10;
  unsigned long Trial_txt_position = 0; // currently Trial file cursor position
  unsigned long Tevent_txt_position =
      0; // currently Tevent file cursor position
  RewardFlag GaveFreeReward = {
      0, 0, 0, 0}; // [freeReward flag L, R, M, past_trials] todo...

  // Task-specific
  int TrialBlockInitPeriod = 50;
  int SamplePeriod = 1000;
  int DelayPeriod = 250;
  int PreCueDelayPeriod = 44;
  int PreCuePeriod = 1000;
  int TimeOut = 2000;
  int AnswerPeriod = 10000;
  int ConsumptionPeriod = 750;
  int StopLickingPeriod = 1000;
  int EarlyLickPeriod = 100;
  int extra_TimeOut = 0;
  unsigned long InterBlockIntervalMs = 3600000UL;
  unsigned int hardestTrials = 0;
  int rule = 1;
} Parameters_behavior;
Parameters_behavior S;

// Fundemental training Parameters
const int Fundemental_SamplePeriod = 1000;
/********** Define OutputAction**********/
OutputAction LeftWaterOutput = {"DO1", 1};
OutputAction RightWaterOutput = {"DO2", 1};

/// @brief /////////////// top buzz sound
OutputAction CueOutput = {"tPWM2", 6}; // 32KHz
OutputAction NoiseOutput = {"Flag1", 1};

/// @brief /////////////// left buzz sound
OutputAction LeftLowSoundOutput = {"tPWM1", 1};  // tPWM1 left sound , 3KHz
OutputAction LeftHighSoundOutput = {"tPWM1", 2}; // tPWM1 left sound, 12KHz
OutputAction LeftCueSoundOutput = {"tPWM1", 3};  // tPWM1 left sound , 6KHz

OutputAction LeftLowMidSoundOutput = {"tPWM1", 4};  // tPWM1 left sound, 4.5K
OutputAction LeftHighMidSoundOutput = {"tPWM1", 5}; // tPWM1 left sound , 8.5KHz

/// @brief /////////////// right buzz sound
OutputAction RightLowSoundOutput = {"tPWM3", 1};  // tPWM3 right sound
OutputAction RightHighSoundOutput = {"tPWM3", 2}; // tPWM3 right sound
OutputAction RightCueSoundOutput = {"tPWM3", 3};  // tPWM3 right sound

OutputAction RightLowMidSoundOutput = {"tPWM3", 4};  // tPWM3 right sound
OutputAction RightHighMidSoundOutput = {"tPWM3", 5}; // tPWM3 right sound

OutputAction TimeAlignmentOutput = {
    "Flag3", 3}; // schedule a time alignment signal in loop()
OutputAction TimeAlignmentDisableOutput = {"DACTimeAlignment",
                                           1};         // disable time alignment
OutputAction SpikeRecordingOutput = {"SerialCode", 2}; // open mode Spike
OutputAction LFPRecordingOutput = {"SerialCode", 1};   // open mode LFP
OutputAction Inactive_Output = {"Flag2", 1};
OutputAction smartFinish_Output = {"Flag4", 1};
OutputAction Inavailable_Output = {"Flag5", 1};

OutputAction CapReinitOutput = {"Flag3", 1};  // Flag 2: cap disable; 1: re init
OutputAction CapDisableOutput = {"Flag3", 2}; // Flag 2: cap disable; 1: re init
// sound frequency ;sound orients ;light orients ;reversal trials ;wavelength
// ;light intensity;

byte TrialOutcome = 3; // 0 no-response; 1 correct; 2 error; 3 others
byte is_earlylick = 2; // 0-no earlylick; 1-earlylick; 2-undef
byte MaxSame = 10;     // maximum same EL trials in the last n trials
unsigned int Perf100 = 0;
unsigned int EarlyLick100 = 0;
unsigned long last_reward_time = 0;
unsigned long last_update_time = 0;
int timed_reward_count = 0;
unsigned long TrialStartTimestampMs = 0; // millis() at TimeAlignmentOutput
bool TrialStartTimestampValid = false;   // false => write -1 to Trial.txt

int EL_Favor = 1;          // 1 no favor; 0 favor
int BaselineTrialFlag = 0;
byte CapSaveRequested = 0;
byte pause_signal_PC = 0;
bool paused = 1;
byte TrialTriggerInputGateEnabled =
    1; // 1: DI0 enabled; 0: all digital inputs disabled
byte TrialTriggerInputGateRequested = 1;
byte TrialTriggerInputGateApplyPending = 0;
const byte TrialTriggerFreeWaterProtocolIndex = 255;
const unsigned int TrialTriggerFreeWaterRewardDurationMs = 200;
byte TrialTriggerFreeWaterActive = 0;
byte TrialTriggerFreeWaterRequested = 0;
byte TrialTriggerFreeWaterApplyPending = 0;
byte TrialTriggerSavedProtocolIndex = 0;
unsigned int TrialTriggerSavedProtocolTrials = 0;
float TrialTriggerSavedProtocolPerf = 0.0f;
unsigned long TrialTriggerSavedCurrentProtocolFirstTrialUnix = 0;
unsigned long TrialTriggerSavedCurrentRuleFirstTrialUnix = 0;
byte TrialTriggerSavedInputGateEnabled = 1;
byte TrialTriggerSavedInputGateRequested = 1;
byte TrialTriggerSavedInterBlockIntervalGateEnabled = 1;
byte TrialTriggerSavedTrialBlockFreeRewardPending = 1;
byte TrialTriggerSavedTrialBlockReadyCuePlayed = 0;
unsigned long TrialTriggerSavedLastTrialBlockTriggerMs = 0;
byte PauseAfterTrialTriggerFreeWaterExitRequested = 0;
byte InterBlockIntervalGateEnabled = 1;
byte ledState = LOW;
byte TrialBlockOnset = 1; // current trial is the first trial of trialblock
byte TrialBlockFreeRewardPending = 1;
byte BlockSwitchMode = 0;
byte BlockSwitchStep = 0;
byte BlockSwitchSecondRule = 1;
byte PendingProtocolIndex = 255;
unsigned long CurrentProtocolFirstTrialUnix = 0;
unsigned long CurrentRuleFirstTrialUnix = 0;
unsigned long LastParaSPersistUnix = 0;
const unsigned int RuleSwitchHintCorrectTrialLimit = 5;
byte RuleSwitchHintActive = 0;
unsigned int RuleSwitchCorrectCount = 0;
byte CurrentELPunishEnabled = 0;
byte PreviousRule =
    0; // rule used by the previous protocol block for congruency antibias
unsigned long LastTrialBlockTriggerMs = 0;
byte TrialBlockReadyCuePlayed = 0;
byte ChargingWarningActive = 0;
unsigned long ChargingWarningEndMs = 0;
unsigned long ChargingWarningLastUpdateMs = 0;
byte ChargingWarningLedState = 0;
const byte ChargingWarningFanDOIndex = 2; // DO3 relay output, Teensy pin 32
const unsigned long ChargingWarningRampMs = 1500;
unsigned long ChargingWarningStartMs = 0;
unsigned long ChargingWarningNextLedMs = 0;
unsigned long ChargingWarningNextFanMs = 0;
byte ChargingWarningLedPhase = 0;
byte ChargingWarningFanEnabled = 1;
byte ChargingWarningFanState = 0;

float Alpha500 = 0.998;
float currProtocolPerf_corrected = 0;

byte TrialType = 1;          // 0 undef; 1 left; 2 right; 3 middle;
byte SampleType = 2;
float currStimu[2] = {1, 1}; // stimu1: sound orientation[-1, 1];
                             // stimu2: sound freq[-1, 1]

float S1Feat[2] = {-1.0, 1.0};
float S2EasyFeat[2] = {-1.0, 1.0};

String HostProtocolBlockId = "block_000_p0";
byte HostProtocolIndex = 0;
unsigned int HostConditionTrialMin = 0;
byte HostConditionPerfMode = 0; // 0 none, 1 side50, 2 retention100, 3 legacy p0 time
byte HostConditionThreshold = 0;
unsigned int HostConditionWindowSize = 0;
byte HostConditionRequiredCount = 1;
unsigned int HostRetentionWindowValid = 0;
unsigned int HostRetentionWindowCorrect = 0;
byte HostRetentionHits = 0;
unsigned int HostRetentionLastWindowPerf = 0;
unsigned long HostConditionTimeMinSec = Protocol0MinDurationSec;

const byte HostFlowMaxBlocks = 24;
byte HostFlowLength = 0;
byte HostFlowCurrentIndex = 0;
byte HostFlowProtocols[HostFlowMaxBlocks];
unsigned int HostFlowTrialMin[HostFlowMaxBlocks];
unsigned long HostFlowTimeMinSec[HostFlowMaxBlocks];
byte HostFlowPerfMode[HostFlowMaxBlocks];
byte HostFlowThreshold[HostFlowMaxBlocks];
unsigned int HostFlowWindowSize[HostFlowMaxBlocks];
byte HostFlowRequiredCount[HostFlowMaxBlocks];
byte HostFlowFixed[HostFlowMaxBlocks];

static unsigned long currentUnixTime() {
  return (unsigned long)Teensy3Clock.get();
}

static bool isBlockRuleProtocol(byte protocol_index) {
  return protocol_index == 5 || protocol_index == 7 || protocol_index == 9;
}

static bool hasElapsedSince(unsigned long start_unix,
                            unsigned long duration_sec) {
  if (start_unix == 0) {
    return false;
  }
  unsigned long now_unix = currentUnixTime();
  if (now_unix < start_unix) {
    return false;
  }
  return (now_unix - start_unix) >= duration_sec;
}

static bool applyPendingProtocolSwitchIfReady(bool force_now);
static void initDefaultHostProtocolFlow();
static String hostFlowBlockId(byte index);
static void syncHostFlowIndexToProtocol(byte protocol_index);
static void applyCurrentHostFlowBlock(bool reset_progress_if_changed);
static void storeCurrentConditionInFlowBlock();
static bool parseHostProtocolCondition(const char *buf);
static bool parseHostProtocolFlow(const char *buf);
static String buildHostProtocolFlowCommand();
static bool advanceHostProtocolFlow(bool force_now);
static bool isHostProtocolConditionReady();
static void updateHostRetentionProgressAfterTrial();
static void resetHostRetentionProgress();
static void appendProgressField(String &packet, const char *key,
                                const String &value);
static void appendByteArrayProgressField(String &packet, const char *key,
                                         const byte *values, byte length);
static void appendUIntArrayProgressField(String &packet, const char *key,
                                         const unsigned int *values,
                                         byte length);
static void appendULongArrayProgressField(String &packet, const char *key,
                                          const unsigned long *values,
                                          byte length);
void trialSelection();
void switchToProtocol(byte next_protocol);
int write_SD_para_S();
void SendProtocolProgress2PC();

static void setTrialBlockAvailabilityLights(byte enabled) {
  byte lightValue = enabled ? S.low_light_intensity : 0;
  analogWrite(20, lightValue);
  analogWrite(5, lightValue);
}

static byte isTrialTriggerInputEnabled() {
  return (TrialTriggerInputGateEnabled == 1 &&
          InterBlockIntervalGateEnabled == 1)
             ? 1
             : 0;
}

static void applyTrialTriggerInputGate() {
  byte portEnabled[4] = {0, 0, 0, 0};
  if (isTrialTriggerInputEnabled() == 1) {
    portEnabled[0] = 1;
  }
  smart.setDigitalInputsEnabled(portEnabled);
}

static bool isTrialTriggerFreeWaterActive() {
  return false;
}

static void clearTrialTriggerFreeWaterState() {
  TrialTriggerFreeWaterActive = 0;
  TrialTriggerFreeWaterRequested = 0;
  TrialTriggerFreeWaterApplyPending = 0;
}

static void applyPauseAfterTrialTriggerFreeWaterExitIfNeeded() {
  if (PauseAfterTrialTriggerFreeWaterExitRequested == 0 ||
      TrialTriggerFreeWaterActive == 1) {
    return;
  }
  pause_signal_PC = 1;
  PauseAfterTrialTriggerFreeWaterExitRequested = 0;
}

static bool isTrialTriggerFreeWaterRestoreSafe() {
  return TrialBlockOnset == 1 || !smartRunning;
}

static bool isProtocolSwitchSafeNow() {
  return TrialBlockOnset == 1 || !smartRunning;
}

static void requestExitTrialTriggerFreeWaterProtocol() {
  if (TrialTriggerFreeWaterActive == 0) {
    return;
  }
  TrialTriggerFreeWaterRequested = 0;
  TrialTriggerFreeWaterApplyPending = 1;
}

static byte persistedProtocolIndex() {
  if (isTrialTriggerFreeWaterActive()) {
    return TrialTriggerSavedProtocolIndex;
  }
  return S.currProtocolIndex;
}

static unsigned int persistedProtocolTrials() {
  if (isTrialTriggerFreeWaterActive()) {
    return TrialTriggerSavedProtocolTrials;
  }
  return S.currProtocolTrials;
}

static float persistedProtocolPerf() {
  if (isTrialTriggerFreeWaterActive()) {
    return TrialTriggerSavedProtocolPerf;
  }
  return S.currProtocolPerf;
}

static void enterTrialTriggerFreeWaterProtocol() {
  if (isTrialTriggerFreeWaterActive()) {
    return;
  }
  TrialTriggerSavedProtocolIndex = S.currProtocolIndex;
  TrialTriggerSavedProtocolTrials = S.currProtocolTrials;
  TrialTriggerSavedProtocolPerf = S.currProtocolPerf;
  TrialTriggerSavedCurrentProtocolFirstTrialUnix = CurrentProtocolFirstTrialUnix;
  TrialTriggerSavedCurrentRuleFirstTrialUnix = CurrentRuleFirstTrialUnix;
  TrialTriggerSavedInputGateEnabled = TrialTriggerInputGateEnabled;
  TrialTriggerSavedInputGateRequested = TrialTriggerInputGateRequested;
  TrialTriggerSavedInterBlockIntervalGateEnabled = InterBlockIntervalGateEnabled;
  TrialTriggerSavedTrialBlockFreeRewardPending = TrialBlockFreeRewardPending;
  TrialTriggerSavedTrialBlockReadyCuePlayed = TrialBlockReadyCuePlayed;
  TrialTriggerSavedLastTrialBlockTriggerMs = LastTrialBlockTriggerMs;
  TrialTriggerFreeWaterActive = 1;
  S.currProtocolIndex = TrialTriggerFreeWaterProtocolIndex;
  S.currProtocolTrials = TrialTriggerSavedProtocolTrials;
  S.currProtocolPerf = TrialTriggerSavedProtocolPerf;
  TrialTriggerInputGateEnabled = 1;
  TrialTriggerInputGateRequested = 1;
  InterBlockIntervalGateEnabled = 1;
  TrialBlockOnset = 1;
  TrialBlockFreeRewardPending = 1;
  TrialBlockReadyCuePlayed = 0;
  LastTrialBlockTriggerMs = 0;
  setTrialBlockAvailabilityLights(0);
  applyTrialTriggerInputGate();
  trialSelection();
  SendProtocolProgress2PC();
}

static void exitTrialTriggerFreeWaterProtocol() {
  if (TrialTriggerFreeWaterActive == 0) {
    return;
  }
  if (S.currProtocolIndex == TrialTriggerFreeWaterProtocolIndex) {
    S.currProtocolIndex = TrialTriggerSavedProtocolIndex;
    S.currProtocolTrials = TrialTriggerSavedProtocolTrials;
    S.currProtocolPerf = TrialTriggerSavedProtocolPerf;
    CurrentProtocolFirstTrialUnix =
        TrialTriggerSavedCurrentProtocolFirstTrialUnix;
    CurrentRuleFirstTrialUnix = TrialTriggerSavedCurrentRuleFirstTrialUnix;
    TrialTriggerInputGateEnabled = TrialTriggerSavedInputGateEnabled;
    TrialTriggerInputGateRequested = TrialTriggerSavedInputGateRequested;
    InterBlockIntervalGateEnabled =
        TrialTriggerSavedInterBlockIntervalGateEnabled;
    TrialBlockOnset = 1;
    TrialBlockFreeRewardPending = TrialTriggerSavedTrialBlockFreeRewardPending;
    TrialBlockReadyCuePlayed = TrialTriggerSavedTrialBlockReadyCuePlayed;
    LastTrialBlockTriggerMs = TrialTriggerSavedLastTrialBlockTriggerMs;
    setTrialBlockAvailabilityLights(0);
    applyTrialTriggerInputGate();
    clearTrialTriggerFreeWaterState();
    trialSelection();
    write_SD_para_S();
    SendProtocolProgress2PC();
    applyPauseAfterTrialTriggerFreeWaterExitIfNeeded();
    return;
  }
  // A manual protocol switch already moved the firmware out of the temporary
  // free-water state. Clear only the overlay flag so later D1 acks cannot
  // report a stale pause.
  clearTrialTriggerFreeWaterState();
  write_SD_para_S();
  SendProtocolProgress2PC();
  applyPauseAfterTrialTriggerFreeWaterExitIfNeeded();
}

static void processTrialTriggerFreeWaterRequest() {
  if (TrialTriggerFreeWaterApplyPending == 0) {
    return;
  }
  if (TrialTriggerFreeWaterRequested == 1) {
    if (TrialBlockOnset != 1) {
      return; // only enter the temporary free-water state when no trial is running
    }
    enterTrialTriggerFreeWaterProtocol();
  } else {
    if (!isTrialTriggerFreeWaterRestoreSafe()) {
      return; // restore as soon as the current trial reaches a safe boundary
    }
    exitTrialTriggerFreeWaterProtocol();
  }
  TrialTriggerFreeWaterApplyPending = 0;
  char gateStatusBuffer[8];
  sprintf(gateStatusBuffer, "SD%d", TrialTriggerFreeWaterActive ? 0 : 1);
  Serial.println(gateStatusBuffer);
}

static void processTrialTriggerInputGateRequest() {
  if (TrialTriggerInputGateApplyPending == 0) {
    return;
  }
  if (TrialBlockOnset != 1) {
    return; // only apply when no trial is running
  }
  TrialTriggerInputGateEnabled = TrialTriggerInputGateRequested;
  applyTrialTriggerInputGate();
  TrialTriggerInputGateApplyPending = 0;
  char gateStatusBuffer[8];
  sprintf(gateStatusBuffer, "SD%d", TrialTriggerInputGateEnabled);
  Serial.println(gateStatusBuffer);
}

static bool isInterBlockIntervalElapsed() {
  if (LastTrialBlockTriggerMs == 0) {
    return true;
  }
  return (millis() - LastTrialBlockTriggerMs) >= S.InterBlockIntervalMs;
}

static void refreshInterBlockIntervalTriggerGate() {
  byte requested = 1;
  if (!isTrialTriggerFreeWaterActive() && BlockSwitchMode == 1 && TrialBlockOnset == 1 &&
      !isInterBlockIntervalElapsed()) {
    requested = 0;
  }
  if (requested == InterBlockIntervalGateEnabled) {
    return;
  }
  InterBlockIntervalGateEnabled = requested;
  applyTrialTriggerInputGate();
}

static void refreshInterBlockReadyCue() {
  if (TrialBlockOnset != 1) {
    TrialBlockReadyCuePlayed = 0;
    return;
  }
  bool intervalReady =
      (BlockSwitchMode == 1) ? isInterBlockIntervalElapsed() : true;
  bool shouldPlayReadyCue =
      (intervalReady && TrialTriggerInputGateEnabled == 1);
  setTrialBlockAvailabilityLights(isTrialTriggerInputEnabled());
  if (shouldPlayReadyCue && TrialBlockReadyCuePlayed == 0) {
    playInterBlockReadyCue();
    TrialBlockReadyCuePlayed = 1;
    Serial.println("SI1");
  } else if (!shouldPlayReadyCue) {
    TrialBlockReadyCuePlayed = 0;
  }
}

static void markTrialBlockTriggered() {
  LastTrialBlockTriggerMs = millis();
  TrialBlockReadyCuePlayed = 0;
}

static void playInterBlockReadyCue() {
  const uint32_t topSpeakerFreqs[5] = {3000, 4243, 6000, 8485, 12000};
  setTrialBlockAvailabilityLights(1);
  for (int i = 0; i < 5; i++) {
    analogWriteFrequency(2, topSpeakerFreqs[i]);
    analogWrite(2, 128);
    delay(200);
  }
  analogWrite(2, 0);
}

static void setChargingWarningLights(byte left, byte middle, byte right) {
  analogWrite(5, left);
  analogWrite(9, middle);
  analogWrite(20, right);
}

static void setChargingWarningFan(byte enabled) {
  digitalWrite(gpSMART_DO_Lines[ChargingWarningFanDOIndex],
               enabled ? HIGH : LOW);
  static byte toneOn = 0;
  static unsigned long nextToneSwitchMs = 0;
  if (enabled) {
    unsigned long now_ms = millis();
    if ((long)(now_ms - nextToneSwitchMs) >= 0) {
      toneOn = !toneOn;
      if (toneOn) {
        uint32_t toneFreq = (uint32_t)random(16000, 32001);
        analogWriteFrequency(noisePin, toneFreq);
        analogWrite(noisePin, 128); // 50% duty ultrasonic burst
        nextToneSwitchMs = now_ms + (unsigned long)random(40, 180);
      } else {
        analogWrite(noisePin, 0);
        nextToneSwitchMs = now_ms + (unsigned long)random(20, 120);
      }
    }
  } else {
    toneOn = 0;
    nextToneSwitchMs = 0;
    analogWrite(noisePin, 0);
  }
}

static void stopChargingWarning(byte report) {
  ChargingWarningActive = 0;
  ChargingWarningEndMs = 0;
  ChargingWarningLastUpdateMs = 0;
  ChargingWarningStartMs = 0;
  ChargingWarningNextLedMs = 0;
  ChargingWarningNextFanMs = 0;
  ChargingWarningLedState = 0;
  ChargingWarningLedPhase = 0;
  ChargingWarningFanState = 0;
  analogWrite(2, 0);
  digitalWrite(noisePin, LOW);
  setChargingWarningLights(0, 0, 0);
  setChargingWarningFan(0);
  if (report) {
    Serial.println("SW0");
  }
}

static void startChargingWarning(unsigned long duration_ms, byte fan_enabled) {
  if (duration_ms == 0) {
    stopChargingWarning(1);
    return;
  }
  unsigned long now_ms = millis();
  ChargingWarningActive = 1;
  ChargingWarningEndMs = now_ms + duration_ms;
  ChargingWarningLastUpdateMs = now_ms;
  ChargingWarningStartMs = now_ms;
  ChargingWarningNextLedMs = now_ms;
  ChargingWarningNextFanMs = now_ms + (unsigned long)random(200, 900);
  ChargingWarningLedState = 0;
  ChargingWarningLedPhase = 0;
  ChargingWarningFanEnabled = fan_enabled ? 1 : 0;
  ChargingWarningFanState = 0;
  analogWrite(2, 0);
  digitalWrite(noisePin, LOW);
  setChargingWarningLights(0, 0, 0);
  setChargingWarningFan(0);
  Serial.println("SW1");
}

static void updateChargingWarning() {
  if (!ChargingWarningActive) {
    return;
  }
  unsigned long now_ms = millis();
  if ((long)(now_ms - ChargingWarningEndMs) >= 0) {
    stopChargingWarning(1);
    return;
  }

  if (ChargingWarningFanEnabled) {
    if ((long)(now_ms - ChargingWarningNextFanMs) >= 0) {
      ChargingWarningFanState = !ChargingWarningFanState;
      if (ChargingWarningFanState) {
        ChargingWarningNextFanMs = now_ms + (unsigned long)random(1000, 3000);
      } else {
        ChargingWarningNextFanMs = now_ms + (unsigned long)random(400, 2500);
      }
    }
    setChargingWarningFan(ChargingWarningFanState);
  } else {
    setChargingWarningFan(0);
  }

  if ((long)(now_ms - ChargingWarningNextLedMs) < 0) {
    return;
  }
  ChargingWarningLastUpdateMs = now_ms;
  unsigned long elapsed_ms = now_ms - ChargingWarningStartMs;
  byte left = 0;
  byte middle = 0;
  byte right = 0;

  if (elapsed_ms < ChargingWarningRampMs) {
    byte peak =
        (byte)(40 + (215UL * elapsed_ms) / ChargingWarningRampMs);
    byte spill = peak / 4;
    switch (ChargingWarningLedPhase % 3) {
    case 0:
      left = peak;
      middle = spill;
      break;
    case 1:
      middle = peak;
      right = spill;
      break;
    default:
      right = peak;
      left = spill;
      break;
    }
    ChargingWarningNextLedMs = now_ms + (unsigned long)random(80, 180);
  } else if (random(0, 100) < 14) {
    ChargingWarningNextLedMs = now_ms + (unsigned long)random(120, 360);
  } else {
    byte peak = (byte)random(130, 256);
    byte spill = (byte)random(15, 90);
    switch (random(0, 3)) {
    case 0:
      left = peak;
      middle = spill;
      break;
    case 1:
      middle = peak;
      left = spill / 2;
      right = spill;
      break;
    default:
      right = peak;
      middle = spill;
      break;
    }
    ChargingWarningNextLedMs = now_ms + (unsigned long)random(60, 260);
  }

  ChargingWarningLedState = (left || middle || right) ? 1 : 0;
  ChargingWarningLedPhase++;
  setChargingWarningLights(left, middle, right);
}
/********** Communication ***********/
char packetBuffer[1024]; // buffer to hold incoming packet
char outBuffer[2048];   // buffer to hold outcoming packet,UDP max 1472
int cage_id = 101;
char task_name[40];

/********** Time alignment DAC parameter ***********/
const float FREQ = 4000.0f;               // TODO 4 kHz
const float FS = AUDIO_SAMPLE_RATE_EXACT; // ≈ 44.1 kHz
const int SEG_SAMPLES = 44;               // 1 ms ≈ 44 样本（0.998 ms）
// const int AUDIO_BLOCK_SAMPLES = 128;
const int TIME_ALIGNMENT_BLOCK_COUNT = 4;
const int TIME_ALIGNMENT_TOTAL_SAMPLES =
    AUDIO_BLOCK_SAMPLES * TIME_ALIGNMENT_BLOCK_COUNT;
const int TrialStartStateDurationMs = 18;
const byte TimeAlignmentChipCount = 9;
const byte TimeAlignmentChipPattern[TimeAlignmentChipCount] = {1, 1, 1, 0, 0,
                                                               1, 0, 1, 1};
const int16_t AMP = 32766; // 全幅,用来调节 发射脉冲的强度，其中/3
                           // 能保证波形完整谐波更小，使用全幅
                           // 几乎为方波，但交流有效值最大，电磁脉冲更强
const int16_t offset = 32766 - AMP;
const int16_t zero_voltage = -32768;

static void setTimeAlignmentSample(int sample_index, int16_t value) {
  if (sample_index < AUDIO_BLOCK_SAMPLES) {
    block0[sample_index] = value;
  } else if (sample_index < 2 * AUDIO_BLOCK_SAMPLES) {
    block1[sample_index - AUDIO_BLOCK_SAMPLES] = value;
  } else if (sample_index < 3 * AUDIO_BLOCK_SAMPLES) {
    block2[sample_index - 2 * AUDIO_BLOCK_SAMPLES] = value;
  } else if (sample_index < TIME_ALIGNMENT_TOTAL_SAMPLES) {
    block3[sample_index - 3 * AUDIO_BLOCK_SAMPLES] = value;
  }
}

static void buildTimeAlignmentBlocks(const int16_t *seg_sine) {
  for (int i = 0; i < AUDIO_BLOCK_SAMPLES; ++i) {
    block0[i] = zero_voltage;
    block1[i] = zero_voltage;
    block2[i] = zero_voltage;
    block3[i] = zero_voltage;
  }

  int write_index = 0;
  for (int chip = 0; chip < TimeAlignmentChipCount; ++chip) {
    for (int sample = 0;
         sample < SEG_SAMPLES && write_index < TIME_ALIGNMENT_TOTAL_SAMPLES;
         ++sample, ++write_index) {
      int16_t sample_value =
          TimeAlignmentChipPattern[chip] ? seg_sine[sample] : zero_voltage;
      setTimeAlignmentSample(write_index, sample_value);
    }
  }
}

/****************************************************************************************************/
/********************************************** Setup()
 * *********************************************/
/****************************************************************************************************/

void setup() {
  delay(3000);          // for debug
  Serial.begin(115200); // initialize seiral for debugging
  Serial.setTimeout(10);
  pinMode(ledPin, OUTPUT);
  pinMode(switchPin, INPUT_PULLUP); // low if switch on; hight if switch off

  /********** DAC sine output for time alignment***********/
  AudioMemory(20);
  queue1.setBehaviour(AudioPlayQueue::NON_STALLING);
  queue1.setMaxBuffers(20);
  dc1.amplitude(-1.0);
  int16_t seg_sine[SEG_SAMPLES];
  float dphi = 2.0f * M_PI * FREQ / FS;
  float phi = 0.0f;
  for (int i = 0; i < SEG_SAMPLES; ++i) {
    seg_sine[i] = (int16_t)(AMP * sinf(phi) - offset);
    phi += dphi;
  }

  // Use a 1 ms-per-chip OOK code on the 4 kHz carrier so the wireless receiver
  // can detect it more reliably from the envelope.
  buildTimeAlignmentBlocks(seg_sine);
  /********** SD card ***********/
  if (!SD.begin(chipSelect)) {
    Serial.println("SD Card failed, or not present");
    return; // don't do anything more:
  } else {
    Serial.println("SD is working...");
  }
  if (read_SD_cage_info() < 0) { // read cage_id
    cage_id = 101;
    sprintf(task_name, "TBD");
  }
  FLASHLED(200);

  /********** gpSMART ***********/
  smart.init(CapElectrodesUsed); // init smart with num_electrodes used
  pinMode(gpSMART_DO_Lines[ChargingWarningFanDOIndex], OUTPUT);
  setChargingWarningFan(0);
  smart.setLicksDetectionEnabled(0);
  capSetLicksEnabledState(0, 1);
  delay(100);
  applyTrialTriggerInputGate();
  /// tPWM2: top
  smart.setTruePWMFrequency(2, 1, 3000,
                            128); // (low sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(2, 2, 12000,
                            128); // (high sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(2, 3, 6000,
                            128); // (go cue) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty

  smart.setTruePWMFrequency(2, 4, 4243, 128);
  smart.setTruePWMFrequency(2, 5, 8485, 128);
  smart.setTruePWMFrequency(2, 6, 32000,
                            128); // (low sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty

  // tPWM1: left sound;
  smart.setTruePWMFrequency(1, 1, 3000,
                            128); // (low sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(1, 2, 12000,
                            128); // (high sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(1, 3, 6000,
                            128); // (go cue) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty

  smart.setTruePWMFrequency(1, 4, 4243, 128);
  smart.setTruePWMFrequency(1, 5, 8485, 128);

  //  tPWM3: right sound
  smart.setTruePWMFrequency(3, 1, 3000,
                            128); // (low sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(3, 2, 12000,
                            128); // (high sound) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty
  smart.setTruePWMFrequency(3, 3, 6000,
                            128); // (go cue) byte tPWM_num, byte freq_num,
                                  // uint32 frequency, byte duty

  smart.setTruePWMFrequency(3, 4, 4243, 128);
  smart.setTruePWMFrequency(3, 5, 8485, 128);
  FLASHLED(200);

  initDefaultHostProtocolFlow();
  // read parameters from SD Card to override S
  read_SD_para_S();
  SMARTCurrentTrialNum = S.currTrialNum; // for alignment trialIndex
  // write an artificial event to mark the restart of Arduino board
  Ev.events_num = 0;
  Ev.events_id[Ev.events_num] = 1; // restart;
  Ev.events_time[Ev.events_num] = Teensy3Clock.get();
  Ev.events_value[Ev.events_num] = -1;
  Ev.events_num = 1;
  write_SD_event(); // write event to file
  Ev.events_num = 0;
  // random seeds
  randomSeed(analogRead(0));
  trialSelection();

  /********** Online Communication ***********/
  sprintf(outBuffer, "SE: Cage %d is online now; IP: %s", cage_id,
          task_name); // Special message starts with 'S'
  Serial.println(outBuffer);
  FLASHLED(200);

  digitalWrite(ledPin, HIGH); // Light up LED to indicate init finished
}

/****************************************************************************************************/
/********************************************** Loop()
 * **********************************************/
/****************************************************************************************************/
void loop() {
  updateChargingWarning();
  processTrialTriggerFreeWaterRequest();
  processTrialTriggerInputGateRequest();
  refreshInterBlockIntervalTriggerGate();

  // Check if the toggle switch is ON
  if (digitalRead(switchPin) == 0 &&
      pause_signal_PC == 0) { // if yes, run the state matrix
    if (paused == 1) {
      paused = 0;
      digitalWrite(ledPin, HIGH);
      Serial.println("M: Program RESUME!!!");
      // 每次resume都要重新初始化cap值
      capReinitSafely(CapElectrodesUsed);
      applyTrialTriggerInputGate();
      // in case SD card was removed and re-insert, need re-initilization
      SD.begin(chipSelect);
      read_SD_para_S();
      SMARTCurrentTrialNum = S.currTrialNum;
      // free reward to fill the lickport tube
      free_reward(30);
    }
    refreshInterBlockReadyCue();

    if (smartFlag[3]) // smartFinish
    {                 // i.e., a trial is done
      smartFlag[3] = 0;
      digitalWrite(ledPin, HIGH);
      S.currTrialNum++;
      SMARTCurrentTrialNum = S.currTrialNum;
      smart.setLicksDetectionEnabled(0);
      delayMicroseconds(1200); // 等待ISR完成当前周期，避免I2C总线竞争
      capSendTelemetry();
      smart.setLicksDetectionEnabled(1);

      UpdateTrialOutcome();  // including white noise (if error) and inter-trial
                             // interval
      updateHostRetentionProgressAfterTrial();
      write_SD_trial_info(); // log trial info and event to SD
      SendTrialInfo2PC();    // send trial info to PC through Serial

      //////////// for next trial ///////////
      autoChangeProtocol(
          false);        // Change protocol and parameters based on performance;
      autoReward();      // Set Reward Flag if many wrongs in a row;
      trialSelection();  // determine TrialType;
      write_SD_para_S(); // write parameter S (updated after this trial) to SD
                         // card;
      //////////// for next trial ///////////
    } else if (!smartRunning) {
      // Default to -1 for every new trial; set when TrialStart emits
      // TimeAlignmentOutput.
      TrialStartTimestampMs = 0;
      TrialStartTimestampValid = false;
      construct_matrix_and_Run();
      digitalWrite(ledPin, LOW); // Teensy led does not support PWM
    }

    // White noise for error trial or background stimuli
    if (ChargingWarningActive && ChargingWarningFanEnabled &&
        ChargingWarningFanState) {
      // setChargingWarningFan() owns noisePin while warning fan is active.
    } else if (smartFlag[0] == 1) {
      digitalWrite(noisePin, LowBit); // about 55 us/bit
      LowBit = random(2);
    } else {
      digitalWrite(noisePin, LOW);
    }
    // manual cap value read
    if (CapSaveRequested == 1 && TrialBlockOnset == 1) {
      smart.setLicksDetectionEnabled(0);
      delayMicroseconds(1200); // 等待ISR完成当前周期，避免I2C总线竞争
      capSetLicksEnabledState(0, 0);
      capSendTelemetry();
      Serial.println("SC1");
      CapSaveRequested = 0;
    }

    if (smartFlag[2] == 1) { // 打开cap设定为running mode
      smart.setLicksDetectionEnabled(0);
      delayMicroseconds(1200); // 等待ISR完成当前周期，避免I2C总线竞争
      // capSetNormalRunMode();
      capSetBaselineLockedRunMode();
      smart.setLicksDetectionEnabled(1);
      capSetLicksEnabledState(1, 0);
      smartFlag[2] = 0;
    }
    // cap disable
    if (smartFlag[2] == 2) {
      capSetStopMode();
      smart.setLicksDetectionEnabled(0);
      capSetLicksEnabledState(0, 0);
      smartFlag[2] = 0;
    }
    if (smartFlag[2] == 3) {
      TrialStartTimestampMs = millis();
      TrialStartTimestampValid = true;
      bool shouldPersistTimingState = false;
      unsigned long now_unix = currentUnixTime();
      if (!isTrialTriggerFreeWaterActive() && CurrentProtocolFirstTrialUnix == 0) {
        CurrentProtocolFirstTrialUnix = now_unix;
        shouldPersistTimingState = true;
      }
      if (!isTrialTriggerFreeWaterActive() && BlockSwitchMode == 1 && isBlockRuleProtocol(S.currProtocolIndex) &&
          CurrentRuleFirstTrialUnix == 0) {
        CurrentRuleFirstTrialUnix = now_unix;
        shouldPersistTimingState = true;
      }
      if (shouldPersistTimingState) {
        write_SD_para_S();
      }
      smart.TimeAlignmentInTrial();
      Serial.print((String) "C:" + (SMARTCurrentTrialNum + 1));
      smartFlag[2] = 0;
    }

    // trial block setup
    if (smartFlag[1] == 1) {
      // Trial block end: inactive
      TrialBlockOnset = 1;
      TrialBlockFreeRewardPending = 1;
      TrialBlockReadyCuePlayed = 0;
      if (BlockSwitchMode == 1) {
        markTrialBlockTriggered();
        refreshInterBlockIntervalTriggerGate();
        setTrialBlockAvailabilityLights(0);
      } else {
        byte trialEndLightIntensity =
            S.low_light_intensity > 3 ? S.low_light_intensity / 3 : 1;
        // 点亮左右灯，光强度与 TrialEnd 状态保持一致（低亮度）
        analogWrite(20, trialEndLightIntensity);
        analogWrite(5, trialEndLightIntensity);
      }
      if (applyPendingProtocolSwitchIfReady(false)) {
        trialSelection();
        SendProtocolProgress2PC();
        write_SD_para_S();
      }
      smartFlag[1] = 0;
    }

    // Trial init led feedback
    if (millis() - last_update_time > 10) {

      last_update_time = millis();
      if (smartFlag[4] == 0) {
        //  no in inavailable period
        if (digitalRead(gpSMART_DI_Lines[0]) == 0) { // holding the port
          analogWrite(9, S.low_light_intensity);
        } else {
          analogWrite(9, 0);
        }
      } else {
        analogWrite(9, 0);
      }
    }

    if (millis() - last_reward_time >
        3 * 3600000) { // there is no reward in last 3 hours
      // free reward to fill the lickport tube
      free_reward(50);
      last_reward_time = millis();
      Serial.println("M: No Reward in Last 3 Hours.");

      timed_reward_count++;

      if (timed_reward_count >= 4) { // in last 12 hours no reward
        timed_reward_count = 0;
        // more free reward to fill the lickport tube
        free_reward(100);
        last_reward_time = millis();
        Serial.println("E: No Reward in Last 12 Hours.");
      }
    }

    // write current event info
    if (Ev.events_num > 0) {
      write_SD_event();
      Ev.events_num = 0;
    }
  }

  else { // if the toggle switch is off
    if (paused == 0) {
      paused = 1; // execute only one time
      smart.Stop();
      Serial.println("M: Program PAUSED!!!");
      smart.setLicksDetectionEnabled(0);
      capSetLicksEnabledState(0, 0);
    }
    // Flashing LED
    if (ledState == LOW) {
      ledState = HIGH;
    } else {
      ledState = LOW;
    }
    digitalWrite(ledPin, ledState);
    delay(ChargingWarningActive ? 20 : 200);
    // stimulus valve check
    // auditory top(high) -> top(low) -> left -> right
    //    system_output_check();
  }

  updateChargingWarning();

  /********** Serial communication with PC ***********/
  if (Serial.available() > 0) { // receiving data from PC
    size_t len =
        Serial.readBytesUntil('\n', packetBuffer, sizeof(packetBuffer) - 1);
    packetBuffer[len] = '\0';
    if (len > 0 && packetBuffer[len - 1] == '\r') {
      packetBuffer[len - 1] = '\0';
    }
    if (packetBuffer[0] == '\0') {
      return;
    }
    if (strncmp(packetBuffer, "PFLOW,", 6) == 0) {
      if (parseHostProtocolFlow(packetBuffer)) {
        write_SD_para_S();
        bool advanced = false;
        if (isProtocolSwitchSafeNow() && isHostProtocolConditionReady()) {
          advanced = advanceHostProtocolFlow(false);
        }
        if (!advanced) {
          SendProtocolProgress2PC();
        }
      } else {
        Serial.println("E: Invalid PFLOW command.");
      }
      return;
    }
    if (strncmp(packetBuffer, "PCOND,", 6) == 0) {
      if (parseHostProtocolCondition(packetBuffer)) {
        write_SD_para_S();
        bool advanced = false;
        if (isProtocolSwitchSafeNow() && isHostProtocolConditionReady()) {
          advanced = advanceHostProtocolFlow(false);
        }
        if (!advanced) {
          SendProtocolProgress2PC();
        }
      } else {
        Serial.println("E: Invalid PCOND command.");
      }
      return;
    }
    byte commandByte = packetBuffer[0];
    // T for Time calibration,
    switch (commandByte) {
    case 'T':                   // system test
      if (TrialBlockOnset == 1) // Inactive output
      {
        Teensy3Clock.set(atoi(&packetBuffer[1])); // str2int
        // if the state machine is not running , then align the time between
        // HABITS and wireless recorder
        system_output_check();
      }
      break;
    case 'P': // Pause the system
      if (TrialTriggerFreeWaterActive == 1) {
        PauseAfterTrialTriggerFreeWaterExitRequested = 1;
        requestExitTrialTriggerFreeWaterProtocol();
        processTrialTriggerFreeWaterRequest();
        applyPauseAfterTrialTriggerFreeWaterExitIfNeeded();
      } else {
        pause_signal_PC = 1;
      }
      break;
    case 'M': // resuMe the system
      pause_signal_PC = 0;
      PauseAfterTrialTriggerFreeWaterExitRequested = 0;
      break;
    case 'R': // set reward values for left, right and middle
    {
      int reward_left = 0;
      int reward_right = 0;
      int reward_middle = 0;
      if (parseThreeInts(packetBuffer, reward_left, reward_right,
                         reward_middle)) {
        S.reward_left = constrain(reward_left, 1, 200);
        S.reward_right = constrain(reward_right, 1, 200);
        S.reward_middle = constrain(reward_middle, 1, 200);
        write_SD_para_S();
        free_reward(S.reward_left);
      }
      break;
    }
    case 'L': // set Light intensity value for low and high
    {
      int low_light = 0;
      int high_light = 0;
      if (parseTwoInts(packetBuffer, low_light, high_light)) {
        S.low_light_intensity = constrain(low_light, 0, 255);
        S.high_light_intensity = constrain(high_light, 0, 255);
        write_SD_para_S();
      }
      break;
    }
    case 'A': // read All info
      // send back all info in control panel, like reward values,
      sprintf(outBuffer, "A%d;%d;%d;%d;%d;%d;%d;%lu;%d;%lu;", S.reward_left,
              S.reward_right, S.reward_middle, S.low_light_intensity,
              S.high_light_intensity, S.currProtocolIndex, 0,
              Teensy3Clock.get(), S.extra_TimeOut, S.InterBlockIntervalMs);
      Serial.println(outBuffer);
      SendProtocolProgress2PC();
      break;
    case 'H': // 'S'pecical msg: 'H'andshake
      Serial.println("SH");
      smart.ManualTimeAlignment(); // 电磁脉冲标记
      break;
    case 'Z': // Protocol manual change
    {
      int protocol_index = 0;
      if (parseOneInt(packetBuffer, protocol_index)) {
        protocol_index = constrain(protocol_index, 0, 10);
        BlockSwitchMode = 0;
        BlockSwitchStep = 0;
        BlockSwitchSecondRule = 1;
        PendingProtocolIndex = 255;
        TrialBlockReadyCuePlayed = 0;
        LastTrialBlockTriggerMs = 0;
        setTrialBlockAvailabilityLights(0);
        switchToProtocol(protocol_index);
        trialSelection();
        write_SD_para_S();
        SendProtocolProgress2PC();
        free_reward(S.reward_left);
      }
      break;
    }
    case 'B': // Enter BlockSwitchMode now
      BlockSwitchMode = 0;
      BlockSwitchStep = 0;
      BlockSwitchSecondRule = 1;
      PendingProtocolIndex = 255;
      SendProtocolProgress2PC();
      Serial.println("SB0");
      break;
    case 'N':
      SendProtocolProgress2PC();
      break;
    case 'E': // Extra timeout reciever
    {
      int extra_timeout = 0;
      if (parseOneInt(packetBuffer, extra_timeout)) {
        S.extra_TimeOut = constrain(extra_timeout, 0, 30000);
        write_SD_para_S();
      }
      break;
    }
    case 'I': // Inter-block interval receiver (ms)
    {
      int inter_block_interval_ms = 0;
      if (parseOneInt(packetBuffer, inter_block_interval_ms)) {
        S.InterBlockIntervalMs =
            constrain(inter_block_interval_ms, 0, 12 * 60 * 60 * 1000);
        write_SD_para_S();
      }
      break;
    }
    case 'C':
      CapSaveRequested = 1;
      break;
    case 'F':
      smart.SendSoftEvent(1);
      Serial.println("SF1");
      break;
    case 'G': // enter post-training protocol0
      BlockSwitchMode = 0;
      BlockSwitchStep = 0;
      BlockSwitchSecondRule = 1;
      PendingProtocolIndex = 255;
      RuleSwitchHintActive = 0;
      RuleSwitchCorrectCount = 0;
      PreviousRule = 0;
      CurrentELPunishEnabled = 0;
      EL_Favor = 1;
      TrialBlockOnset = 1;
      TrialBlockFreeRewardPending = 1;
      TrialBlockReadyCuePlayed = 0;
      LastTrialBlockTriggerMs = 0;
      setTrialBlockAvailabilityLights(0);
      switchToProtocol(0);
      RuleSwitchHintActive = 0;
      RuleSwitchCorrectCount = 0;
      PreviousRule = 0;
      CurrentProtocolFirstTrialUnix = 0;
      CurrentRuleFirstTrialUnix = 0;
      trialSelection();
      write_SD_para_S();
      SendProtocolProgress2PC();
      free_reward(S.reward_left);
      break;
    case 'W': {
      int warning_duration_ms = 0;
      int fan_enabled = 1;
      bool parsed = parseTwoInts(packetBuffer, warning_duration_ms, fan_enabled);
      if (!parsed) {
        parsed = parseOneInt(packetBuffer, warning_duration_ms);
      }
      if (parsed) {
        warning_duration_ms = constrain(warning_duration_ms, 0, 120000);
        startChargingWarning((unsigned long)warning_duration_ms,
                             fan_enabled > 0 ? 1 : 0);
      }
      break;
    }
    case 'D': // Digital input gate for trial trigger: D0 pause trigger, D1
              // resume trigger. Low-battery handling now stays in Mode0
              // training on the host side; no special firmware protocol is used.
    {
      int gate_enabled = 0;
      if (parseOneInt(packetBuffer, gate_enabled)) {
        TrialTriggerInputGateRequested = gate_enabled > 0 ? 1 : 0;
        TrialTriggerInputGateApplyPending = 1;
        processTrialTriggerInputGateRequest();
        sprintf(outBuffer, "SDP%d", TrialTriggerInputGateRequested);
        Serial.println(outBuffer);
      } else {
        Serial.println("E: Invalid D command.");
      }
      break;
    }
    case 'U': // Upload all SD files
      if (paused == 1) {
        upload_all_sd_files();
      } else {
        Serial.println("E: Must pause before upload.");
      }
      break;
    default: // never happen...
      Serial.println("E: Serial received some unusual packets!");
      break;
    }
  }

} // end of loop()

/****************************************************************************************************/
/********************************************** Functions
 * *******************************************/
/****************************************************************************************************/

int exponent(float beta, float min, float max) {
  float u;
  float x;
  int period;
  for (;;) {
    u = float(float(random(0, 1000)) / float(1000));
    x = -beta * log(u);
    if (min < x && x < max) {
      period = int(x * 1000);
      return period;
    }
  }
}

void construct_matrix_and_Run() {
  /* Final trial structure
   *  Self-trigger(250 ms) -> TrialStart(10 ms) -> SampleDelay(250 ms) ->
   * SampleCue(<=1000 ms) -> TrialEnd
   */
  byte trialEndLightIntensity =
      S.low_light_intensity > 3 ? S.low_light_intensity / 3 : 1;
  OutputAction LeftLightOutput = {
      "PWM1",
      S.low_light_intensity}; // left light, value 0-255,TODO just 1 ,the light
                              // intensity is unaffordable(anlogWrite()command)
  OutputAction RightLightOutput = {
      "PWM5", S.low_light_intensity}; // right light ,same as above
  OutputAction LeftLightOutputDim = {"PWM1", trialEndLightIntensity};
  OutputAction RightLightOutputDim = {"PWM5", trialEndLightIntensity};
  OutputAction LeftLightOutputH = {"PWM1", S.high_light_intensity};
  OutputAction RightLightOutputH = {"PWM5", S.high_light_intensity};

  OutputAction GreenLightOutputBackground = {"PWM3",
                                             10}; // Green light Background...

  /*******************************************************/

  smart.EmptyMatrix(); // clear matrix at the begining of each trial

  switch (S.currProtocolIndex) { // no earlylick punishment
  // Habitation 学习触发 中间水嘴来开启一个 trial free reward(100% 有free
  // reward：随机出现在左右); sample period 最大1000ms，前250ms
  // 如果小鼠舔舐会被视为EL(只在retention 阶段和最后的block
  // switch阶段会立即结束trial并附有200ms的noise，没有timeout，其余阶段这段时间的舔舐会被忽略)
  // 5 steps (100%~20% free reward,30 reward value 学习最终250ms的中间触发
  // S.PreCueDelayPeriod 44 ms；
  case 0: // 50 ms
  case 1: // 100 ms
  case 2: // 150 ms
  case 3: // 200 ms
  // Pre-Ex:  S.PreCueDelayPeriod 44 ms
  case 4: // 250 ms
  {
    bool useNonInfoSample = (S.currProtocolIndex <= 4);
    bool isFreeWaterProtocol = (S.currProtocolIndex == 0);
    unsigned int free_drop_duration = S.reward_middle;
    if (S.currProtocolIndex == 0) {
      S.reward_middle = S.reward_left; // legacy protocol0 behavior
      free_drop_duration = S.reward_middle;
    }

    float reward_dur = S.reward_left;
    OutputAction SampleOutput; // top buzzer
    OutputAction RewardOutput;

    String LeftLickAction;
    String RightLickAction;
    String LeftLickActionAnswer;
    String RightLickActionAnswer;
    String ActionAfterDelay;
    String TrialBeignState;
    String TrialBlockOnsetEL;
    String CapInitReturn;

    if (useNonInfoSample) {
      SampleOutput = CueOutput;
    } else if (currStimu[0] == -1) { // left orientation
      // 根据 currstimu 来决定 sample output stimu1 为 freq low -1 high 1 ；
      // stimu2 spatial left -1 right 1
      switch (int(currStimu[1] * 2)) {
      case -2: // left low
        SampleOutput = LeftLowSoundOutput;
        break;
      case -1: // left low mid
        SampleOutput = LeftLowMidSoundOutput;
        break;
      case 0: // left mid
        SampleOutput = LeftCueSoundOutput;
        break;
      case 1: // left high mid
        SampleOutput = LeftHighMidSoundOutput;
        break;
      case 2: // left high
        SampleOutput = LeftHighSoundOutput;
        break;
      default:
        break;
      }
    } else if (currStimu[0] == 1) { // right orientation
      switch (int(currStimu[1] * 2)) {
      case -2: // right low
        SampleOutput = RightLowSoundOutput;
        break;
      case -1: // right low mid
        SampleOutput = RightLowMidSoundOutput;
        break;
      case 0: // right mid
        SampleOutput = RightCueSoundOutput;
        break;
      case 1: // right high mid
        SampleOutput = RightHighMidSoundOutput;
        break;
      case 2: // right high
        SampleOutput = RightHighSoundOutput;
        break;
      default:
        break;
      }
    }

    switch (TrialType) {
    case 1: // left choice ; 3khz
    {
      LeftLickAction = "Reward";
      LeftLickActionAnswer = "Reward";

      RightLickAction = "SampleCue";
      RightLickActionAnswer = "AnswerPeriod";

      RewardOutput = LeftWaterOutput;
      reward_dur = S.reward_left;
    } break;
    case 2: // right choice ; 12khz
    {
      LeftLickAction = "SampleCue";
      LeftLickActionAnswer = "AnswerPeriod";

      RightLickAction = "Reward";
      RightLickActionAnswer = "Reward";

      RewardOutput = RightWaterOutput;
      reward_dur = S.reward_right;
    } break;
    }

    bool isTrialBlockOnsetTrial = (TrialBlockOnset == 1);
    if (isTrialBlockOnsetTrial) {
      TrialBlockFreeRewardPending = 1;
    }
    if (isFreeWaterProtocol) {
      ActionAfterDelay = "GiveFreeDrop";
    } else if (TrialBlockFreeRewardPending) {
      ActionAfterDelay = "GiveFreeDrop";
    } else if (int(random(100)) < (100 - 20 * S.currProtocolIndex)) {
      ActionAfterDelay = "GiveFreeDrop";
    } else {
      ActionAfterDelay = "SampleDelay";
    }

    // for time alignment
    if (isTrialBlockOnsetTrial) {
      TrialBeignState = "NeuralReader";
      CapInitReturn = "CapDisableBeforeTrialBlockEnd";
      TrialBlockOnset = 0;
    } else { // within one trial block
      TrialBeignState = "TrialStart";
      CapInitReturn = "Fixed_ITI";
    }
    TrialBlockOnsetEL = "ReturnInitFixation";

    /*******************************************************State Machine
     * HABITS******************************************************************
     */
    StateTransition TrialBlockDelay_Cond[2] = {
        {"Tup", TrialBeignState}, {"DI1Rising", TrialBlockOnsetEL}};
    StateTransition ReturnInitFixation_Cond[2] = {
        {"Tup", CapInitReturn}, {"DI1Falling", "TrialBlockDelay"}};
    // If the PC never confirms the wireless charging state for this block,
    // invalidate the block instead of continuing training blindly.
    StateTransition NeuralReader_Cond[2] = {{"SoftEvent1", "CapReinit"},
                                            {"Tup", "TrialBlockEnd"}};
    StateTransition CapReinit_Cond[1] = {{"Tup", "TrialStart"}};
    StateTransition TrialStart_Cond[1] = {{"Tup", "PreCueDelayPeriod"}};
    StateTransition PreCueDelayPeriod_Cond[1] = {{"Tup", ActionAfterDelay}};
    StateTransition EarlyLickAborted_Cond[1] = {{"Tup", "TimeOutEL"}};
    StateTransition SampleDelay_Cond[1] = {{"Tup", "SampleCue"}};
    StateTransition TrialEnd_Cond[2] = {
        {"Tup", "CapDisableBeforeTrialBlockEnd"}, {"DI1Falling", "exit"}};
    StateTransition GiveFreeDrop_Cond[1] = {{"Tup", "SampleDelay"}};
    StateTransition AnswerPeriod_Cond[3] = {{"Lick1In", LeftLickActionAnswer},
                                            {"Lick2In", RightLickActionAnswer},
                                            {"Tup", "NoResponse"}};
    StateTransition Reward_Cond[1] = {{"Tup", "RewardConsumption"}};
    StateTransition Tup_Exit_Cond[1] = {{"Tup", "Fixed_ITI"}};
    StateTransition ErrorTrial_Cond[1] = {{"Tup", "exit"}};
    StateTransition Fixed_ITI_Return_Cond[1] = {{"Tup", "Fixed_ITI"}};
    StateTransition SampleCue_Cond[3] = {{"Lick1In", LeftLickAction},
                                         {"Lick2In", RightLickAction},
                                         {"Tup", "AnswerPeriod"}};
    StateTransition Cue_Cond[1] = {{"Tup", "NoResponse"}};
    StateTransition NoResponse_Cond[1] = {{"Tup", "TrialEnd"}};
    StateTransition CapDisableBeforeTrialBlockEnd_Cond[1] = {
        {"Tup", "TrialBlockEnd"}};
    StateTransition TrialBlockEnd_Cond[2] = {{"Tup", "exit"},
                                             {"DI1Falling", "exit"}};
    StateTransition Fixed_ITI_Cond[3] = {{"Tup", "TrialEnd"},
                                         {"Lick1In", "Fixed_ITI_Return"},
                                         {"Lick2In", "Fixed_ITI_Return"}};
    // StateTransition CapReader_Cond[1] = {{"Tup", "TrialBlockEnd"}};

    CurrentELPunishEnabled = 0;
    OutputAction NeuralReader_Output[3] = {SpikeRecordingOutput,
                                           LeftLightOutput, RightLightOutput};
    OutputAction CapReinit_Output[3] = {CapReinitOutput, LeftLightOutput,
                                        RightLightOutput};
    OutputAction TrialStart_Output[1] = {TimeAlignmentOutput};
    OutputAction TrialStartWithLed_Output[3] = {
        TimeAlignmentOutput, LeftLightOutput, RightLightOutput};
    OutputAction PreSampleLed_Output[3] = {TimeAlignmentDisableOutput,
                                           LeftLightOutput, RightLightOutput};
    OutputAction DoubleLed_Output[2] = {LeftLightOutput, RightLightOutput};
    OutputAction TrialBlockDelayWithTelemetry_Output[2] = {LeftLightOutput,
                                                           RightLightOutput};
    OutputAction TrialBlockDelayHint_Output[3] = {CueOutput, LeftLightOutput,
                                                  RightLightOutput};
    OutputAction TrialBlockDelayHintWithTelemetry_Output[3] = {
        CueOutput, LeftLightOutput, RightLightOutput};
    OutputAction Sample_Output[1] = {SampleOutput};
    OutputAction SampleDelay_Output[3] = {SampleOutput, LeftLightOutput,
                                          RightLightOutput};
    OutputAction FreeRewardWithSample_Output[4] = {
        RewardOutput, SampleOutput, LeftLightOutput, RightLightOutput};
    OutputAction Reward_Output[1] = {RewardOutput};
    OutputAction FreeReward_Output[1] = {RewardOutput};
    OutputAction NoOutput[0] = {};
    OutputAction ErrorOutput[2] = {NoiseOutput, Inavailable_Output};
    OutputAction EarlyLickPunish_Output[4] = {
        NoiseOutput, Inavailable_Output, LeftLightOutputH, RightLightOutputH};
    OutputAction EndCue_Output[1] = {CueOutput};
    OutputAction TrialEnd_Output[3] = {smartFinish_Output, LeftLightOutputDim,
                                       RightLightOutputDim};
    OutputAction CapDisableBeforeTrialBlockEnd_Output[1] = {CapDisableOutput};
    OutputAction TrialBlockEnd_Output[2] = {Inactive_Output,
                                            LFPRecordingOutput};
    OutputAction TimeOut_Output[1] = {Inavailable_Output};
    // OutputAction CapReader_Output[1] = {{"Flag2", 2}};

    bool playRuleSwitchHintCue =
        (RuleSwitchHintActive == 1 && BlockSwitchMode == 0);
    int sampleCuePeriod = min(S.SamplePeriod, 1000);
    gpSMART_State states[22] = {};
    // visual flash states and conditions
    states[0] = smart.CreateState(
        "TrialBlockDelay", S.TrialBlockInitPeriod, 2, TrialBlockDelay_Cond,
        playRuleSwitchHintCue ? 3 : 2,
        playRuleSwitchHintCue
            ? TrialBlockDelayHintWithTelemetry_Output
            : TrialBlockDelayWithTelemetry_Output); // remove trial initiation
                                                    // without engagement
    states[20] = smart.CreateState(
        "ReturnInitFixation", 5000, 2, ReturnInitFixation_Cond, 2,
        DoubleLed_Output); // avoid mis-detection of fixed action or give mice
                           // the second chance to re-fix.
    states[18] = smart.CreateState("NeuralReader", 5000, 2, NeuralReader_Cond,
                                   3, NeuralReader_Output);
    states[19] = smart.CreateState("CapReinit", 50, 1, CapReinit_Cond, 3,
                                   CapReinit_Output);

    states[15] = smart.CreateState(
        "TrialStart", TrialStartStateDurationMs, 1, TrialStart_Cond, 3,
        TrialStartWithLed_Output); // for time alignment marker playback
    states[3] = smart.CreateState(
        "PreCueDelayPeriod", S.PreCueDelayPeriod, 1, PreCueDelayPeriod_Cond, 3,
        PreSampleLed_Output); // for islating the noised alignment signal
    states[11] =
        smart.CreateState("EarlyLickAborted", 200, 1, EarlyLickAborted_Cond, 4,
                          EarlyLickPunish_Output); // not used
    states[13] = smart.CreateState("SampleDelay", 250, 1, SampleDelay_Cond, 3,
                                   SampleDelay_Output);
    states[1] = smart.CreateState("SampleCue", sampleCuePeriod, 3,
                                  SampleCue_Cond, 1, Sample_Output);
    states[14] = smart.CreateState("Cue", 100, 1, Cue_Cond, 1,
                                   EndCue_Output); // not used
    states[4] =
        smart.CreateState("GiveFreeDrop", free_drop_duration, 1, GiveFreeDrop_Cond,
                          4, FreeRewardWithSample_Output);
    states[5] = smart.CreateState("AnswerPeriod", S.AnswerPeriod, 3,
                                  AnswerPeriod_Cond, 1, TimeOut_Output);
    states[6] = smart.CreateState("Reward", reward_dur, 1, Reward_Cond, 1,
                                  Reward_Output);
    states[7] = smart.CreateState("RewardConsumption", S.ConsumptionPeriod, 1,
                                  Tup_Exit_Cond, 1, TimeOut_Output);
    states[8] = smart.CreateState("NoResponse", 10, 1, NoResponse_Cond, 0,
                                  NoOutput); // enter to the Trial end
    states[9] = smart.CreateState("ErrorTrial", 500, 1, ErrorTrial_Cond, 2,
                                  ErrorOutput); // not used
    states[10] =
        smart.CreateState("TimeOutEL", exponent(3, 2, 4), 1, Tup_Exit_Cond, 1,
                          TimeOut_Output); // not used
    states[17] =
        smart.CreateState("Fixed_ITI", 1000, 3, Fixed_ITI_Cond, 1,
                          TimeOut_Output); // Fixed 1000 ms ITI without licking
    states[12] = smart.CreateState("Fixed_ITI_Return", 100, 1,
                                   Fixed_ITI_Return_Cond, 1, TimeOut_Output);
    states[2] =
        smart.CreateState("TrialEnd", 30000, 2, TrialEnd_Cond, 3,
                          TrialEnd_Output); // waiting next self-initated trial

    states[21] = smart.CreateState("CapDisableBeforeTrialBlockEnd", 100, 1,
                                   CapDisableBeforeTrialBlockEnd_Cond, 1,
                                   CapDisableBeforeTrialBlockEnd_Output);
    states[16] = smart.CreateState(
        "TrialBlockEnd", 12 * 60 * 60 * 1000, 2, TrialBlockEnd_Cond, 2,
        TrialBlockEnd_Output); // TrialBlock end + Serial output for Mode1 open

    // Predefine State sequence.
    for (int i = 0; i < 22; i++) {
      smart.AddBlankState(states[i].Name);
    }

    // Add a state to state machine.
    for (int i = 0; i < 22; i++) {
      smart.AddState(&states[i]);
    }
    // smart.PrintMatrix(); // for debug
    // Run the matrix
    smart.Run();
  } break;

  // Loc
  case 5:
  case 6:
  // Freq
  case 7: // freq
  case 8: // freq retention
  // reversal freq
  case 9:  // reversal freq
  case 10: // reversal retention random trial
  {
    OutputAction SampleOutput;
    OutputAction RewardOutput;

    String LeftLickAction;
    String RightLickAction;
    String TrialBeignState;
    String TrialBlockOnsetEL;
    String ActionAfterDelay;
    String CapInitReturn;

    float reward_dur = S.reward_left;

    // 根据 currstimu 来决定 sample output stimu1 为 freq low -1 high 1 ；
    // stimu2 spatial left -1 right 1
    if (currStimu[0] == -1) { // left orientation
      switch (int(currStimu[1] * 2)) {
      case -2: // left low
        SampleOutput = LeftLowSoundOutput;
        break;
      case -1: // left low mid
        SampleOutput = LeftLowMidSoundOutput;
        break;
      case 0: // left mid
        SampleOutput = LeftCueSoundOutput;
        break;
      case 1: // left high mid
        SampleOutput = LeftHighMidSoundOutput;
        break;
      case 2: // left high
        SampleOutput = LeftHighSoundOutput;
        break;
      default:
        break;
      }
    } else if (currStimu[0] == 1) { // right orientation
      switch (int(currStimu[1] * 2)) {
      case -2: // right low
        SampleOutput = RightLowSoundOutput;
        break;
      case -1: // right low mid
        SampleOutput = RightLowMidSoundOutput;
        break;
      case 0: // right mid
        SampleOutput = RightCueSoundOutput;
        break;
      case 1: // right high mid
        SampleOutput = RightHighMidSoundOutput;
        break;
      case 2: // right high
        SampleOutput = RightHighSoundOutput;
        break;
      default:
        break;
      }
    }

    switch (TrialType) {
    case 1: // left choice ; 3khz
    {
      LeftLickAction = "Reward";
      RightLickAction = "ErrorTrial";

      RewardOutput = LeftWaterOutput;
      reward_dur = S.reward_left;
    } break;
    case 2: // right choice ; 12khz
    {
      LeftLickAction = "ErrorTrial";
      RightLickAction = "Reward";

      RewardOutput = RightWaterOutput;
      reward_dur = S.reward_right;
    } break;
    }

    BaselineTrialFlag = 0;
    // for time alignment
    bool isTrialBlockOnsetTrial = (TrialBlockOnset == 1);
    if (isTrialBlockOnsetTrial) {
      TrialBlockFreeRewardPending = 1;
    }
    if (isTrialBlockOnsetTrial) {
      TrialBeignState = "NeuralReader";
      CapInitReturn = "CapDisableBeforeTrialBlockEnd";
      TrialBlockOnset = 0;
    } else { // within one trial block
      TrialBeignState = "TrialStart";
      CapInitReturn = "Fixed_ITI";
    }
    TrialBlockOnsetEL = "ReturnInitFixation";
    bool enforceEarlyLickPunish =
        ((S.currProtocolIndex == 6 || S.currProtocolIndex == 8 ||
          S.currProtocolIndex == 10) ||
         BlockSwitchMode == 1);
    bool elFavorOverride = (EL_Favor == 0);
    bool effectiveEnforceEarlyLickPunish =
        (enforceEarlyLickPunish && !elFavorOverride);
    bool allowGiveFreeDrop =
        (BlockSwitchMode == 0 &&
         (S.currProtocolIndex == 5 || S.currProtocolIndex == 7 ||
          S.currProtocolIndex == 9));
    if (allowGiveFreeDrop && TrialBlockFreeRewardPending) {
      ActionAfterDelay = "GiveFreeDrop";
    } else if (allowGiveFreeDrop &&
               ((TrialType == 1 && S.GaveFreeReward.flag_L_water == 1) ||
                (TrialType == 2 && S.GaveFreeReward.flag_R_water == 1))) {
      ActionAfterDelay = "GiveFreeDrop";
      if (TrialType == 1) {
        S.GaveFreeReward.flag_L_water = 0;
      } else {
        S.GaveFreeReward.flag_R_water = 0;
      }
    } else {
      ActionAfterDelay = "SampleDelay";
    }
    if (RuleSwitchHintActive == 1 &&
        RuleSwitchCorrectCount < RuleSwitchHintCorrectTrialLimit &&
        BlockSwitchMode == 0) {
      reward_dur = min(reward_dur * 2.0f, 200.0f);
    }
    StateTransition TrialBlockDelay_Cond[2] = {
        {"Tup", TrialBeignState}, {"DI1Rising", TrialBlockOnsetEL}};
    StateTransition ReturnInitFixation_Cond[2] = {
        {"Tup", CapInitReturn}, {"DI1Falling", "TrialBlockDelay"}};
    // If the PC never confirms the wireless charging state for this block,
    // invalidate the block instead of continuing training blindly.
    StateTransition NeuralReader_Cond[2] = {{"SoftEvent1", "CapReinit"},
                                            {"Tup", "TrialBlockEnd"}};
    StateTransition CapReinit_Cond[1] = {{"Tup", "TrialStart"}};
    StateTransition PreCueDelayPeriod_Cond[1] = {{"Tup", ActionAfterDelay}};
    StateTransition TrialStart_Cond[1] = {{"Tup", "PreCueDelayPeriod"}};
    StateTransition SampleDelay_Cond[1] = {{"Tup", "SampleCue"}};
    StateTransition TrialEnd_Cond[2] = {
        {"Tup", "CapDisableBeforeTrialBlockEnd"}, {"DI1Falling", "exit"}};
    StateTransition GiveFreeDrop_Cond[1] = {{"Tup", "SampleDelay"}};
    StateTransition SampleCue_Cond[3] = {{"Lick1In", LeftLickAction},
                                         {"Lick2In", RightLickAction},
                                         {"Tup", "AnswerPeriod"}};
    StateTransition AnswerPeriod_Cond[3] = {
        {"Lick1In", LeftLickAction},
        {"Lick2In", RightLickAction},
        {"Tup", "NoResponse"}}; // 这里因为cap的损坏改为： left action
    // 为 触发一下光电； right 为不触发
    StateTransition Reward_Cond[1] = {{"Tup", "RewardConsumption"}};
    StateTransition Tup_Exit_Cond[1] = {{"Tup", "Fixed_ITI"}};
    StateTransition ErrorTrial_Cond[1] = {{"Tup", "TimeOut"}};
    StateTransition Fixed_ITI_Return_Cond[1] = {{"Tup", "Fixed_ITI"}};
    StateTransition ErrorTest_Cond[1] = {{"Tup", "AnswerPeriod"}};
    StateTransition Cue_Cond[1] = {{"Tup", "AnswerPeriod"}};
    StateTransition NoResponse_Cond[1] = {{"Tup", "TrialEnd"}};
    StateTransition CapDisableBeforeTrialBlockEnd_Cond[1] = {
        {"Tup", "TrialBlockEnd"}};
    StateTransition TrialBlockEnd_Cond[2] = {{"Tup", "exit"},
                                             {"DI1Falling", "exit"}};
    StateTransition Fixed_ITI_Cond[3] = {{"Tup", "TrialEnd"},
                                         {"Lick1In", "Fixed_ITI_Return"},
                                         {"Lick2In", "Fixed_ITI_Return"}};

    StateTransition EarlyLickAborted_Cond[1] = {{"Tup", "TrialEnd"}};
    StateTransition Tup_StopLicking_Cond[1] = {{"Tup", "StopLicking"}};
    StateTransition StopLicking_Cond[3] = {{"Lick1In", "StopLickingReturn"},
                                           {"Lick2In", "StopLickingReturn"},
                                           {"Tup", "Fixed_ITI"}};

    CurrentELPunishEnabled = effectiveEnforceEarlyLickPunish ? 1 : 0;
    OutputAction NeuralReader_Output[3] = {SpikeRecordingOutput,
                                           LeftLightOutput, RightLightOutput};
    OutputAction CapReinit_Output[3] = {CapReinitOutput, LeftLightOutput,
                                        RightLightOutput};
    OutputAction TrialStartWithLed_Output[3] = {
        TimeAlignmentOutput, LeftLightOutput, RightLightOutput};
    OutputAction PreSampleLed_Output[3] = {TimeAlignmentDisableOutput,
                                           LeftLightOutput, RightLightOutput};
    OutputAction DoubleLed_Output[2] = {LeftLightOutput, RightLightOutput};
    OutputAction TrialBlockDelayWithTelemetry_Output[2] = {LeftLightOutput,
                                                           RightLightOutput};
    OutputAction TrialBlockDelayHint_Output[3] = {CueOutput, LeftLightOutput,
                                                  RightLightOutput};
    OutputAction TrialBlockDelayHintWithTelemetry_Output[3] = {
        CueOutput, LeftLightOutput, RightLightOutput};
    OutputAction Sample_Output[1] = {SampleOutput};
    OutputAction SampleDelay_Output[3] = {SampleOutput, LeftLightOutput,
                                          RightLightOutput};
    OutputAction FreeRewardWithSample_Output[4] = {
        RewardOutput, SampleOutput, LeftLightOutput, RightLightOutput};
    OutputAction Reward_Output[1] = {RewardOutput};
    OutputAction FreeReward_Output[1] = {RewardOutput};
    OutputAction NoOutput[0] = {};
    OutputAction ErrorOutput[2] = {NoiseOutput, Inavailable_Output};
    OutputAction EarlyLickPunish_Output[4] = {
        NoiseOutput, Inavailable_Output, LeftLightOutputH, RightLightOutputH};
    OutputAction EndCue_Output[1] = {CueOutput};
    OutputAction TrialStart_Output[1] = {TimeAlignmentOutput};
    OutputAction TrialEnd_Output[3] = {smartFinish_Output, LeftLightOutputDim,
                                       RightLightOutputDim};
    OutputAction CapDisableBeforeTrialBlockEnd_Output[1] = {CapDisableOutput};
    OutputAction TrialBlockEnd_Output[2] = {Inactive_Output,
                                            LFPRecordingOutput};
    OutputAction TimeOut_Output[1] = {Inavailable_Output};

    bool playRuleSwitchHintCue =
        (RuleSwitchHintActive == 1 && BlockSwitchMode == 0);
    int sampleCuePeriod = min(S.SamplePeriod, 1000);
    gpSMART_State states[26] = {};
    // visual flash states and conditions
    states[0] = smart.CreateState(
        "TrialBlockDelay", S.TrialBlockInitPeriod, 2, TrialBlockDelay_Cond,
        playRuleSwitchHintCue ? 3 : 2,
        playRuleSwitchHintCue ? TrialBlockDelayHintWithTelemetry_Output
                              : TrialBlockDelayWithTelemetry_Output); //
    states[25] = smart.CreateState(
        "ReturnInitFixation", 5000, 2, ReturnInitFixation_Cond, 2,
        DoubleLed_Output); // avoid mis-detection of fixed action or give mice
                           // the second chance to re-fix.
    states[23] = smart.CreateState("NeuralReader", 5000, 2, NeuralReader_Cond,
                                   3, NeuralReader_Output);
    states[24] = smart.CreateState("CapReinit", 50, 1, CapReinit_Cond, 3,
                                   CapReinit_Output);

    states[20] =
        smart.CreateState("TrialStart", TrialStartStateDurationMs, 1,
                          TrialStart_Cond, 3, TrialStartWithLed_Output); // msec
    states[3] =
        smart.CreateState("PreCueDelayPeriod", S.PreCueDelayPeriod,
                          effectiveEnforceEarlyLickPunish ? 1 : 1,
                          PreCueDelayPeriod_Cond, 3, PreSampleLed_Output);
    states[11] =
        smart.CreateState("EarlyLickAborted", 200, 1, EarlyLickAborted_Cond, 4,
                          EarlyLickPunish_Output);
    states[16] = smart.CreateState("SampleCue", sampleCuePeriod, 3,
                                   SampleCue_Cond, 1, Sample_Output); // 500ms
    states[1] = smart.CreateState("SampleDelay", 250,
                                  effectiveEnforceEarlyLickPunish ? 1 : 1,
                                  SampleDelay_Cond, 3, SampleDelay_Output);
    states[18] = smart.CreateState("Cue", 100, 1, Cue_Cond, 1, EndCue_Output);
    states[4] =
        smart.CreateState("GiveFreeDrop", S.reward_middle, 1, GiveFreeDrop_Cond,
                          4, FreeRewardWithSample_Output); // not used
    states[5] = smart.CreateState("AnswerPeriod", S.AnswerPeriod, 3,
                                  AnswerPeriod_Cond, 1, TimeOut_Output);
    states[6] = smart.CreateState("Reward", reward_dur, 1, Reward_Cond, 1,
                                  Reward_Output);
    states[7] = smart.CreateState("RewardConsumption", S.ConsumptionPeriod, 1,
                                  Tup_StopLicking_Cond, 1, TimeOut_Output);
    states[14] = smart.CreateState("StopLicking", S.StopLickingPeriod, 3,
                                   StopLicking_Cond, 1, TimeOut_Output);
    states[15] = smart.CreateState("StopLickingReturn", 100, 1,
                                   Tup_StopLicking_Cond, 1, TimeOut_Output);
    states[8] =
        smart.CreateState("NoResponse", 10, 1, NoResponse_Cond, 0, NoOutput);
    states[9] = smart.CreateState("ErrorTrial", 500, 1, ErrorTrial_Cond, 2,
                                  ErrorOutput);
    states[17] = smart.CreateState("ErrorTest", 10, 1, ErrorTest_Cond, 0,
                                   NoOutput); // not used
    states[13] = smart.CreateState("TimeOut", S.TimeOut + S.extra_TimeOut, 1,
                                   Tup_Exit_Cond, 1, TimeOut_Output);
    states[10] =
        smart.CreateState("TimeOutEL", exponent(int(S.TimeOut / 1000), 2, 9), 1,
                          Tup_Exit_Cond, 1, TimeOut_Output);
    states[22] =
        smart.CreateState("Fixed_ITI", 1000, 3, Fixed_ITI_Cond, 1,
                          TimeOut_Output); // Fixed 1000 ms ITI without licking
    states[12] = smart.CreateState("Fixed_ITI_Return", 100, 1,
                                   Fixed_ITI_Return_Cond, 1, TimeOut_Output);
    states[2] = smart.CreateState("TrialEnd", 30000, 2, TrialEnd_Cond, 3,
                                  TrialEnd_Output);

    states[19] = smart.CreateState("CapDisableBeforeTrialBlockEnd", 100, 1,
                                   CapDisableBeforeTrialBlockEnd_Cond, 1,
                                   CapDisableBeforeTrialBlockEnd_Output);
    states[21] = smart.CreateState(
        "TrialBlockEnd", 12 * 60 * 60 * 1000, 2, TrialBlockEnd_Cond, 2,
        TrialBlockEnd_Output); // TrialBlock end + Serial output for Mode1 open
    // Predefine State sequence.
    for (int i = 0; i < 26; i++) {
      smart.AddBlankState(states[i].Name);
    }

    // Add a state to state machine.
    for (int i = 0; i < 26; i++) {
      smart.AddState(&states[i]);
    }
    // smart.PrintMatrix(); // for debug
    // Run the matrix
    smart.Run();
  } break;
  default:
    break;
  } // end for Switch(protocol)
}

void UpdateTrialOutcome() {
  /* data will be stored in public variable 'trial_res', which includes:
    trial_res.nEvent:           number of event happened in last trial
    trial_res.eventTimeStamps[]: time stamps for each event
    trial_res.EventID[]:          event id for each event
    trial_res.nVisited:       number of states visited in last trial
    trial_res.stateVisited[]:   the states visited in last trail
  */

  TrialOutcome = 4; // 0 no-response; 1 correct; 2 error; 3 el; 4 others
  int ErrorState = 9;

  for (int i = 0; i < trial_res.nVisited; i++) {
    if (trial_res.stateVisited[i] == 6 || trial_res.stateVisited[i] == 8 ||
        trial_res.stateVisited[i] ==
            ErrorState) { // Reward || No Response || Error

      if (trial_res.stateVisited[i] == 6) {
        TrialOutcome = 1;
      } else if (trial_res.stateVisited[i] == 8) {
        TrialOutcome = 0;
      } else {
        TrialOutcome = 2;
      }
      break;
    }
  }

  if (TrialOutcome == 1) {
    if (!isTrialTriggerFreeWaterActive()) {
      TrialBlockFreeRewardPending = 0;
    }
    S.totalRewardNum++;
    last_reward_time = millis(); // record the last reward time
    timed_reward_count = 0;
    if (!isTrialTriggerFreeWaterActive() && RuleSwitchHintActive == 1) {
      RuleSwitchCorrectCount++;
      if (RuleSwitchCorrectCount >= RuleSwitchHintCorrectTrialLimit) {
        RuleSwitchHintActive = 0;
        RuleSwitchCorrectCount = 0;
      }
    }
  }

  is_earlylick = 2;
  if (S.currProtocolIndex >= 0) {
    is_earlylick = 0;
    for (int i = 0; i < trial_res.nVisited; i++) {
      if (trial_res.stateVisited[i] == 11) { // earlylick delay
        is_earlylick = 1;
        TrialOutcome = 3;
        break;
      }
    }
  }

  // do FIFO
  for (int i = 0; i < RECORD_TRIALS - 1; i++) {
    S.ProtocolIndexHistory[i] = S.ProtocolIndexHistory[i + 1];
    S.TrialTypeHistory[i] = S.TrialTypeHistory[i + 1]; //
    S.Stimu1History[i] = S.Stimu1History[i + 1];
    S.Stimu2History[i] = S.Stimu2History[i + 1];
    S.OutcomeHistory[i] = S.OutcomeHistory[i + 1]; //
    S.SampleTypeHistory[i] = S.SampleTypeHistory[i + 1];
    S.EarlyLickHistory[i] = S.EarlyLickHistory[i + 1]; //
  }
  // Keep record current trial info in the last position (RECORD_TRIALS-1) of
  // the matrix
  S.ProtocolIndexHistory[RECORD_TRIALS - 1] = S.currProtocolIndex;
  S.OutcomeHistory[RECORD_TRIALS - 1] = TrialOutcome;
  S.TrialTypeHistory[RECORD_TRIALS - 1] = TrialType;
  S.Stimu1History[RECORD_TRIALS - 1] =
      int((currStimu[0] * 2) + 2); // 0~4 -> -1 -> 1
  S.Stimu2History[RECORD_TRIALS - 1] = int((currStimu[1] * 2) + 2);
  S.EarlyLickHistory[RECORD_TRIALS - 1] = is_earlylick;
  S.SampleTypeHistory[RECORD_TRIALS - 1] = SampleType;

  // byte Outcomes_sum = 0;
  // byte protocol_trials_count = 0;
  // for (int i = RECORD_TRIALS - 1; i >= 0; i--)
  // {
  //   if (S.ProtocolIndexHistory[i] != S.currProtocolIndex)
  //   {
  //     continue;
  //   }
  //   if (S.OutcomeHistory[i] == 1)
  //   {
  //     Outcomes_sum++;
  //   }
  //   protocol_trials_count++;
  //   if (protocol_trials_count >= easy_perf_trials)
  //   {
  //     break;
  //   }
  // }

  // EMA S.currProtocolPerf ; alpha is 0.02 ; 50 trials average
  float CurrOutcome = 0;
  // recording the last valid trial
  if (!isTrialTriggerFreeWaterActive() &&
      S.OutcomeHistory[RECORD_TRIALS - 1] != 3 &&
      S.OutcomeHistory[RECORD_TRIALS - 1] != 0) {
    S.currProtocolTrials++;
    if (S.OutcomeHistory[RECORD_TRIALS - 1] == 1) {
      CurrOutcome = 1.0;
    } else if (S.OutcomeHistory[RECORD_TRIALS - 1] == 2) {
      CurrOutcome = 0.0;
    }
    S.currProtocolPerf =
        S.currProtocolPerf * Alpha500 + (1.0 - Alpha500) * CurrOutcome;
  }
  if (S.currProtocolTrials > 0) {
    currProtocolPerf_corrected =
        S.currProtocolPerf / (1.0 - pow(Alpha500, S.currProtocolTrials));
  } else {
    currProtocolPerf_corrected = 0;
  }
  Perf100 = 0;
  EarlyLick100 = 0;
  for (int i = 0; i < 100; i++) {
    if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
      continue;
    }
    if (S.OutcomeHistory[i] == 1) {
      Perf100++;
    }
    if (S.EarlyLickHistory[i] == 1) {
      EarlyLick100++;
    }
  }
}

void SendTrialInfo2PC() {
  int easy_perf100 = getEasyTrialPerf100();
  String TrialInfo_str = "T";

  TrialInfo_str += String(S.currTrialNum);
  TrialInfo_str += ",";

  TrialInfo_str += String(TrialType);
  TrialInfo_str += ",";

  TrialInfo_str += String(S.currProtocolIndex);
  TrialInfo_str += ",";

  TrialInfo_str += String(TrialOutcome);
  TrialInfo_str += ",";

  TrialInfo_str += String(EarlyLick100);
  TrialInfo_str += ",";

  TrialInfo_str += String(Perf100);
  TrialInfo_str += ",";

  TrialInfo_str += String(S.currProtocolTrials);
  TrialInfo_str += ",";

  TrialInfo_str += String(abs(easy_perf100));
  TrialInfo_str += ",";

  TrialInfo_str += String(S.rule);
  TrialInfo_str += ",";

  TrialInfo_str += String(currStimu[0], 2);
  TrialInfo_str += ",";

  TrialInfo_str += String(currStimu[1], 2);
  TrialInfo_str += ",";

  TrialInfo_str.toCharArray(outBuffer, sizeof(outBuffer));
  Serial.println(outBuffer);
}

void SendProtocolProgress2PC() {
  int left_easy_perf50 = 0;
  int right_easy_perf50 = 0;
  int easy_perf100 = 0;
  unsigned int left_easy_count = 0;
  unsigned int right_easy_count = 0;
  unsigned int easy100_count = 0;
  getEasyTrialStatsByType(1, easy_perf_side_trials, left_easy_perf50,
                          left_easy_count);
  getEasyTrialStatsByType(2, easy_perf_side_trials, right_easy_perf50,
                          right_easy_count);
  getEasyTrialStatsByType(0, easy_perf_trials, easy_perf100, easy100_count);

  bool easy_perf50_ready = (left_easy_count >= easy_perf_side_trials &&
                            right_easy_count >= easy_perf_side_trials &&
                            left_easy_perf50 >= EasyPerf50SwitchThreshold &&
                            right_easy_perf50 >= EasyPerf50SwitchThreshold);

  unsigned long protocol_elapsed_sec =
      getElapsedSinceSec(CurrentProtocolFirstTrialUnix);
  unsigned long rule_elapsed_sec =
      getElapsedSinceSec(CurrentRuleFirstTrialUnix);
  syncHostFlowIndexToProtocol(S.currProtocolIndex);
  applyCurrentHostFlowBlock(false);
  bool host_time_ready =
      (HostConditionTimeMinSec == 0 ||
       hasElapsedSince(CurrentProtocolFirstTrialUnix, HostConditionTimeMinSec));
  bool block_rule_time_ready = (rule_elapsed_sec >= BlockRuleMinDurationSec &&
                                CurrentRuleFirstTrialUnix > 0);

  byte next_protocol = 255;
  byte trial_op = 2; // 0 none, 1 >, 2 >=
  unsigned int trial_target = HostConditionTrialMin;
  bool trial_ready = (S.currProtocolTrials >= trial_target);
  bool pending_ready = false;
  bool auto_ready = isHostProtocolConditionReady();
  byte block_next_a = 255;
  byte block_next_b = 255;

  if (PendingProtocolIndex != 255) {
    next_protocol = PendingProtocolIndex;
    pending_ready = (TrialBlockOnset == 1);
    auto_ready = pending_ready;
  } else if (HostFlowLength > 0 && HostFlowCurrentIndex + 1 < HostFlowLength) {
    next_protocol = HostFlowProtocols[HostFlowCurrentIndex + 1];
  }

  String Progress_str = "PG,";
  Progress_str.reserve(1600);
  appendProgressField(Progress_str, "mode", String(0));
  appendProgressField(Progress_str, "curr", String(S.currProtocolIndex));
  appendProgressField(Progress_str, "next", String(next_protocol));
  appendProgressField(Progress_str, "pending", String(PendingProtocolIndex));
  appendProgressField(Progress_str, "trial_trigger_free_water",
                      String(0));
  appendProgressField(Progress_str, "trial_trigger_saved_protocol",
                      String(S.currProtocolIndex));
  appendProgressField(Progress_str, "pending_ready",
                      String((int)pending_ready));
  appendProgressField(Progress_str, "auto_ready", String((int)auto_ready));
  appendProgressField(Progress_str, "trial_block_onset",
                      String(TrialBlockOnset));
  appendProgressField(Progress_str, "block_step", String(BlockSwitchStep));
  appendProgressField(Progress_str, "block_second_rule",
                      String(BlockSwitchSecondRule));
  appendProgressField(Progress_str, "block_next_a", String(block_next_a));
  appendProgressField(Progress_str, "block_next_b", String(block_next_b));
  appendProgressField(Progress_str, "trial_count",
                      String(S.currProtocolTrials));
  appendProgressField(Progress_str, "trial_target", String(trial_target));
  appendProgressField(Progress_str, "trial_op", String(trial_op));
  appendProgressField(Progress_str, "trial_ready", String((int)trial_ready));
  appendProgressField(Progress_str, "easy_threshold",
                      String(EasyPerf50SwitchThreshold));
  appendProgressField(Progress_str, "easy_required",
                      String(easy_perf_side_trials));
  appendProgressField(Progress_str, "easy_ready",
                      String((int)easy_perf50_ready));
  appendProgressField(Progress_str, "left_easy_perf", String(left_easy_perf50));
  appendProgressField(Progress_str, "left_easy_count", String(left_easy_count));
  appendProgressField(Progress_str, "right_easy_perf",
                      String(right_easy_perf50));
  appendProgressField(Progress_str, "right_easy_count",
                      String(right_easy_count));
  appendProgressField(Progress_str, "easy100_required",
                      String(easy_perf_trials));
  appendProgressField(Progress_str, "easy100_perf", String(easy_perf100));
  appendProgressField(Progress_str, "easy100_count", String(easy100_count));
  appendProgressField(Progress_str, "protocol_elapsed_sec",
                      String(protocol_elapsed_sec));
  appendProgressField(Progress_str, "protocol_time_target_sec",
                      String(HostConditionTimeMinSec));
  appendProgressField(Progress_str, "protocol_time_ready",
                      String((int)host_time_ready));
  appendProgressField(Progress_str, "rule_elapsed_sec",
                      String(rule_elapsed_sec));
  appendProgressField(Progress_str, "rule_time_target_sec",
                      String(BlockRuleMinDurationSec));
  appendProgressField(Progress_str, "rule_time_ready",
                      String((int)block_rule_time_ready));
  appendProgressField(Progress_str, "host_block", HostProtocolBlockId);
  appendProgressField(Progress_str, "host_protocol", String(HostProtocolIndex));
  appendProgressField(Progress_str, "host_trial_min",
                      String(HostConditionTrialMin));
  appendProgressField(Progress_str, "host_time_min_sec",
                      String(HostConditionTimeMinSec));
  appendProgressField(Progress_str, "host_time_ready",
                      String((int)(HostConditionTimeMinSec == 0 ||
                                   hasElapsedSince(CurrentProtocolFirstTrialUnix,
                                                   HostConditionTimeMinSec))));
  appendProgressField(Progress_str, "host_perf_mode",
                      String(HostConditionPerfMode));
  appendProgressField(Progress_str, "host_threshold",
                      String(HostConditionThreshold));
  appendProgressField(Progress_str, "host_window",
                      String(HostConditionWindowSize));
  appendProgressField(Progress_str, "host_required",
                      String(HostConditionRequiredCount));
  appendProgressField(Progress_str, "host_ready",
                      String((int)isHostProtocolConditionReady()));
  appendProgressField(Progress_str, "protocol_switch_safe",
                      String((int)isProtocolSwitchSafeNow()));
  appendProgressField(Progress_str, "flow_at_end",
                      String((int)(HostFlowLength == 0 ||
                                   HostFlowCurrentIndex + 1 >= HostFlowLength)));
  appendProgressField(Progress_str, "flow_len", String(HostFlowLength));
  appendProgressField(Progress_str, "flow_current_index",
                      String(HostFlowCurrentIndex));
  appendByteArrayProgressField(Progress_str, "flow_protocols",
                               HostFlowProtocols, HostFlowLength);
  appendUIntArrayProgressField(Progress_str, "flow_trial_min",
                               HostFlowTrialMin, HostFlowLength);
  appendULongArrayProgressField(Progress_str, "flow_time_min_sec",
                                HostFlowTimeMinSec, HostFlowLength);
  appendByteArrayProgressField(Progress_str, "flow_perf_mode",
                               HostFlowPerfMode, HostFlowLength);
  appendByteArrayProgressField(Progress_str, "flow_threshold",
                               HostFlowThreshold, HostFlowLength);
  appendUIntArrayProgressField(Progress_str, "flow_window",
                               HostFlowWindowSize, HostFlowLength);
  appendByteArrayProgressField(Progress_str, "flow_required",
                               HostFlowRequiredCount, HostFlowLength);
  appendByteArrayProgressField(Progress_str, "flow_fixed",
                               HostFlowFixed, HostFlowLength);
  appendProgressField(Progress_str, "retention_hits",
                      String(HostRetentionHits));
  appendProgressField(Progress_str, "retention_window_valid",
                      String(HostRetentionWindowValid));
  appendProgressField(Progress_str, "retention_window_correct",
                      String(HostRetentionWindowCorrect));
  appendProgressField(Progress_str, "retention_last_window_perf",
                      String(HostRetentionLastWindowPerf));
  appendProgressField(Progress_str, "rule_hint_active",
                      String((int)RuleSwitchHintActive));
  appendProgressField(Progress_str, "rule_hint_count",
                      String(RuleSwitchCorrectCount));
  appendProgressField(Progress_str, "rule_hint_target",
                      String(RuleSwitchHintCorrectTrialLimit));

  Progress_str.toCharArray(outBuffer, sizeof(outBuffer));
  Serial.println(outBuffer);
}

int getEasyTrialPerf100() {
  return getEasyTrialPerfByType(0, easy_perf_trials);
}

static void getEasyTrialStatsByType(byte trial_type, byte required_trials,
                                    int &perf, unsigned int &total_num) {
  int correct_num = 0;
  total_num = 0;
  for (int i = RECORD_TRIALS - 1; i >= 0; i--) {
    if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
      continue;
    }
    if (S.ProtocolIndexHistory[i] == S.currProtocolIndex &&
        S.SampleTypeHistory[i] == 2 &&
        (trial_type == 0 || S.TrialTypeHistory[i] == trial_type) &&
        (S.OutcomeHistory[i] == 1 || S.OutcomeHistory[i] == 2)) {
      total_num++;
      if (S.OutcomeHistory[i] == 1) {
        correct_num++;
      }
      if (total_num >= required_trials) {
        break;
      }
    } else if (S.ProtocolIndexHistory[i] != S.currProtocolIndex) {
      break;
    }
  }

  perf = (total_num > 0) ? int((100.0f * float(correct_num)) / float(total_num))
                         : 0;
}

int getEasyTrialPerfByType(byte trial_type, byte required_trials) {
  int perf = 0;
  unsigned int total_num = 0;
  getEasyTrialStatsByType(trial_type, required_trials, perf, total_num);
  if (total_num < required_trials) {
    return -perf; // negative = not enough trials yet, but value is real-time
                  // perf
  }
  return perf;
}

static unsigned long getElapsedSinceSec(unsigned long start_unix) {
  if (start_unix == 0) {
    return 0;
  }
  unsigned long now_unix = currentUnixTime();
  if (now_unix < start_unix) {
    return 0;
  }
  return now_unix - start_unix;
}

static void appendProgressField(String &packet, const char *key,
                                const String &value) {
  packet += key;
  packet += "=";
  packet += value;
  packet += ";";
}

static void appendByteArrayProgressField(String &packet, const char *key,
                                         const byte *values, byte length) {
  packet += key;
  packet += "=";
  for (byte i = 0; i < length; i++) {
    if (i > 0) {
      packet += ".";
    }
    packet += String(values[i]);
  }
  packet += ";";
}

static void appendUIntArrayProgressField(String &packet, const char *key,
                                         const unsigned int *values,
                                         byte length) {
  packet += key;
  packet += "=";
  for (byte i = 0; i < length; i++) {
    if (i > 0) {
      packet += ".";
    }
    packet += String(values[i]);
  }
  packet += ";";
}

static void appendULongArrayProgressField(String &packet, const char *key,
                                          const unsigned long *values,
                                          byte length) {
  packet += key;
  packet += "=";
  for (byte i = 0; i < length; i++) {
    if (i > 0) {
      packet += ".";
    }
    packet += String(values[i]);
  }
  packet += ";";
}

static void resetHostRetentionProgress() {
  HostRetentionWindowValid = 0;
  HostRetentionWindowCorrect = 0;
  HostRetentionHits = 0;
  HostRetentionLastWindowPerf = 0;
}

static void setHostFlowBlockDefaults(byte index, byte protocol_index) {
  if (index >= HostFlowMaxBlocks) {
    return;
  }
  protocol_index = constrain(protocol_index, 0, 10);
  HostFlowProtocols[index] = protocol_index;
  HostFlowTrialMin[index] = 0;
  HostFlowTimeMinSec[index] = 0;
  HostFlowPerfMode[index] = 0;
  HostFlowThreshold[index] = 0;
  HostFlowWindowSize[index] = 0;
  HostFlowRequiredCount[index] = 1;
  HostFlowFixed[index] = 0;
  if (protocol_index == 0) {
    HostFlowTimeMinSec[index] = Protocol0MinDurationSec;
  } else if (protocol_index >= 1 && protocol_index <= 4) {
    HostFlowTrialMin[index] = 101;
  } else if (protocol_index == 5 || protocol_index == 7 ||
             protocol_index == 9) {
    HostFlowTrialMin[index] = 250;
    HostFlowPerfMode[index] = 1;
    HostFlowThreshold[index] = EasyPerf50SwitchThreshold;
    HostFlowWindowSize[index] = easy_perf_side_trials;
  } else if (protocol_index == 6 || protocol_index == 8 ||
             protocol_index == 10) {
    HostFlowTrialMin[index] = 1000;
    HostFlowPerfMode[index] = 2;
    HostFlowThreshold[index] = 75;
    HostFlowWindowSize[index] = 100;
    HostFlowRequiredCount[index] = 10;
  }
}

static void initDefaultHostProtocolFlow() {
  const byte default_length = 15;
  const byte default_protocols[default_length] = {
      0, 1, 2, 3, 4, 7, 8, 9, 10, 7, 8, 9, 10, 5, 6};
  HostFlowLength = default_length;
  HostFlowCurrentIndex = 0;
  for (byte i = 0; i < HostFlowMaxBlocks; i++) {
    byte protocol_index = (i < default_length) ? default_protocols[i] : 0;
    setHostFlowBlockDefaults(i, protocol_index);
  }
  applyCurrentHostFlowBlock(false);
}

static String hostFlowBlockId(byte index) {
  String block_id = "block_";
  if (index < 10) {
    block_id += "00";
  } else if (index < 100) {
    block_id += "0";
  }
  block_id += String(index);
  block_id += "_p";
  block_id += String(HostFlowProtocols[index]);
  return block_id;
}

static int hostFlowIndexFromBlockId(const String &block_id) {
  if (!block_id.startsWith("block_")) {
    return -1;
  }
  int start = 6;
  int end = block_id.indexOf('_', start);
  if (end <= start) {
    return -1;
  }
  int index = block_id.substring(start, end).toInt();
  if (index < 0 || index >= HostFlowLength) {
    return -1;
  }
  return index;
}

static void syncHostFlowIndexToProtocol(byte protocol_index) {
  if (HostFlowLength == 0) {
    initDefaultHostProtocolFlow();
  }
  if (HostFlowCurrentIndex < HostFlowLength &&
      HostFlowProtocols[HostFlowCurrentIndex] == protocol_index) {
    return;
  }
  for (byte i = 0; i < HostFlowLength; i++) {
    if (HostFlowProtocols[i] == protocol_index && HostFlowFixed[i] == 0) {
      HostFlowCurrentIndex = i;
      return;
    }
  }
  for (byte i = 0; i < HostFlowLength; i++) {
    if (HostFlowProtocols[i] == protocol_index) {
      HostFlowCurrentIndex = i;
      return;
    }
  }
}

static void applyCurrentHostFlowBlock(bool reset_progress_if_changed) {
  if (HostFlowLength == 0 || HostFlowCurrentIndex >= HostFlowLength) {
    return;
  }
  bool condition_changed =
      HostProtocolIndex != HostFlowProtocols[HostFlowCurrentIndex] ||
      HostConditionTrialMin != HostFlowTrialMin[HostFlowCurrentIndex] ||
      HostConditionTimeMinSec != HostFlowTimeMinSec[HostFlowCurrentIndex] ||
      HostConditionPerfMode != HostFlowPerfMode[HostFlowCurrentIndex] ||
      HostConditionThreshold != HostFlowThreshold[HostFlowCurrentIndex] ||
      HostConditionWindowSize != HostFlowWindowSize[HostFlowCurrentIndex] ||
      HostConditionRequiredCount != HostFlowRequiredCount[HostFlowCurrentIndex];
  HostProtocolIndex = HostFlowProtocols[HostFlowCurrentIndex];
  HostProtocolBlockId = hostFlowBlockId(HostFlowCurrentIndex);
  HostConditionTrialMin = HostFlowTrialMin[HostFlowCurrentIndex];
  HostConditionTimeMinSec = HostFlowTimeMinSec[HostFlowCurrentIndex];
  HostConditionPerfMode = HostFlowPerfMode[HostFlowCurrentIndex];
  HostConditionThreshold = HostFlowThreshold[HostFlowCurrentIndex];
  HostConditionWindowSize = HostFlowWindowSize[HostFlowCurrentIndex];
  HostConditionRequiredCount = HostFlowRequiredCount[HostFlowCurrentIndex];
  if (reset_progress_if_changed && condition_changed) {
    resetHostRetentionProgress();
  }
}

static void storeCurrentConditionInFlowBlock() {
  if (HostFlowLength == 0 || HostFlowCurrentIndex >= HostFlowLength) {
    return;
  }
  HostFlowProtocols[HostFlowCurrentIndex] = HostProtocolIndex;
  HostFlowTrialMin[HostFlowCurrentIndex] = HostConditionTrialMin;
  HostFlowTimeMinSec[HostFlowCurrentIndex] = HostConditionTimeMinSec;
  HostFlowPerfMode[HostFlowCurrentIndex] = HostConditionPerfMode;
  HostFlowThreshold[HostFlowCurrentIndex] = HostConditionThreshold;
  HostFlowWindowSize[HostFlowCurrentIndex] = HostConditionWindowSize;
  HostFlowRequiredCount[HostFlowCurrentIndex] = HostConditionRequiredCount;
}

static byte parseDotByteAt(const String &text, byte index, byte fallback,
                           byte min_value, byte max_value) {
  int start = 0;
  for (byte i = 0; i <= index; i++) {
    int end = text.indexOf('.', start);
    if (i == index) {
      String token = (end < 0) ? text.substring(start) : text.substring(start, end);
      token.trim();
      if (token.length() == 0) {
        return fallback;
      }
      return constrain(token.toInt(), min_value, max_value);
    }
    if (end < 0) {
      return fallback;
    }
    start = end + 1;
  }
  return fallback;
}

static unsigned int parseDotUIntAt(const String &text, byte index,
                                   unsigned int fallback) {
  int start = 0;
  for (byte i = 0; i <= index; i++) {
    int end = text.indexOf('.', start);
    if (i == index) {
      String token = (end < 0) ? text.substring(start) : text.substring(start, end);
      token.trim();
      if (token.length() == 0) {
        return fallback;
      }
      return (unsigned int)max(0, token.toInt());
    }
    if (end < 0) {
      return fallback;
    }
    start = end + 1;
  }
  return fallback;
}

static unsigned long parseDotULongAt(const String &text, byte index,
                                     unsigned long fallback) {
  int start = 0;
  for (byte i = 0; i <= index; i++) {
    int end = text.indexOf('.', start);
    if (i == index) {
      String token = (end < 0) ? text.substring(start) : text.substring(start, end);
      token.trim();
      if (token.length() == 0) {
        return fallback;
      }
      return (unsigned long)max(0L, token.toInt());
    }
    if (end < 0) {
      return fallback;
    }
    start = end + 1;
  }
  return fallback;
}

static bool parseHostProtocolCondition(const char *buf) {
  char block_id[40] = {};
  int protocol = 0;
  int trial_min = 0;
  long time_min_sec = 0;
  int perf_mode = 0;
  int threshold = 0;
  int window_size = 0;
  int required_count = 0;
  int parsed = sscanf(buf, "PCOND,%39[^,],%d,%d,%ld,%d,%d,%d,%d", block_id,
                      &protocol, &trial_min, &time_min_sec, &perf_mode,
                      &threshold, &window_size, &required_count);
  if (parsed != 8) {
    parsed = sscanf(buf, "PCOND,%39[^,],%d,%d,%d,%d,%d,%d", block_id,
                    &protocol, &trial_min, &perf_mode, &threshold,
                    &window_size, &required_count);
    if (parsed != 7) {
      return false;
    }
    time_min_sec = 0;
  }
  bool conditionChanged =
      HostProtocolBlockId != String(block_id) ||
      HostProtocolIndex != constrain(protocol, 0, 10) ||
      HostConditionTrialMin != (unsigned int)max(0, trial_min) ||
      HostConditionTimeMinSec != (unsigned long)max(0L, time_min_sec) ||
      HostConditionPerfMode != constrain(perf_mode, 0, 3) ||
      HostConditionThreshold != constrain(threshold, 0, 100) ||
      HostConditionWindowSize != (unsigned int)max(0, window_size) ||
      HostConditionRequiredCount != constrain(required_count, 1, 100);
  HostProtocolBlockId = String(block_id);
  HostProtocolIndex = constrain(protocol, 0, 10);
  HostConditionTrialMin = max(0, trial_min);
  HostConditionTimeMinSec = (unsigned long)max(0L, time_min_sec);
  HostConditionPerfMode = constrain(perf_mode, 0, 3);
  HostConditionThreshold = constrain(threshold, 0, 100);
  HostConditionWindowSize = max(0, window_size);
  HostConditionRequiredCount = constrain(required_count, 1, 100);
  int parsed_index = hostFlowIndexFromBlockId(HostProtocolBlockId);
  if (parsed_index >= 0) {
    HostFlowCurrentIndex = byte(parsed_index);
  } else {
    syncHostFlowIndexToProtocol(HostProtocolIndex);
  }
  storeCurrentConditionInFlowBlock();
  if (conditionChanged) {
    resetHostRetentionProgress();
  }
  return true;
}

static bool parseHostProtocolFlow(const char *buf) {
  String text = String(buf);
  text.trim();
  if (!text.startsWith("PFLOW,")) {
    return false;
  }
  text = text.substring(6);
  String fields[10];
  int start = 0;
  for (byte i = 0; i < 10; i++) {
    int end = text.indexOf(',', start);
    if (end < 0) {
      fields[i] = text.substring(start);
      start = text.length();
    } else {
      fields[i] = text.substring(start, end);
      start = end + 1;
    }
    fields[i].trim();
  }
  int current_index = fields[0].toInt();
  int requested_length = fields[1].toInt();
  if (requested_length <= 0) {
    return false;
  }
  HostFlowLength = constrain(requested_length, 1, HostFlowMaxBlocks);
  HostFlowCurrentIndex = constrain(current_index, 0, HostFlowLength - 1);
  for (byte i = 0; i < HostFlowLength; i++) {
    byte protocol_index = parseDotByteAt(fields[2], i, 0, 0, 10);
    setHostFlowBlockDefaults(i, protocol_index);
    HostFlowTrialMin[i] = parseDotUIntAt(fields[3], i, HostFlowTrialMin[i]);
    HostFlowTimeMinSec[i] = parseDotULongAt(fields[4], i, HostFlowTimeMinSec[i]);
    HostFlowPerfMode[i] = parseDotByteAt(fields[5], i, HostFlowPerfMode[i], 0, 3);
    HostFlowThreshold[i] = parseDotByteAt(fields[6], i, HostFlowThreshold[i], 0, 100);
    HostFlowWindowSize[i] = parseDotUIntAt(fields[7], i, HostFlowWindowSize[i]);
    HostFlowRequiredCount[i] =
        parseDotByteAt(fields[8], i, HostFlowRequiredCount[i], 1, 100);
    HostFlowFixed[i] = parseDotByteAt(fields[9], i, HostFlowFixed[i], 0, 1);
  }
  syncHostFlowIndexToProtocol(S.currProtocolIndex);
  applyCurrentHostFlowBlock(true);
  return true;
}

static String buildHostProtocolFlowCommand() {
  String command = "PFLOW,";
  command.reserve(768);
  command += String(HostFlowCurrentIndex);
  command += ",";
  command += String(HostFlowLength);
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowProtocols[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowTrialMin[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowTimeMinSec[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowPerfMode[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowThreshold[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowWindowSize[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowRequiredCount[i]);
  }
  command += ",";
  for (byte i = 0; i < HostFlowLength; i++) {
    if (i > 0) {
      command += ".";
    }
    command += String(HostFlowFixed[i]);
  }
  command += ",";
  return command;
}

static bool isHostProtocolConditionReady() {
  if (S.currProtocolIndex != HostProtocolIndex) {
    return false;
  }
  if (S.currProtocolTrials < HostConditionTrialMin) {
    return false;
  }
  if (HostConditionTimeMinSec > 0 &&
      !hasElapsedSince(CurrentProtocolFirstTrialUnix,
                       HostConditionTimeMinSec)) {
    return false;
  }
  if (HostConditionPerfMode == 0) {
    return true;
  }
  if (HostConditionPerfMode == 1) {
    int left_perf = 0;
    int right_perf = 0;
    unsigned int left_count = 0;
    unsigned int right_count = 0;
    byte required = byte(constrain(HostConditionWindowSize, 1, 255));
    getEasyTrialStatsByType(1, required, left_perf, left_count);
    getEasyTrialStatsByType(2, required, right_perf, right_count);
    return left_count >= required && right_count >= required &&
           left_perf >= HostConditionThreshold &&
           right_perf >= HostConditionThreshold;
  }
  if (HostConditionPerfMode == 2) {
    return HostRetentionHits >= HostConditionRequiredCount;
  }
  if (HostConditionPerfMode == 3) {
    return true;
  }
  return false;
}

static void updateHostRetentionProgressAfterTrial() {
  if (HostConditionPerfMode != 2 || S.currProtocolIndex != HostProtocolIndex) {
    return;
  }
  byte outcome = S.OutcomeHistory[RECORD_TRIALS - 1];
  if (S.SampleTypeHistory[RECORD_TRIALS - 1] != 2 ||
      (outcome != 1 && outcome != 2)) {
    return;
  }
  HostRetentionWindowValid++;
  if (outcome == 1) {
    HostRetentionWindowCorrect++;
  }
  unsigned int required_window = max((unsigned int)1, HostConditionWindowSize);
  if (HostRetentionWindowValid >= required_window) {
    HostRetentionLastWindowPerf =
        int((100.0f * float(HostRetentionWindowCorrect)) /
            float(HostRetentionWindowValid));
    if (HostRetentionLastWindowPerf >= HostConditionThreshold) {
      if (HostRetentionHits < 255) {
        HostRetentionHits++;
      }
    } else {
      HostRetentionHits = 0;
    }
    HostRetentionWindowValid = 0;
    HostRetentionWindowCorrect = 0;
  }
}

static bool advanceHostProtocolFlow(bool force_now) {
  syncHostFlowIndexToProtocol(S.currProtocolIndex);
  applyCurrentHostFlowBlock(false);
  if (HostFlowLength == 0 || HostFlowCurrentIndex >= HostFlowLength) {
    return false;
  }
  if (HostFlowCurrentIndex + 1 >= HostFlowLength) {
    return false;
  }
  HostFlowFixed[HostFlowCurrentIndex] = 1;
  HostFlowCurrentIndex++;
  applyCurrentHostFlowBlock(false);
  switchToProtocol(HostFlowProtocols[HostFlowCurrentIndex]);
  PendingProtocolIndex = 255;
  trialSelection();
  write_SD_para_S();
  SendProtocolProgress2PC();
  return true;
}

void activateRuleSwitchHint() {
  RuleSwitchHintActive = 1;
  RuleSwitchCorrectCount = 0;
}

void applyProtocolPreset(byte protocol_index) {
  S.currProtocolIndex = protocol_index;
  S.currProtocolTrials = 0;
  S.currProtocolPerf = 0;
  S.reward_middle = S.reward_left;
  S.TrialPresentMode = 0;
  S.SamplePeriod = Fundemental_SamplePeriod;
  S.AnswerPeriod = 5000;
  S.ConsumptionPeriod = 750;
  S.StopLickingPeriod = 400;
  S.PreCueDelayPeriod = 44;
  S.PreCuePeriod = 1000;

  switch (protocol_index) {
  case 0:
    S.TrialBlockInitPeriod = 50;
    S.TimeOut = 2000;
    S.AnswerPeriod = 10000;
    S.rule = 0;
    break;
  case 1:
    S.TrialBlockInitPeriod = 100;
    S.TimeOut = 2000;
    S.AnswerPeriod = 10000;
    S.rule = 0;
    break;
  case 2:
    S.TrialBlockInitPeriod = 150;
    S.TimeOut = 2000;
    S.AnswerPeriod = 10000;
    S.rule = 0;
    break;
  case 3:
    S.TrialBlockInitPeriod = 200;
    S.TimeOut = 2000;
    S.AnswerPeriod = 10000;
    S.rule = 0;
    break;
  case 4:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.AnswerPeriod = 10000;
    S.rule = 0;
    break;
  case 5:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 0;
    break;
  case 6:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 0;
    break;
  case 7:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 1;
    break;
  case 8:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 1;
    break;
  case 9:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 2;
    break;
  case 10:
    S.TrialBlockInitPeriod = 250;
    S.TimeOut = 2000;
    S.rule = 2;
    break;
  default:
    break;
  }
}

void switchToProtocol(byte next_protocol) {
  if (TrialTriggerFreeWaterActive == 1) {
    clearTrialTriggerFreeWaterState();
  }
  byte previous_rule = S.rule;
  applyProtocolPreset(next_protocol);
  PreviousRule = previous_rule;
  syncHostFlowIndexToProtocol(next_protocol);
  applyCurrentHostFlowBlock(false);
  CurrentProtocolFirstTrialUnix = 0;
  CurrentRuleFirstTrialUnix = 0;
  if (S.rule != previous_rule) {
    activateRuleSwitchHint();
  }
  resetHostRetentionProgress();
}

static bool applyPendingProtocolSwitchIfReady(bool force_now) {
  if (PendingProtocolIndex == 255) {
    return false;
  }
  if (!force_now && TrialBlockOnset != 1) {
    return false;
  }
  switchToProtocol(PendingProtocolIndex);
  PendingProtocolIndex = 255;
  return true;
}

void autoChangeProtocol(bool manual) {
  if (isTrialTriggerFreeWaterActive()) {
    return;
  }
  syncHostFlowIndexToProtocol(S.currProtocolIndex);
  applyCurrentHostFlowBlock(false);

  if (PendingProtocolIndex != 255) {
    applyPendingProtocolSwitchIfReady(manual);
    return;
  }

  switch (S.currProtocolIndex) {
  case 0:
  case 1:
  case 2:
  case 3:
  case 4:
  case 5:
  case 6:
  case 7:
  case 8:
  case 9:
  case 10:
    if (manual || isHostProtocolConditionReady()) {
      advanceHostProtocolFlow(manual);
    }
    break;
  default:
    break;
  }
}

void autoReward() {
  if (isTrialTriggerFreeWaterActive()) {
    return;
  }
  S.GaveFreeReward.past_trials++;
  byte error_trials = MaxSame; // consecutive 10 errors in a paticular trial
                               // type => free reward in next trial
  if (S.GaveFreeReward.past_trials >= error_trials) {
    byte n_RSideErrors = 0;
    byte n_LSideErrors = 0;
    byte inspected_trials = 0;
    for (int i = RECORD_TRIALS - 1; i >= 0 && inspected_trials < error_trials;
         i--) {
      if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
        continue;
      }
      inspected_trials++;
      if (S.TrialTypeHistory[i] == 2 && S.OutcomeHistory[i] != 1) {
        n_RSideErrors++;
      } else if (S.TrialTypeHistory[i] == 1 && S.OutcomeHistory[i] != 1) {
        n_LSideErrors++;
      }
    }
    if (inspected_trials < error_trials) {
      return;
    }
    if (n_RSideErrors == error_trials) {
      S.GaveFreeReward.flag_R_water = 1;
      S.GaveFreeReward.flag_L_water = 0;
      S.GaveFreeReward.past_trials = 0;
    } else if (n_LSideErrors == error_trials) {
      S.GaveFreeReward.flag_R_water = 0;
      S.GaveFreeReward.flag_L_water = 1;
      S.GaveFreeReward.past_trials = 0;
    }
  }
}

void trialSelection() {
  bool antiBiasEnabled = true; // shaping and retention 6/8/10
  float antiBiasLeftProb = 0.5f;
  if (antiBiasEnabled) {
    int leftLickCount = 0;
    int rightLickCount = 0;
    for (int i = 0; i < RECORD_TRIALS; i++) {
      if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
        continue;
      }
      byte outcome = S.OutcomeHistory[i];
      byte trialType = S.TrialTypeHistory[i];
      if (outcome == 1 && trialType == 1) {
        leftLickCount++;
      } else if (outcome == 1 && trialType == 2) {
        rightLickCount++;
      } else if (outcome == 2 && trialType == 1) {
        rightLickCount++;
      } else if (outcome == 2 && trialType == 2) {
        leftLickCount++;
      }
    }
    int totalLickCount = leftLickCount + rightLickCount;
    if (totalLickCount >= 10) {
      antiBiasLeftProb = float(rightLickCount + 1) / float(totalLickCount + 2);
      if (antiBiasLeftProb < SideAntiBiasMinProb) {
        antiBiasLeftProb = SideAntiBiasMinProb;
      } else if (antiBiasLeftProb > SideAntiBiasMaxProb) {
        antiBiasLeftProb = SideAntiBiasMaxProb;
      }
    }
  }

  // ---- Congruency antibias for all normal protocols ----
  // Classify stimuli against the previous protocol's rule: congruent trials keep
  // the same correct action, while incongruent trials switch action. Bias toward
  // the weaker category whenever both categories exist for this rule transition.
  bool congAntiBiasAllowed =
      (!isTrialTriggerFreeWaterActive() && S.currProtocolIndex <= 10);
  bool congAntiBiasEnabled = false;
  float congDesiredProb =
      0.5f; // desired proportion of congruent trials (among non-ambiguous)

  if (congAntiBiasAllowed) {
    // Check if both congruent and incongruent stimuli exist for this rule
    // transition
    bool hasCongruent = false;
    bool hasIncongruent = false;
    for (int s1idx = 0; s1idx < 2; s1idx++) {
      for (int s2idx = 0; s2idx < 2; s2idx++) {
        byte tt_prev =
            computeTrialType(PreviousRule, S1Feat[s1idx], S2EasyFeat[s2idx]);
        byte tt_curr = computeTrialType(S.rule, S1Feat[s1idx], S2EasyFeat[s2idx]);
        if (tt_prev == 0 || tt_curr == 0)
          continue; // ambiguous (S2==0)
        if (tt_prev == tt_curr)
          hasCongruent = true;
        else
          hasIncongruent = true;
      }
    }

    // Only apply if BOTH congruent AND incongruent stimuli exist
    // (e.g., Rule 1<->2 are all incongruent, so skip)
    if (hasCongruent && hasIncongruent) {
      // Count performance from last 100 valid trials of current protocol
      int congCorrect = 0, congTotal = 0;
      int incongCorrect = 0, incongTotal = 0;
      int validCount = 0;

      for (int i = RECORD_TRIALS - 1; i >= 0 && validCount < 100; i--) {
        if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
          continue;
        }
        if (S.ProtocolIndexHistory[i] != S.currProtocolIndex)
          break;
        byte outcome = S.OutcomeHistory[i];
        if (outcome != 1 && outcome != 2)
          continue; // skip no-response / earlylick

        // Reconstruct stimuli from history (stored as int((stimu * 2) + 2))
        float hist_s1 = (float(S.Stimu1History[i]) - 2.0f) / 2.0f;
        float hist_s2 = (float(S.Stimu2History[i]) - 2.0f) / 2.0f;

        byte tt_prev = computeTrialType(PreviousRule, hist_s1, hist_s2);
        byte tt_curr = computeTrialType(S.rule, hist_s1, hist_s2);

        if (tt_prev == 0 || tt_curr == 0)
          continue; // ambiguous, skip

        validCount++;
        if (tt_prev == tt_curr) { // congruent
          congTotal++;
          if (outcome == 1)
            congCorrect++;
        } else { // incongruent
          incongTotal++;
          if (outcome == 1)
            incongCorrect++;
        }
      }

      // Compute congruency bias if enough data (>= 10 valid categorized trials)
      if (congTotal + incongTotal >= 10) {
        congAntiBiasEnabled = true;
        float congRate =
            (congTotal > 0) ? float(congCorrect) / float(congTotal) : 0.5f;
        float incongRate = (incongTotal > 0)
                               ? float(incongCorrect) / float(incongTotal)
                               : 0.5f;

        // If mouse is better at congruent -> present less congruent (more
        // incongruent) congDesiredProb = proportion of congruent trials we want
        congDesiredProb =
            (incongRate + 0.001f) / (congRate + incongRate + 0.002f);

        // Cap at [0.2, 0.8] to avoid extreme imbalance (max 80% for either)
        if (congDesiredProb > 0.8f)
          congDesiredProb = 0.8f;
        if (congDesiredProb < 0.2f)
          congDesiredProb = 0.2f;
      }
    }
  }

  // ---- Main stimulus selection loop with rejection sampling ----
  bool trialSelected = false;
  for (int attempt = 0; attempt < 64; attempt++) {
    currStimu[0] = S1Feat[int(random(0, 2))];
    currStimu[1] = S2EasyFeat[int(random(0, 2))];
    rule_define(S.rule, currStimu[0], currStimu[1]);

    // Side antibias for shaping and retention; capped at 20/80.
    if (antiBiasEnabled && (TrialType == 1 || TrialType == 2)) {
      float leftWeight = antiBiasLeftProb;
      float rightWeight = 1.0f - antiBiasLeftProb;
      float maxWeight = leftWeight > rightWeight ? leftWeight : rightWeight;
      float acceptWeight = (TrialType == 1) ? leftWeight : rightWeight;
      float acceptProb = maxWeight > 0.0f ? (acceptWeight / maxWeight) : 1.0f;
      if (float(random(0, 10000)) >= acceptProb * 10000.0f) {
        continue; // reject this draw
      }
    }

    // Congruency antibias when the current rule has both categories and a bias.
    if (congAntiBiasEnabled) {
      byte tt_prev = computeTrialType(PreviousRule, currStimu[0], currStimu[1]);
      byte tt_curr = computeTrialType(S.rule, currStimu[0], currStimu[1]);

      // Compute acceptance based on congDesiredProb
      float maxAccept =
          (congDesiredProb > 0.5f) ? congDesiredProb : (1.0f - congDesiredProb);

      if (tt_prev == 0 || tt_curr == 0) {
        continue;
      } else {
        bool isCongruent = (tt_prev == tt_curr);
        float congAccept = congDesiredProb / maxAccept;
        float incongAccept = (1.0f - congDesiredProb) / maxAccept;
        float acceptProb = isCongruent ? congAccept : incongAccept;
        if (float(random(0, 10000)) >= acceptProb * 10000.0f) {
          continue;
        }
      }
    }

    trialSelected = true;
    break;
  }
  if (!trialSelected) {
    rule_define(S.rule, currStimu[0], currStimu[1]);
  }
  int switch_indicator = 0;
  for (int i = RECORD_TRIALS - 1; i >= 0; i--) {
    if (S.ProtocolIndexHistory[i] == TrialTriggerFreeWaterProtocolIndex) {
      continue;
    }
    if (S.OutcomeHistory[i] == S.OutcomeHistory[RECORD_TRIALS - 1] &&
        S.OutcomeHistory[i] == 3) {
      switch_indicator++;
    } else {
      break;
    }
  }
  if (switch_indicator >= MaxSame) {
    // 连续10次 earlylick
    EL_Favor = 0; // do a favor trial
  } else {
    EL_Favor = 1;
  }
  Serial.print("CongDesiredProb:");
  Serial.print(congDesiredProb);
  Serial.print(" CongEnabled:");
  Serial.println(congAntiBiasEnabled);
}

/**************************************************************************************************************/
/********************************************** SD related Functions
 * *******************************************/
/**************************************************************************************************************/
void parseHistoryLineToArray(String history_line, byte *target,
                             int target_len) {
  for (int i = 0; i < target_len; i++) {
    target[i] = 0;
  }
  int start = 0;
  int idx = 0;
  while (idx < target_len && start < history_line.length()) {
    int sep = history_line.indexOf(';', start);
    String token;
    if (sep < 0) {
      token = history_line.substring(start);
      start = history_line.length();
    } else {
      token = history_line.substring(start, sep);
      start = sep + 1;
    }
    token.trim();
    if (token.length() > 0) {
      target[idx] = byte(token.toInt());
    }
    idx++;
  }
}

int write_SD_para_S() {
  LastParaSPersistUnix = currentUnixTime();
  File dataFile = SD.open("paraS.txt", FILE_WRITE_BEGIN);
  if (dataFile) {
    // currTrialNum
    dataFile.print("currTrialNum = ");
    dataFile.println(S.currTrialNum);
    // currProtocolIndex
    dataFile.print("currProtocolIndex = ");
    dataFile.println(persistedProtocolIndex());
    // currProtocolTrials
    dataFile.print("currProtocolTrials = ");
    dataFile.println(persistedProtocolTrials());
    // currProtocolPerf
    dataFile.print("currProtocolPerf = ");
    dataFile.println(persistedProtocolPerf(), 4);
    // TrialPresentMode
    dataFile.print("TrialPresentMode = ");
    dataFile.println(S.TrialPresentMode);
    // ProtocolIndexHistory
    dataFile.print("ProtocolIndexHistory = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.ProtocolIndexHistory[i]);
      dataFile.print("; ");
    }
    dataFile.println();
    // TrialTypeHistory
    dataFile.print("TrialTypeHistory = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.TrialTypeHistory[i]);
      dataFile.print("; ");
    }
    dataFile.println();
    // OutcomeHistory
    dataFile.print("OutcomeHistory = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.OutcomeHistory[i]);
      dataFile.print("; ");
    }
    dataFile.println();

    // EarlyLickHistory
    dataFile.print("EarlyLickHistory = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.EarlyLickHistory[i]);
      dataFile.print("; ");
    }
    dataFile.println();

    // Stimu1History
    dataFile.print("Stimu1History = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.Stimu1History[i]);
      dataFile.print("; ");
    }
    dataFile.println();

    // Stimu2History
    dataFile.print("Stimu2History = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.Stimu2History[i]);
      dataFile.print("; ");
    }
    dataFile.println();

    // SampleTypeHistory
    dataFile.print("SampleTypeHistory = ");
    for (int i = 0; i < RECORD_TRIALS; i++) {
      dataFile.print(S.SampleTypeHistory[i]);
      dataFile.print("; ");
    }
    dataFile.println();

    // totalRewardNum
    dataFile.print("totalRewardNum = ");
    dataFile.println(S.totalRewardNum);
    // reward_left
    dataFile.print("reward_left = ");
    dataFile.println(S.reward_left);
    // reward_right
    dataFile.print("reward_right = ");
    dataFile.println(S.reward_right);
    // reward_middle
    dataFile.print("reward_middle = ");
    dataFile.println(S.reward_middle);
    // GaveFreeReward
    dataFile.print("GaveFreeReward.flag_L_water = ");
    dataFile.println(S.GaveFreeReward.flag_L_water);
    dataFile.print("GaveFreeReward.flag_R_water = ");
    dataFile.println(S.GaveFreeReward.flag_R_water);
    dataFile.print("GaveFreeReward.flag_M_water = ");
    dataFile.println(S.GaveFreeReward.flag_M_water);
    dataFile.print("GaveFreeReward.past_trials = ");
    dataFile.println(S.GaveFreeReward.past_trials);

    // TrialBlockInitPeriod
    dataFile.print("TrialBlockInitPeriod = ");
    dataFile.println(S.TrialBlockInitPeriod);
    // SamplePeriod
    dataFile.print("SamplePeriod = ");
    dataFile.println(S.SamplePeriod);
    // DelayPeriod
    dataFile.print("DelayPeriod = ");
    dataFile.println(S.DelayPeriod);
    // TimeOut
    dataFile.print("TimeOut = ");
    dataFile.println(S.TimeOut);
    // AnswerPeriod
    dataFile.print("AnswerPeriod = ");
    dataFile.println(S.AnswerPeriod);
    // ConsumptionPeriod
    dataFile.print("ConsumptionPeriod = ");
    dataFile.println(S.ConsumptionPeriod);
    // StopLickingPeriod
    dataFile.print("StopLickingPeriod = ");
    dataFile.println(S.StopLickingPeriod);
    // EarlyLickPeriod
    dataFile.print("EarlyLickPeriod = ");
    dataFile.println(S.EarlyLickPeriod);
    // retention_counter
    dataFile.print("retention_counter = ");
    dataFile.println(S.retention_counter);
    // low_light_intensity
    dataFile.print("low_light_intensity = ");
    dataFile.println(S.low_light_intensity);
    // high_light_intensity
    dataFile.print("high_light_intensity = ");
    dataFile.println(S.high_light_intensity);
    // extra_TimeOut
    dataFile.print("extra_TimeOut = ");
    dataFile.println(S.extra_TimeOut);
    // Trial_txt_position
    dataFile.print("Trial_txt_position = ");
    dataFile.println(S.Trial_txt_position);
    // Tevent_txt_position
    dataFile.print("Tevent_txt_position = ");
    dataFile.println(S.Tevent_txt_position);
    // PreCueDelayPeriod
    dataFile.print("PreCueDelayPeriod = ");
    dataFile.println(S.PreCueDelayPeriod);
    // PreCuePeriod
    dataFile.print("PreCuePeriod = ");
    dataFile.println(S.PreCuePeriod);
    // hardestTrials
    dataFile.print("hardestTrials = ");
    dataFile.println(S.hardestTrials);
    // rule
    dataFile.print("rule = ");
    dataFile.println(S.rule);
    dataFile.print("TrialBlockOnset = ");
    dataFile.println(TrialBlockOnset);
    dataFile.print("BlockSwitchMode = ");
    dataFile.println(BlockSwitchMode);
    dataFile.print("BlockSwitchStep = ");
    dataFile.println(BlockSwitchStep);
    dataFile.print("BlockSwitchSecondRule = ");
    dataFile.println(BlockSwitchSecondRule);
    dataFile.print("PendingProtocolIndex = ");
    dataFile.println(PendingProtocolIndex);
    dataFile.print("RuleSwitchHintActive = ");
    dataFile.println(RuleSwitchHintActive);
    dataFile.print("RuleSwitchCorrectCount = ");
    dataFile.println(RuleSwitchCorrectCount);
    dataFile.print("EL_Favor = ");
    dataFile.println(EL_Favor);
    dataFile.print("PreviousRule = ");
    dataFile.println(PreviousRule);
    dataFile.print("InterBlockIntervalMs = ");
    dataFile.println(S.InterBlockIntervalMs);
    dataFile.print("CurrentProtocolFirstTrialUnix = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedCurrentProtocolFirstTrialUnix
                         : CurrentProtocolFirstTrialUnix);
    dataFile.print("CurrentRuleFirstTrialUnix = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedCurrentRuleFirstTrialUnix
                         : CurrentRuleFirstTrialUnix);
    dataFile.print("LastParaSPersistUnix = ");
    dataFile.println(LastParaSPersistUnix);
    dataFile.print("TrialTriggerFreeWaterActive = ");
    dataFile.println(isTrialTriggerFreeWaterActive() ? 1 : 0);
    dataFile.print("TrialTriggerSavedProtocolIndex = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedProtocolIndex
                         : S.currProtocolIndex);
    dataFile.print("TrialTriggerSavedProtocolTrials = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedProtocolTrials
                         : S.currProtocolTrials);
    dataFile.print("TrialTriggerSavedProtocolPerf = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedProtocolPerf
                         : S.currProtocolPerf,
                     4);
    dataFile.print("TrialTriggerSavedCurrentProtocolFirstTrialUnix = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedCurrentProtocolFirstTrialUnix
                         : CurrentProtocolFirstTrialUnix);
    dataFile.print("TrialTriggerSavedCurrentRuleFirstTrialUnix = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedCurrentRuleFirstTrialUnix
                         : CurrentRuleFirstTrialUnix);
    dataFile.print("TrialTriggerSavedInputGateEnabled = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedInputGateEnabled
                         : TrialTriggerInputGateEnabled);
    dataFile.print("TrialTriggerSavedInputGateRequested = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedInputGateRequested
                         : TrialTriggerInputGateRequested);
    dataFile.print("TrialTriggerSavedInterBlockIntervalGateEnabled = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedInterBlockIntervalGateEnabled
                         : InterBlockIntervalGateEnabled);
    dataFile.print("TrialTriggerSavedTrialBlockFreeRewardPending = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedTrialBlockFreeRewardPending
                         : TrialBlockFreeRewardPending);
    dataFile.print("TrialTriggerSavedTrialBlockReadyCuePlayed = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedTrialBlockReadyCuePlayed
                         : TrialBlockReadyCuePlayed);
    dataFile.print("TrialTriggerSavedLastTrialBlockTriggerMs = ");
    dataFile.println(isTrialTriggerFreeWaterActive()
                         ? TrialTriggerSavedLastTrialBlockTriggerMs
                         : LastTrialBlockTriggerMs);
    dataFile.print("HostProtocolBlockId = ");
    dataFile.println(HostProtocolBlockId);
    dataFile.print("HostProtocolIndex = ");
    dataFile.println(HostProtocolIndex);
    dataFile.print("HostConditionTrialMin = ");
    dataFile.println(HostConditionTrialMin);
    dataFile.print("HostConditionTimeMinSec = ");
    dataFile.println(HostConditionTimeMinSec);
    dataFile.print("HostConditionPerfMode = ");
    dataFile.println(HostConditionPerfMode);
    dataFile.print("HostConditionThreshold = ");
    dataFile.println(HostConditionThreshold);
    dataFile.print("HostConditionWindowSize = ");
    dataFile.println(HostConditionWindowSize);
    dataFile.print("HostConditionRequiredCount = ");
    dataFile.println(HostConditionRequiredCount);
    dataFile.print("HostRetentionWindowValid = ");
    dataFile.println(HostRetentionWindowValid);
    dataFile.print("HostRetentionWindowCorrect = ");
    dataFile.println(HostRetentionWindowCorrect);
    dataFile.print("HostRetentionHits = ");
    dataFile.println(HostRetentionHits);
    dataFile.print("HostRetentionLastWindowPerf = ");
    dataFile.println(HostRetentionLastWindowPerf);
    dataFile.print("HostProtocolFlow = ");
    dataFile.println(buildHostProtocolFlowCommand());
  } else {
    Serial.println("E: error opening paraS.txt for write");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 0;
}

int read_SD_para_S() {
  File dataFile = SD.open("paraS.txt", FILE_READ);
  if (dataFile) {
    // currTrialNum
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.currTrialNum = string_tmp.toInt();
    // currProtocolIndex
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.currProtocolIndex = string_tmp.toInt();
    // currProtocolTrials
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.currProtocolTrials = string_tmp.toInt();
    // currProtocolPerf
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.currProtocolPerf = string_tmp.toFloat();
    // TrialPresentMode
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.TrialPresentMode = string_tmp.toInt();
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.ProtocolIndexHistory, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.TrialTypeHistory, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.OutcomeHistory, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.EarlyLickHistory, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.Stimu1History, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.Stimu2History, RECORD_TRIALS);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    parseHistoryLineToArray(string_tmp, S.SampleTypeHistory, RECORD_TRIALS);

    // totalRewardNum
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.totalRewardNum = string_tmp.toInt();
    // reward_left
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.reward_left = string_tmp.toInt();
    // reward_right
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.reward_right = string_tmp.toInt();
    // reward_middle
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.reward_middle = string_tmp.toInt();

    // GaveFreeReward
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.GaveFreeReward.flag_L_water = string_tmp.toInt();
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.GaveFreeReward.flag_R_water = string_tmp.toInt();
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.GaveFreeReward.flag_M_water = string_tmp.toInt();
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.GaveFreeReward.past_trials = string_tmp.toInt();

    // TrialBlockInitPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.TrialBlockInitPeriod = string_tmp.toInt();
    // SamplePeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.SamplePeriod = string_tmp.toInt();
    // DelayPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.DelayPeriod = string_tmp.toInt();
    // TimeOut
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.TimeOut = string_tmp.toInt();
    // AnswerPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.AnswerPeriod = string_tmp.toInt();
    // ConsumptionPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.ConsumptionPeriod = string_tmp.toInt();
    // StopLickingPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.StopLickingPeriod = string_tmp.toInt();
    // EarlyLickPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.EarlyLickPeriod = string_tmp.toInt();
    // retention_counter
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.retention_counter = string_tmp.toInt();
    // low_light_intensity
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.low_light_intensity = string_tmp.toInt();
    // high_light_intensity
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.high_light_intensity = string_tmp.toInt();
    // extra_TimeOut
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.extra_TimeOut = string_tmp.toInt();
    // Trial_txt_position
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.Trial_txt_position = string_tmp.toInt();
    // Tevent_txt_position
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.Tevent_txt_position = string_tmp.toInt();
    // PreCueDelayPeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.PreCueDelayPeriod = string_tmp.toInt();
    // PreCuePeriod
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.PreCuePeriod = string_tmp.toInt();
    // hardestTrials
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.hardestTrials = string_tmp.toInt();
    // rule
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n');
    S.rule = string_tmp.toInt();
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      //  = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      BlockSwitchMode = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      BlockSwitchStep = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      BlockSwitchSecondRule = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      PendingProtocolIndex = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      RuleSwitchHintActive = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      RuleSwitchCorrectCount = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      EL_Favor = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      PreviousRule = constrain(string_tmp.toInt(), 0, 2);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      S.InterBlockIntervalMs = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      CurrentProtocolFirstTrialUnix = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      CurrentRuleFirstTrialUnix = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      LastParaSPersistUnix = string_tmp.toInt();
    }
    byte restoredFreeWaterActive = 0;
    TrialTriggerSavedProtocolIndex = S.currProtocolIndex;
    TrialTriggerSavedProtocolTrials = S.currProtocolTrials;
    TrialTriggerSavedProtocolPerf = S.currProtocolPerf;
    TrialTriggerSavedCurrentProtocolFirstTrialUnix = CurrentProtocolFirstTrialUnix;
    TrialTriggerSavedCurrentRuleFirstTrialUnix = CurrentRuleFirstTrialUnix;
    TrialTriggerSavedInputGateEnabled = TrialTriggerInputGateEnabled;
    TrialTriggerSavedInputGateRequested = TrialTriggerInputGateRequested;
    TrialTriggerSavedInterBlockIntervalGateEnabled =
        InterBlockIntervalGateEnabled;
    TrialTriggerSavedTrialBlockFreeRewardPending = TrialBlockFreeRewardPending;
    TrialTriggerSavedTrialBlockReadyCuePlayed = TrialBlockReadyCuePlayed;
    TrialTriggerSavedLastTrialBlockTriggerMs = LastTrialBlockTriggerMs;
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      restoredFreeWaterActive = string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedProtocolIndex = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedProtocolTrials = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedProtocolPerf = string_tmp.toFloat();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedCurrentProtocolFirstTrialUnix = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedCurrentRuleFirstTrialUnix = string_tmp.toInt();
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedInputGateEnabled = string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedInputGateRequested = string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedInterBlockIntervalGateEnabled =
          string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedTrialBlockFreeRewardPending =
          string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedTrialBlockReadyCuePlayed =
          string_tmp.toInt() > 0 ? 1 : 0;
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      TrialTriggerSavedLastTrialBlockTriggerMs = string_tmp.toInt();
    }
    byte restoredHostFlow = 0;
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      string_tmp.trim();
      if (string_tmp.length() > 0) {
        HostProtocolBlockId = string_tmp;
      }
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostProtocolIndex = constrain(string_tmp.toInt(), 0, 10);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionTrialMin = max(0, string_tmp.toInt());
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionTimeMinSec = (unsigned long)max(0, string_tmp.toInt());
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionPerfMode = constrain(string_tmp.toInt(), 0, 3);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionThreshold = constrain(string_tmp.toInt(), 0, 100);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionWindowSize = max(0, string_tmp.toInt());
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostConditionRequiredCount = constrain(string_tmp.toInt(), 1, 100);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostRetentionWindowValid = max(0, string_tmp.toInt());
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostRetentionWindowCorrect = max(0, string_tmp.toInt());
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostRetentionHits = constrain(string_tmp.toInt(), 0, 100);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      HostRetentionLastWindowPerf = constrain(string_tmp.toInt(), 0, 100);
    }
    if (dataFile.available()) {
      string_tmp = dataFile.readStringUntil('=');
      string_tmp = dataFile.readStringUntil('\n');
      string_tmp.trim();
      if (string_tmp.startsWith("PFLOW,")) {
        restoredHostFlow = parseHostProtocolFlow(string_tmp.c_str()) ? 1 : 0;
      }
    }
    if (TrialTriggerSavedProtocolIndex > 10) {
      TrialTriggerSavedProtocolIndex = 0;
    }
    if (restoredFreeWaterActive == 1) {
      TrialTriggerFreeWaterActive = 1;
      TrialTriggerFreeWaterRequested = 1;
      TrialTriggerFreeWaterApplyPending = 0;
      PauseAfterTrialTriggerFreeWaterExitRequested = 0;
      S.currProtocolIndex = TrialTriggerFreeWaterProtocolIndex;
      S.currProtocolTrials = TrialTriggerSavedProtocolTrials;
      S.currProtocolPerf = TrialTriggerSavedProtocolPerf;
      CurrentProtocolFirstTrialUnix =
          TrialTriggerSavedCurrentProtocolFirstTrialUnix;
      CurrentRuleFirstTrialUnix = TrialTriggerSavedCurrentRuleFirstTrialUnix;
      TrialTriggerInputGateEnabled = 1;
      TrialTriggerInputGateRequested = 1;
      InterBlockIntervalGateEnabled = 1;
      TrialBlockOnset = 1;
      TrialBlockFreeRewardPending = 1;
      TrialBlockReadyCuePlayed = 0;
      LastTrialBlockTriggerMs = 0;
      applyTrialTriggerInputGate();
    } else {
      clearTrialTriggerFreeWaterState();
      PauseAfterTrialTriggerFreeWaterExitRequested = 0;
    }
    if (S.currProtocolIndex > 10 && !isTrialTriggerFreeWaterActive()) {
      S.currProtocolIndex = 0;
    }
    if (S.rule > 2) {
      S.rule = 0;
    }
    if (TrialBlockOnset > 1) {
      TrialBlockOnset = 1;
    }
    BlockSwitchMode = 0;
    if (BlockSwitchStep > 2) {
      BlockSwitchStep = 0;
    }
    if (BlockSwitchSecondRule != 1 && BlockSwitchSecondRule != 2) {
      BlockSwitchSecondRule = 1;
    }
    if (PendingProtocolIndex != 255 && PendingProtocolIndex > 10) {
      PendingProtocolIndex = 255;
    }
    if (RuleSwitchHintActive > 1) {
      RuleSwitchHintActive = 0;
    }
    if (EL_Favor != 0 && EL_Favor != 1) {
      EL_Favor = 1;
    }
    if (S.InterBlockIntervalMs > 12UL * 60UL * 60UL * 1000UL) {
      S.InterBlockIntervalMs = 3600000UL;
    }
    unsigned long now_unix = currentUnixTime();
    if (CurrentProtocolFirstTrialUnix > 0 && now_unix > 0 &&
        CurrentProtocolFirstTrialUnix > now_unix) {
      CurrentProtocolFirstTrialUnix = 0;
    }
    if (CurrentRuleFirstTrialUnix > 0 && now_unix > 0 &&
        CurrentRuleFirstTrialUnix > now_unix) {
      CurrentRuleFirstTrialUnix = 0;
    }
    if (LastParaSPersistUnix > 0 && now_unix > 0 &&
        LastParaSPersistUnix > now_unix) {
      LastParaSPersistUnix = 0;
    }
    syncHostFlowIndexToProtocol(isTrialTriggerFreeWaterActive()
                                    ? TrialTriggerSavedProtocolIndex
                                    : S.currProtocolIndex);
    if (restoredHostFlow == 0) {
      storeCurrentConditionInFlowBlock();
    }
    applyCurrentHostFlowBlock(false);
    byte timingProtocolIndex = isTrialTriggerFreeWaterActive()
                                   ? TrialTriggerSavedProtocolIndex
                                   : S.currProtocolIndex;
    if (BlockSwitchMode == 0 || !isBlockRuleProtocol(timingProtocolIndex)) {
      CurrentRuleFirstTrialUnix = 0;
    }
  } else {
    Serial.println("E: Error opening paraS.txt for read");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 0;
}

// Write Fixation Events to SD card
int write_SD_event() {
  File dataFile = SD.open("event.txt", FILE_WRITE);
  if (dataFile) {
    for (int i = 0; i < Ev.events_num; i++) {
      dataFile.print(Ev.events_time[i]);
      dataFile.print(" ");
      dataFile.print(Ev.events_id[i]);
      dataFile.print(" ");
      dataFile.println(Ev.events_value[i]);
    }
  } else {
    Serial.println("Error opening event.txt");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 0;
}

int read_SD_cage_info() {
  // read file to identify the cage number
  File dataFile = SD.open("cage_info.txt", FILE_READ);
  if (dataFile) {
    dataFile.seek(0);
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n'); // 1st line: cage_id = xx
    cage_id = string_tmp.toInt();
    string_tmp = dataFile.readStringUntil('=');
    string_tmp = dataFile.readStringUntil('\n'); // 4th line: task = xx
    string_tmp.toCharArray(task_name, 40);
  } else {
    Serial.println("Can not open file: 'cage_info.txt'.");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 1;
}

int write_SD_cage_info() {
  File dataFile = SD.open("cage_info.txt", FILE_WRITE_BEGIN);
  if (dataFile) {
    dataFile.print("cage_id = ");
    dataFile.println(cage_id);
  } else {
    Serial.println("Can not open file: 'cage_info.txt'.");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 1;
}

// Write Trial info to SD card
int write_SD_trial_info() {
  File dataFile = SD.open("Trial.txt", FILE_WRITE);
  if (dataFile) {
    // time
    dataFile.print(Teensy3Clock.get());
    dataFile.print(" ");

    // trial#
    dataFile.print(S.currTrialNum);
    dataFile.print(" ");

    dataFile.print(S.currProtocolIndex);
    dataFile.print(" ");

    dataFile.print(TrialType);
    dataFile.print(" ");

    dataFile.print(TrialOutcome);
    dataFile.print(" ");

    dataFile.print(S.SamplePeriod);
    dataFile.print(" ");

    dataFile.print(S.DelayPeriod);
    dataFile.print(" ");

    dataFile.print(is_earlylick);
    dataFile.print(" ");

    dataFile.print(S.rule);
    dataFile.print(" ");

    dataFile.print(currStimu[0]);
    dataFile.print(" ");

    dataFile.print(currStimu[1]);
    dataFile.print(" ");

    dataFile.print(BaselineTrialFlag);
    dataFile.print(" ");
    dataFile.print(S.currProtocolTrials);
    dataFile.print(" ");
    dataFile.print(int(currProtocolPerf_corrected * 100));
    dataFile.print(" ");
    dataFile.print(SampleType);
    dataFile.print(" ");
    dataFile.print(EL_Favor);
    dataFile.print(" ");
    dataFile.print(CurrentELPunishEnabled);
    dataFile.print(" ");
    dataFile.print(BlockSwitchMode);
    dataFile.print(" ");
    dataFile.print(BlockSwitchStep);
    dataFile.print(" ");
    dataFile.print(RuleSwitchHintActive);
    dataFile.print(" ");
    dataFile.print(RuleSwitchCorrectCount);
    dataFile.print(" ");
    dataFile.print(TrialBlockOnset);
    dataFile.print(" ");
    dataFile.print(PendingProtocolIndex);
    dataFile.print(" ");
    if (TrialStartTimestampValid) {
      dataFile.print(TrialStartTimestampMs);
    } else {
      dataFile.print(-1);
    }
    dataFile.print(" ");

    // state_visited
    dataFile.print(trial_res.nVisited);

    for (int i = 0; i < trial_res.nVisited; i++) {
      dataFile.print(" ");
      dataFile.print(trial_res.stateVisited[i]);
      dataFile.print(" ");
      dataFile.print(trial_res.stateTimeStamps[i]);
    }
    dataFile.println();

    // Serial prinnt
    String TrialInfo_str = "Trial:";
    TrialInfo_str += String(S.currTrialNum);
    TrialInfo_str += " ";
    TrialInfo_str += String(TrialOutcome);
    TrialInfo_str += " ";
    TrialInfo_str += String(trial_res.nVisited);

    for (int i = 0; i < trial_res.nVisited; i++) {
      TrialInfo_str += " ";
      TrialInfo_str += String(trial_res.stateVisited[i]);
      TrialInfo_str += " ";
      TrialInfo_str += String(trial_res.stateTimeStamps[i]);
    }
    TrialInfo_str += " ";
    if (TrialInfo_str.length() > 2000) {
      Serial.println("E: TrialInfo_str is too long.");
    } else {
      TrialInfo_str.toCharArray(outBuffer, TrialInfo_str.length() + 1);
      Serial.println(outBuffer);
    }

  } else {
    Serial.println("E: error opening Trial.txt'.");
    dataFile.close();
    return -1;
  }
  dataFile.close();

  // Tevent.txt
  dataFile = SD.open("Tevent.txt", FILE_WRITE);
  if (dataFile) {
    dataFile.print(S.currTrialNum);
    dataFile.print(" ");
    dataFile.print(trial_res.nEvent);
    for (int i = 0; i < trial_res.nEvent; i++) {
      dataFile.print(" ");
      dataFile.print(trial_res.EventID[i]);
      dataFile.print(" ");
      dataFile.print(trial_res.eventTimeStamps[i]);
    }
    dataFile.println();
    // Serial output
    String TrialInfo_str = "Tevent:";
    TrialInfo_str += String(S.currTrialNum);
    TrialInfo_str += " ";
    TrialInfo_str += String(trial_res.nEvent);
    for (int i = 0; i < trial_res.nEvent; i++) {
      TrialInfo_str += " ";
      TrialInfo_str += String(trial_res.EventID[i]);
      TrialInfo_str += " ";
      TrialInfo_str += String(trial_res.eventTimeStamps[i]);
    }
    TrialInfo_str += " ";
    if (TrialInfo_str.length() > 2000) {
      Serial.println("E: TrialInfo_str is too long.");
    } else {
      TrialInfo_str.toCharArray(outBuffer, TrialInfo_str.length() + 1);
      Serial.println(outBuffer);
    }
  } else {
    Serial.println("E: error opening Tevent.txt");
    dataFile.close();
    return -1;
  }
  dataFile.close();
  return 0;
}

// Pure computation of TrialType without modifying globals
// Returns 0 if ambiguous (S2==0 under rule 1 or 2)
byte computeTrialType(int rule, float stimu1, float stimu2) {
  if (rule == 0) { // attend to stimu1 location
    if (stimu1 == -1)
      return 1; // left
    else if (stimu1 == 1)
      return 2;           // right
  } else if (rule == 1) { // attend to stimu2 freq
    if (stimu2 < 0)
      return 1; // left
    else if (stimu2 > 0)
      return 2; // right
    else
      return 0;           // ambiguous (S2==0)
  } else if (rule == 2) { // attend to stimu2 reversal freq
    if (stimu2 < 0)
      return 2; // right
    else if (stimu2 > 0)
      return 1; // left
    else
      return 0; // ambiguous (S2==0)
  }
  return 0;
}

void rule_define(int rule, float stimu1, float stimu2) {
  if (rule == 0) {      // attend to stimu1 location
    SampleType = 2;     // easiest trial type
    if (stimu1 == -1) { // left
      TrialType = 1;
    } else if (stimu1 == 1) { // right
      TrialType = 2;
    }
  } else if (rule == 1) { // attend to stimu2 freq
    if (stimu2 < 0) {
      TrialType = 1; // left
    } else if (stimu2 > 0) {
      TrialType = 2; // right
    } else {
      TrialType = (stimu1 == -1) ? 1 : 2;
    }

    SampleType = 2;
  } else if (rule == 2) { // attend to stimu2 reversal freq
    if (stimu2 < 0) {
      TrialType = 2;
    } else if (stimu2 > 0) {
      TrialType = 1;
    } else {
      TrialType = (stimu1 == -1) ? 2 : 1;
    }

    SampleType = 2;
  }
}

/***************************************Other
 * functions************************************************/
int sum_array(byte a[], int array_length) {
  int res = 0;
  for (int i = 0; i < array_length; i++) {
    res = res + a[i];
  }
  return res;
}

int compare_array_sum(byte array1[], byte oprant1, byte array2[], byte oprant2,
                      int start_ind, int end_ind) {
  int num = 0;
  for (int i = start_ind; i < end_ind; i++) {
    if (array1[i] == oprant1 && array2[i] == oprant2) {
      num++;
    }
  }
  return num;
}

int compare_array_sum(byte array1[], byte oprant1, int start_ind, int end_ind) {
  int num = 0;
  for (int i = start_ind; i < end_ind; i++) {
    if (array1[i] == oprant1) {
      num++;
    }
  }
  return num;
}

void free_reward(int rew_duration_ms) {
  smart.ManualOverride("DO1", 1); // override valve
  smart.ManualOverride("DO2", 1); // override valve
  delay(rew_duration_ms);
  smart.ManualOverride("DO1", 0);
  smart.ManualOverride("DO2", 0);
}

void system_output_check() {
  analogWriteFrequency(2, 3000);
  analogWrite(2, 128);
  analogWrite(5, 10);
  delay(200);
  analogWriteFrequency(2, 10000);
  analogWrite(2, 128);
  delay(200);
  analogWrite(2, 0);
  analogWrite(5, 0);
  delay(500);
  // middle
  analogWriteFrequency(3, 3000);
  analogWrite(3, 128);
  analogWrite(9, 10);
  delay(200);
  analogWriteFrequency(3, 10000);
  analogWrite(3, 128);
  delay(200);
  analogWrite(3, 0);
  analogWrite(9, 0);
  delay(500);

  // right
  analogWriteFrequency(16, 3000);
  analogWrite(16, 128);
  analogWrite(20, 10);
  delay(200);
  analogWriteFrequency(16, 10000);
  analogWrite(16, 128);
  delay(200);
  analogWrite(16, 0);
  analogWrite(20, 0);
  delay(500);
}

void upload_all_sd_files() {
  File root = SD.open("/");
  if (!root) {
    Serial.println("E: Could not open root directory.");
    return;
  }
  Serial.println("SD_UPLOAD_START");
  while (true) {
    File entry = root.openNextFile();
    if (!entry) {
      break; // No more files
    }
    if (!entry.isDirectory()) {
      Serial.print("SD_FILE_START:");
      Serial.print(entry.name());
      Serial.print(",");
      Serial.println(entry.size());

      while (entry.available()) {
        String line = entry.readStringUntil('\n');
        line.replace("\r", "");
        Serial.print("SD_DATA:");
        Serial.println(line);
        Serial.flush(); // Wait for data to transmit
        delay(2);       // Prevent python-side RX buffer overrun
      }
      Serial.print("SD_FILE_END:");
      Serial.println(entry.name());
      delay(5); // Small delay between files
    }
    entry.close();
  }
  Serial.println("SD_UPLOAD_END");
}
