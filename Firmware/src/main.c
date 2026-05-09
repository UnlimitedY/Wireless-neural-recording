/*
 * include
 */
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/sys/reboot.h>

#include <errno.h>

#include "..\RHD_Recording\RHDRecording.h"
#include "..\ESB_wireless\ESB_wireless.h"

#include "..\Sensor_recording\LC709204F.h"
#include "..\Sensor_recording\LSM6DS3.h"
#include "..\Sensor_recording\mp2710.h"

// for debugging
#include <zephyr/logging/log.h>
#include <math.h>
#include <string.h>
// for DSP
#include "..\RHD_Recording\OnlineFilter.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define LOG_MODULE_NAME LFP_Recording_peripherial
LOG_MODULE_REGISTER(LOG_MODULE_NAME);
/*
 * typedef
 */
typedef unsigned char u8_t;
typedef unsigned short u16_t;
typedef unsigned int u32_t;

#define BATTERY_LOW_VOLTAGE_MV           3250U
#define BATTERY_RECOVER_VOLTAGE_MV       3300U
#define BATTERY_LOW_HOLD_MS              100U
#define BATTERY_RECOVER_STABLE_MS        2000U
#define BATTERY_COMM_FAILURE_LIMIT       3U
#define POWER_GUARD_STATUS_FLAG_LOW_VBAT_HOLD       0x0001U
#define POWER_GUARD_STATUS_FLAG_RECOVERING          0x0002U
#define POWER_GUARD_STATUS_FLAG_REBOOT_PENDING      0x0004U
#define POWER_GUARD_STATUS_FLAG_SKIP_AUTO_RECOVERY  0x0008U
#define POWER_GUARD_STATUS_FLAG_AUTO_RECOVERY_DONE  0x0010U
#define WATCHDOG_TIMEOUT_MS              5000U
#define SENSOR_INIT_RETRY_COUNT          8U
#define SENSOR_INIT_RETRY_DELAY_MS       250U
#define RHD_INIT_RETRY_COUNT             3U
#define RHD_INIT_RETRY_DELAY_MS          20U
#define ESB_IDLE_TIMEOUT_MS              250U
#define ESB_TIMESTAMP_RETRY_COUNT        3U
#define REBOOT_CONTEXT_MAGIC             0xA5U
#define MODE0_POWER_TELEMETRY_PERIOD_MS  1000U
#define MODE0_BATTERY_POLL_PERIOD_MS     1000U
#define MODE0_POWER_FLAG_TX_POWER_MASK   0x000FU
#define MODE0_POWER_FLAG_RETX_SHIFT      4U
#define MODE0_POWER_FLAG_RETX_MASK       0x00F0U
#define ESB_MODE_CONFIG_COUNT            4U
#define ESB_TX_POWER_CODE_0DBM           0U
#define ESB_TX_POWER_CODE_4DBM           1U
#define ESB_TX_POWER_CODE_NEG4DBM        2U
#define ESB_TX_POWER_CODE_NEG8DBM        3U
#define ESB_TX_POWER_CODE_NEG12DBM       4U
#define ESB_TX_POWER_CODE_NEG16DBM       5U
#define ESB_TX_POWER_CODE_NEG20DBM       6U
#define ESB_TX_POWER_CODE_NEG40DBM       7U
#define ESB_RETRANSMIT_DELAY_DEFAULT_US  1200U
#define ESB_RETRANSMIT_DELAY_MIN_US      450U
#define ESB_RETRANSMIT_DELAY_MAX_US      4000U
#define ESB_NOACK_PERCENT_MAX            100U

typedef enum {
        POWER_GUARD_NORMAL = 0,
        POWER_GUARD_LOW_VBAT_HOLD,
        POWER_GUARD_RECOVERING,
        POWER_GUARD_REBOOT_PENDING,
} power_guard_state_t;

/*
* Recording channels number
* mode0 : 16 channels lfp 1Khz recording
* mode1 : 16 channel spike 20Khz recording; single raw data + 16 MUA
 * mode2 : 16 channel ~10.417Khz 8-bit raw data (v2); legacy 4 channel 20Khz wrapper remains available;
* mode3 : 16 channel ESA recording; 1Khz (downsampling from 12.5Khz) + MUA
*/
#define Channel_recorded 16
#define Channel_recorded_spike 16 // selected channel indices <= Channel_recorded
u8_t raw_channel[Channel_recorded_spike] = {0, 1, 2, 3, 4, 5, 6, 7 ,8 ,9 ,10 ,11 ,12 ,13 ,14 ,15}; 
u8_t recorded_channel_num = Channel_recorded_spike; // default: enable all channels

// nrf_gpio_pin_control_select
/*
 * extern marco define and struct define
 */
u16_t init_tx_buf[1];
u16_t init_rx_buf[1];

/***********************mode0********************* */
// for LFP 16 channels raw data
u16_t mode_0_m_tx_buf[LFP_TX_BUFFER_SIZE];
u16_t mode_0_m_rx_buf[2][LFP_RX_BUFFER_SIZE];
#define MODE0_DECIMATED_SAMPLES_PER_CHANNEL ((SAMPLE_POINT_NUM * TARGET_FS) / ORIGINAL_FS)
float32_t mode_0_input_buffer[Channel_recorded * SAMPLE_POINT_NUM * time_window];
int16_t mode_0_input_counts_buffer[Channel_recorded * SAMPLE_POINT_NUM * time_window];
float32_t mode_0_decimated_buffer[Channel_recorded * MODE0_DECIMATED_SAMPLES_PER_CHANNEL];
float32_t mode_0_mand_packet_buffer[MODE0_MAND_POINTS_PER_PACKET];
static bool mode0_accel_update_pending = false;
static bool mode0_status_update_pending = false;
static u8_t mode0_spi_chunk_count = 0;
static uint32_t mode0_packet_overflow_words = 0;
volatile bool mode0_processing_reset_request = false;
volatile bool mode0_power_reset_request = false;
volatile u8_t mode0_quant_bits = MODE0_QUANT_BITS_DEFAULT;
volatile u16_t mode0_quant_full_scale_uv = MODE0_QUANT_FULL_SCALE_UV_DEFAULT;

/***********************mode3********************* */
u16_t mode_3_m_tx_buf[MODE_3_TX_BUFFER_SIZE]; // ppi convert command; /**< TX buffer. */
u16_t mode_3_m_rx_buf[2][MODE_3_RX_BUFFER_SIZE]; /*< RX buffer. double buffer >*/
u16_t mode_2_m_tx_buf[MODE_3_TX_BUFFER_SIZE];
u16_t mode_2_m_rx_buf[2][MODE_3_RX_BUFFER_SIZE];

u16_t mode_3_array_t[MODE_3_SPI_RX_BUF_SIZE];
u16_t mode_3_array_lfp_t[MODE_3_LFP_SIZE];
u16_t mode_3_array_esa_t[MODE_3_ESA_SIZE];
// mode3 原始数据缓冲计数与设定：累计4个chunk后合并一次性发送
#define MODE3_RAW_BUFFER_CHUNKS 4
u8_t mode3_raw_chunk_count = 0; // 当前已缓冲的chunk数量（0~4）
u16_t mode_3_array_t_raw[Channel_recorded][CHUNK_SIZE * MODE3_RAW_BUFFER_CHUNKS]; // 做一个缓冲来最大话利用esb包的空间；
/***********************mode1, 2 spike********************* */
u16_t spike_m_tx_buf[SPIKE_TX_BUFFER_SIZE]; 
u16_t spike_m_rx_buf[2][SPIKE_RX_BUFFER_SIZE]; 

u16_t spike_channel_array_t[SPIKE_SAMPLE_POINT_NUM * time_window];
u16_t spike_channel_array[Channel_recorded_spike][SPIKE_SAMPLE_POINT_NUM * time_window];	

u16_t MutiUnitActivityArray[(SPIKE_SAMPLE_POINT_NUM / MUA_BIN_SIZE) * time_window]; 

#define MODE2_V2_PENDING_BUFFER_SAMPLES (MODE2_V2_PACKET_SAMPLES + CHUNK_SIZE)
static int8_t mode2_v2_pending_samples_buffer[MODE2_V2_PENDING_BUFFER_SAMPLES * MODE2_V2_CHANNELS];
static u16_t mode2_v2_pending_sample_count = 0;
static u16_t mode2_v2_packet_counter = 0;
static volatile uint32_t mode2_v2_tx_drop_counter = 0;
static bool mode2_v2_accel_pending = false;
static int16_t mode2_v2_pending_accel[3] = {0, 0, 0};

// spi instance init
const nrfx_spim_t spi = NRFX_SPIM_INSTANCE(SPI_INSTANCE);
const nrfx_spim_t spi_init = NRFX_SPIM_INSTANCE(SPI_INSTANCE_INIT); // for imu and rhd init

volatile uint32_t spi_overflow_flag = 0;

u16_t RHD_command = 0x00ff;

const u16_t NINE_DUMMPY[9] = {0x00ff, 0x00ff, 0x00ff, 0x00ff, 0x00ff, 0x00ff, 0x00ff, 0x00ff, 0x00ff};

// low power
const u16_t Register_config_lowpower[18] = {Register0_disable, lfp_Register1, lfp_Register2, lfp_Register3, lfp_Register4, 
                                        lfp_Register5, lfp_Register6, lfp_Register7, lfp_Register8, lfp_Register9, lfp_Register10, 
                                        lfp_Register11, lfp_Register12, lfp_Register13, lfp_Register14, lfp_Register15, 
                                        lfp_Register16, lfp_Register17};
/////////////////////////////////////// 1Khz sampling rate                                       
// mode 0
const u16_t Register_config_lfp[18] = {lfp_Register0_enable, lfp_Register1, lfp_Register2, lfp_Register3, lfp_Register4, 
                                        lfp_Register5, lfp_Register6, lfp_Register7, lfp_Register8, lfp_Register9, lfp_Register10, 
                                        lfp_Register11, lfp_Register12, lfp_Register13, lfp_Register14, lfp_Register15, 
                                        lfp_Register16, lfp_Register17};

/////////////////////////////////////// 20Khz sampling rate  
// mode 1
const u16_t Register_config_spike[18] = {spike_Register0_enable, spike_Register1, spike_Register2, spike_Register3, spike_Register4, 
                                        spike_Register5, spike_Register6, spike_Register7, spike_Register8, spike_Register9, 
                                        spike_Register10, spike_Register11, spike_Register12, spike_Register13, spike_Register14, 
                                        spike_Register15, spike_Register16, spike_Register17};
// mode 2
const u16_t Register_config_spike_raw[18] = {spike_Register0_enable, spike_Register1, spike_Register2, spike_Register3, 
        spike_raw_Register4, 
                                        spike_Register5, spike_Register6, spike_Register7, spike_Register8, spike_Register9, 
                                        spike_Register10, spike_Register11, spike_Register12, spike_Register13, spike_Register14, 
                                        spike_Register15, spike_Register16, spike_Register17};

/////////////////////////////////////// 12.5Khz sampling rate  
// mode 3
const u16_t Register_config_mode3[18] = {spike_Register0_enable, 
        spike_mode3_Register1, spike_mode3_Register2, spike_Register3, spike_mode3_Register4, 
                                        spike_Register5, spike_Register6, spike_Register7, spike_Register8, spike_Register9, 
                                        spike_Register10, spike_Register11, spike_Register12, spike_Register13, spike_Register14, 
                                        spike_Register15, spike_Register16, spike_Register17};
const u16_t Register_config_impedance[18] = {spike_Register0_enable,
        spike_Register1, spike_Register2, spike_Register3, impedance_Register4,
                                        spike_Register5, spike_Register6, spike_Register7, spike_Register8, spike_Register9,
                                        spike_Register10, spike_Register11, spike_Register12, spike_Register13, spike_Register14,
                                        spike_Register15, spike_Register16, spike_Register17};

const nrfx_gpiote_t gpiote_instance = NRFX_GPIOTE_INSTANCE(GPIOE_INST); 

const nrfx_timer_t RHD_timer_nRFX = NRFX_TIMER_INSTANCE(1);
const nrfx_timer_t SPI_timer_RESET = NRFX_TIMER_INSTANCE(3);

/********************************Sample rate**********************************/
bool mode_switch_flag = false; 
u8_t mode3_esa_reref_enable = MODE3_ESA_REREF_DISABLED; // default ReRef off
bool mode3_pending_mode_switch = false; // 延迟mode切换标志：等待raw chunk发送完毕
u16_t mode3_pending_mode = 0;           // 待切换的目标mode

u16_t sampe_mode = 0; // default mode0
uint32_t timer_period = 50; // default value 
uint32_t reset_ticks_value = MODE0_SPI_CHUNK_WORDS;

volatile bool spi_buff_flag = false;
volatile bool buffer_is_full = false;
volatile bool spi_ready_buff_flag = false;
volatile uint32_t spi_ready_overflow_flag = 0;
static uint32_t last_structured_spi_overflow_words = 0;

uint8_t gp_channel_1;
uint8_t gp_channel_2;
uint8_t gp_channel_3;

uint8_t gpiote_channel;
/*
 * hypo function
 */
#define reversebit(x, y) x ^= (1 << y)

u16_t tx_payload_wraped_num = 0; // count the number of wrapped tx packages

static int8_t mode2_v2_quantize_raw_uv(float32_t uv)
{
        if (uv > MODE2_V2_SCALE_UV) {
                uv = MODE2_V2_SCALE_UV;
        } else if (uv < -MODE2_V2_SCALE_UV) {
                uv = -MODE2_V2_SCALE_UV;
        }

        float32_t scaled = (uv * (float32_t)MODE2_V2_QUANT_MAX) / MODE2_V2_SCALE_UV;
        int32_t q = (int32_t)(scaled >= 0.0f ? scaled + 0.5f : scaled - 0.5f);
        if (q > MODE2_V2_QUANT_MAX) {
                q = MODE2_V2_QUANT_MAX;
        } else if (q < -MODE2_V2_QUANT_MAX) {
                q = -MODE2_V2_QUANT_MAX;
        }
        return (int8_t)q;
}

static void mode2_v2_reset(void)
{
        mode2_v2_pending_sample_count = 0;
        mode2_v2_packet_counter = 0;
        mode2_v2_tx_drop_counter = 0;
        mode2_v2_accel_pending = false;
        mode2_v2_pending_accel[0] = 0;
        mode2_v2_pending_accel[1] = 0;
        mode2_v2_pending_accel[2] = 0;
}

static int mode2_v2_append_chunk_and_flush(void)
{
        for (u16_t sample = 0; sample < CHUNK_SIZE; sample++) {
                if (mode2_v2_pending_sample_count >= MODE2_V2_PENDING_BUFFER_SAMPLES) {
                        mode2_v2_pending_sample_count = 0;
                }
                u16_t dst = (u16_t)(mode2_v2_pending_sample_count * MODE2_V2_CHANNELS);
                for (u16_t ch = 0; ch < MODE2_V2_CHANNELS; ch++) {
                        mode2_v2_pending_samples_buffer[dst + ch] =
                                mode2_v2_quantize_raw_uv(input_buffer[ch * CHUNK_SIZE + sample]);
                }
                mode2_v2_pending_sample_count++;
        }

        while (mode2_v2_pending_sample_count >= MODE2_V2_PACKET_SAMPLES) {
                bool include_accel = mode2_v2_accel_pending;
                int write_err = mode2_v2_tx_payload_wrap_8bit(
                        &mode2_v2_pending_samples_buffer[0],
                        MODE2_V2_PACKET_SAMPLES,
                        mode2_v2_packet_counter,
                        include_accel ? mode2_v2_pending_accel : NULL,
                        include_accel);
                if (write_err != 0) {
                        mode2_v2_tx_drop_counter++;
                }
                if (include_accel) {
                        mode2_v2_accel_pending = false;
                }
                mode2_v2_packet_counter++;

                u16_t remaining = (u16_t)(mode2_v2_pending_sample_count - MODE2_V2_PACKET_SAMPLES);
                if (remaining > 0) {
                        memmove(&mode2_v2_pending_samples_buffer[0],
                                &mode2_v2_pending_samples_buffer[MODE2_V2_PACKET_SAMPLES * MODE2_V2_CHANNELS],
                                remaining * MODE2_V2_CHANNELS * sizeof(mode2_v2_pending_samples_buffer[0]));
                }
                mode2_v2_pending_sample_count = remaining;
        }

        return 0;
}
/***********************for debugging and cue************************/ 
#define LED0_NODE DT_ALIAS(led0) // macro function of devicetree; test led

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(LED0_NODE, gpios);

void LED_hinting(uint32_t interval, uint32_t eventNum){
        // interval: 2000; 1000; 500; 200 
        u16_t events = 0;
        while(1)
        { 
                gpio_pin_toggle_dt(&led);
                k_sleep(K_MSEC(interval));
                events++;
                if(events >= 2*eventNum){
                        break;
                }
        }
}

/***********************for low-power setting************************/ 
k_tid_t mainThread;
// 记录 每一次 发送 数据包 允许休眠的时间
static u32_t timerecording; // 

/**************************system flag & command_flag & spike detection params**************************************/
// system flags
int RHD_err, err;

bool sample_switch = false; // 初始化程序的时候默认直接进入到采样lfp数据sample 的阶段
volatile bool remote_reboot_requested = false;
bool impedance_test_request = false;
bool impedance_test_active = false;
static uint32_t impedance_magnitude_ohm[Channel_recorded] = {0};
static int16_t impedance_phase_cdeg[Channel_recorded] = {0};

/*
 * Impedance test configuration — two-phase approach
 *
 * External RC low-pass filter between electrodes and RHD2132 inputs:
 *   R_series = 220 kOhm, C_shunt = 47 pF (per channel, including reference)
 *   RC cutoff ~ 15.4 kHz,  tau ~ 10.3 us
 *
 * The 47 pF shunt capacitor limits the maximum measurable impedance to
 * Z_47pF = 1/(2*pi*f*47pF).  At different test frequencies:
 *   f=1000 Hz : Z_47pF =  3.39 MOhm  (standard impedance, ceiling ~3 MOhm)
 *   f= 100 Hz : Z_47pF = 33.9  MOhm  (open-circuit check, ceiling ~30 MOhm)
 *
 * Phase 1: measure all channels at 1 kHz  (standard electrode impedance)
 * Phase 2: re-check channels near 1 kHz ceiling at 100 Hz to confirm
 *          whether electrode is high-Z or truly open/broken
 *
 * Open-circuit marker: magnitude = 0xFFFFFFFF
 * 220 kOhm series R adds ~440 kOhm systematic offset to all readings
 */
#define IMPEDANCE_TEST_CHANNEL_COUNT Channel_recorded
#define IMPEDANCE_TEST_CONVERT_OFFSET 8
#define IMPEDANCE_TEST_WAVE_POINTS 20
#define IMPEDANCE_TEST_SETTLE_CYCLES 5
#define IMPEDANCE_TEST_MEASURE_CYCLES 8
#define IMPEDANCE_TEST_TARGET_FREQ_HZ   1000U  /* primary: standard 1 kHz */
#define IMPEDANCE_OPENCHECK_FREQ_HZ     100U   /* secondary: open-circuit check */
#define IMPEDANCE_OPENCHECK_SUSPECT_OHM 2500000U  /* 1 kHz result > 2.5M -> suspect */
#define IMPEDANCE_OPENCHECK_CONFIRM_OHM 15000000U /* 100 Hz result > 15M -> open */
#define IMPEDANCE_OPEN_MARKER           0xFFFFFFFFU
#define IMPEDANCE_DIAGNOSTIC_LOG        1

/* Zcheck Register 5 values for each capacitor scale
 * Bit layout: (DACpower<<6)|(load<<5)|(scale<<3)|(connAll<<2)|(selPol<<1)|En
 * Common: DACpower=1, load=0, connAll=0, selPol=0, En=1 -> base 0x41 */
#define IMPEDANCE_ZCHECK_REG5_100FF  0x41  /* scale=00, Cs = 0.1 pF */
#define IMPEDANCE_ZCHECK_REG5_1PF    0x49  /* scale=01, Cs = 1.0 pF */
#define IMPEDANCE_ZCHECK_REG5_10PF   0x59  /* scale=11, Cs = 10  pF */

/* Auto-range: keep peak electrode voltage between 5% and 70% of ADC FS
 * RHD2132 input range ~ +/-6380 uV  (Vref / Gain = 1.225/192) */
#define IMPEDANCE_ADC_FS_UV  (RHD2132_ADC_REF_VOLTAGE_v / 192.0f * 1e6f)
#define IMPEDANCE_RANGE_LO   (IMPEDANCE_ADC_FS_UV * 0.05f)
#define IMPEDANCE_RANGE_HI   (IMPEDANCE_ADC_FS_UV * 0.70f)

static uint32_t impedance_spi_errors = 0;

static const uint8_t impedance_dac_wave[IMPEDANCE_TEST_WAVE_POINTS] = {
        128, 167, 202, 231, 249, 255, 249, 231, 202, 167,
        128,  89,  54,  25,   7,   1,   7,  25,  54,  89
};

// esb package head & control flags
u32_t packet_timestamp = 0; 
uint32_t timestamp_HABITS = 0;
uint32_t timestamp_baseline = 0;
uint32_t timestamp_LTNSRS = 0;

uint32_t packet_sent_counter[2] = {0, 0}; // success ; fail ; flag
volatile uint32_t esb_tx_attempt_counter = 0;
volatile uint32_t esb_tx_retransmit_counter = 0;
static u8_t esb_tx_power_code_by_mode[ESB_MODE_CONFIG_COUNT] = {
        ESB_TX_POWER_CODE_0DBM,
        ESB_TX_POWER_CODE_0DBM,
        ESB_TX_POWER_CODE_0DBM,
        ESB_TX_POWER_CODE_0DBM,
};
static u8_t esb_retransmit_count_by_mode[ESB_MODE_CONFIG_COUNT] = {1, 3, 1, 3};
static u16_t esb_retransmit_delay_us_by_mode[ESB_MODE_CONFIG_COUNT] = {
        ESB_RETRANSMIT_DELAY_DEFAULT_US,
        ESB_RETRANSMIT_DELAY_DEFAULT_US,
        ESB_RETRANSMIT_DELAY_DEFAULT_US,
        ESB_RETRANSMIT_DELAY_DEFAULT_US,
};
static u8_t esb_noack_percent_by_mode[ESB_MODE_CONFIG_COUNT] = {0, 0, 0, 0};
static u16_t esb_noack_accum_by_mode[ESB_MODE_CONFIG_COUNT] = {0, 0, 0, 0};
static volatile bool runtime_esb_mode_config_update_pending = false;
static uint8_t current_esb_tx_power_code = ESB_TX_POWER_CODE_0DBM;
static uint8_t current_esb_retransmit_count = 1;
static u16_t mode0_power_telemetry[MODE0_COMPACT_POWER_WORDS] = {0, 0};
static uint16_t mode0_power_telemetry_seq = 0;
static uint32_t mode0_power_last_snapshot_ms = 0;
static uint32_t mode0_power_last_tx_attempt_total = 0;
static uint32_t mode0_power_last_tx_retransmit_total = 0;
static uint32_t mode0_power_sleep_enter_cycle = 0;
static uint32_t mode0_power_wake_cycle = 0;
static uint32_t mode0_power_sleep_us_accum = 0;
static uint32_t mode0_power_active_us_accum = 0;
static uint16_t mode0_power_sleep_samples = 0;
static uint16_t mode0_power_active_samples = 0;
static bool mode0_power_sleep_timing_active = false;
static bool mode0_power_active_timing_active = false;

bool overflow_signal = 0;
bool sensor_update_flag = 0;
uint64_t stamp_check = 0;

// fixed for mode 2
u8_t spike_raw_channel[4] = {0, 1, 2, 3}; // mode 2

// selectable for mode 1
u8_t recorded_spike_channel = 0; // default: enable channel 0; mode 1

// neural recording for mode 0
/* SPI slot -> physical RHD2132 channel, compensating the 2-command conversion latency. */
u8_t channel_order[Channel_recorded] = {14, 15, 0, 1, 2, 3, 4, 5, 6, 7 ,8 ,9 ,10 ,11 ,12 ,13}; 
/********************************sensor setup**********************************/
const nrfx_twim_t lc_twim = NRFX_TWIM_INSTANCE(LC_INSTANCE_ID);
const nrfx_spim_t lsm_spi = NRFX_SPIM_INSTANCE(LSM_INSTANCE_ID);
uint8_t twimWriteDataBuffer[TWI_MAX_NUM_TX_BYTES];
uint8_t LCDataBuffer[6];

/************************** ESB defination **************************************/
uint8_t bitrate = 1; // 1 : 1mbps; 2: 2mbps
uint8_t rf_channel = 5; // index of rf_ channel
uint8_t active_rf_channel = 84; // current ESB RF channel value
uint8_t advise_channel = 0;
uint8_t rf_channel_list[rf_channel_num] = {2, 17, 33, 50, 67, 84}; // aviod conflict with WiFi
uint8_t rf_channel_rssi_list[rf_channel_num] = {0};

struct esb_payload rx_payload; // command & behavioral events --rx
struct esb_payload tx_payload; // neural signal -- tx

struct esb_payload empty_payload; // when sampling disable 
struct esb_payload timestamp_payload; // neural signal alignment required

u32_t last_statistic_timestamp;
uint8_t sample_watch_dog = 0;
static volatile bool runtime_rf_channel_switch_pending = false;
static uint8_t pending_runtime_rf_channel = 84;
static uint8_t pending_runtime_rf_channel_index = 5;

/***********************************TWI Setup function******************************************/
/*
* 通过IMU来读取一定sample rate的3轴数据，在单次包的发送中，将该数据放入并一起发送到上位机；
*/
int16_t imu_data[6];
int16_t lc_data[3];

struct IMU_settings settings;

// EN_HIZ; CEB; shipping
#define DEFAULT_IMU_MODE_ACCEL 2U
u8_t IMU_init[2] = {DEFAULT_IMU_MODE_ACCEL, 0}; //mode index: low-power; ACCEL; ACCEL + GYRO;    update_flag

static power_guard_state_t power_guard_state = POWER_GUARD_NORMAL;
static uint32_t low_voltage_since_ms = 0;
static uint32_t recover_voltage_since_ms = 0;
static uint8_t battery_comm_failures = 0;
static bool auto_recovery_attempted = false;
static bool skip_auto_recovery_this_boot = false;
volatile u16_t power_guard_status_word = POWER_GUARD_NORMAL;
volatile u16_t power_guard_low_stop_counter = 0;
volatile u16_t power_guard_status_flags = 0;
static bool radio_init_complete = false;
static bool rhd_pipeline_ready = false;
static int wdt_channel_id = -1;
static bool watchdog_ready = false;
static const struct device *wdt_dev = DEVICE_DT_GET(DT_NODELABEL(wdt));

static void low_power(void);

static void power_guard_publish_status(void)
{
        u16_t flags = 0;

        if (power_guard_state == POWER_GUARD_LOW_VBAT_HOLD) {
                flags |= POWER_GUARD_STATUS_FLAG_LOW_VBAT_HOLD;
        }
        if (power_guard_state == POWER_GUARD_RECOVERING) {
                flags |= POWER_GUARD_STATUS_FLAG_RECOVERING;
        }
        if (power_guard_state == POWER_GUARD_REBOOT_PENDING) {
                flags |= POWER_GUARD_STATUS_FLAG_REBOOT_PENDING;
        }
        if (skip_auto_recovery_this_boot) {
                flags |= POWER_GUARD_STATUS_FLAG_SKIP_AUTO_RECOVERY;
        }
        if (auto_recovery_attempted) {
                flags |= POWER_GUARD_STATUS_FLAG_AUTO_RECOVERY_DONE;
        }

        power_guard_status_word = (u16_t)power_guard_state;
        power_guard_status_flags = flags;
}

static void watchdog_feed_if_ready(void)
{
        if (!watchdog_ready) {
                return;
        }
        (void)wdt_feed(wdt_dev, wdt_channel_id);
}

static bool watchdog_startup_init(void)
{
        struct wdt_timeout_cfg config = {
                .window = {
                        .min = 0U,
                        .max = WATCHDOG_TIMEOUT_MS,
                },
                .callback = NULL,
                .flags = WDT_FLAG_RESET_SOC,
        };

        if (!device_is_ready(wdt_dev)) {
                return false;
        }

        wdt_channel_id = wdt_install_timeout(wdt_dev, &config);
        if (wdt_channel_id < 0) {
                wdt_channel_id = -1;
                return false;
        }

        if (wdt_setup(wdt_dev, 0) != 0) {
                wdt_channel_id = -1;
                return false;
        }

        watchdog_ready = true;
        return true;
}

static void detect_boot_reset_context(void)
{
        uint32_t reset_reason = NRF_POWER->RESETREAS;
        uint8_t gpregret = NRF_POWER->GPREGRET;

        if (((reset_reason & POWER_RESETREAS_DOG_Msk) != 0U) ||
            (gpregret == REBOOT_CONTEXT_MAGIC)) {
                skip_auto_recovery_this_boot = true;
        }

        if (reset_reason != 0U) {
                NRF_POWER->RESETREAS = reset_reason;
        }
        NRF_POWER->GPREGRET = 0U;
        power_guard_publish_status();
}

static void request_cold_reboot(void)
{
        NRF_POWER->GPREGRET = REBOOT_CONTEXT_MAGIC;
        power_guard_state = POWER_GUARD_REBOOT_PENDING;
        power_guard_publish_status();
}

static void perform_pending_reboot(bool sampling_active)
{
        if (power_guard_state != POWER_GUARD_REBOOT_PENDING) {
                return;
        }

        sample_switch = false;
        if (sampling_active && rhd_pipeline_ready) {
                timer_stop();
        }
        if (rhd_pipeline_ready) {
                low_power();
        }
        if (radio_init_complete) {
                esb_flush_tx();
                esb_flush_rx();
        }

        watchdog_feed_if_ready();
        k_sleep(K_MSEC(30));
        sys_reboot(SYS_REBOOT_COLD);
}

static void process_remote_reboot_request(bool sampling_active)
{
        if (!remote_reboot_requested) {
                return;
        }
        remote_reboot_requested = false;
        request_cold_reboot();
        perform_pending_reboot(sampling_active);
}

static void clear_voltage_recovery_tracking(void)
{
        low_voltage_since_ms = 0U;
        recover_voltage_since_ms = 0U;
        auto_recovery_attempted = false;
        power_guard_publish_status();
}

static void cancel_auto_recovery_attempt(void)
{
        sample_switch = false;
        power_guard_state = POWER_GUARD_LOW_VBAT_HOLD;
        recover_voltage_since_ms = 0U;
        auto_recovery_attempted = true;
        power_guard_publish_status();
}

static bool battery_gauge_soft_recover(void)
{
        uint16_t chip_id = 0;

        if (twim_init() != NRFX_SUCCESS) {
                return false;
        }

        watchdog_feed_if_ready();
        k_sleep(K_MSEC(20));

        if (getChipID(&chip_id) != NRFX_SUCCESS || chip_id != 0x001eU) {
                return false;
        }
        if (LC_operate() != NRFX_SUCCESS) {
                return false;
        }
        if (LC_init() != NRFX_SUCCESS) {
                return false;
        }

        return true;
}

static void power_guard_process_voltage(uint16_t battery_mv, bool sampling_active)
{
        uint32_t now = k_uptime_get_32();

        if (battery_mv < BATTERY_LOW_VOLTAGE_MV) {
                recover_voltage_since_ms = 0U;
                if (!sampling_active) {
                        auto_recovery_attempted = false;
                }
                if (sampling_active && power_guard_state == POWER_GUARD_NORMAL) {
                        if (low_voltage_since_ms == 0U) {
                                low_voltage_since_ms = now;
                        } else if ((uint32_t)(now - low_voltage_since_ms) >= BATTERY_LOW_HOLD_MS) {
                                sample_switch = false;
                                if (power_guard_state != POWER_GUARD_LOW_VBAT_HOLD) {
                                        power_guard_low_stop_counter++;
                                }
                                power_guard_state = POWER_GUARD_LOW_VBAT_HOLD;
                                auto_recovery_attempted = false;
                                power_guard_publish_status();
                                LOG_INF("Battery low %u mV, stop sampling", battery_mv);
                        }
                } else {
                        low_voltage_since_ms = now;
                        if (power_guard_state == POWER_GUARD_RECOVERING) {
                                power_guard_low_stop_counter++;
                                power_guard_state = POWER_GUARD_LOW_VBAT_HOLD;
                                power_guard_publish_status();
                        }
                }
                return;
        }

        low_voltage_since_ms = 0U;
        if (power_guard_state != POWER_GUARD_LOW_VBAT_HOLD) {
                return;
        }
        if (battery_mv <= BATTERY_RECOVER_VOLTAGE_MV) {
            recover_voltage_since_ms = 0U;
            return;
        }
        if (recover_voltage_since_ms == 0U) {
                recover_voltage_since_ms = now;
                return;
        }
        if (auto_recovery_attempted || skip_auto_recovery_this_boot) {
                return;
        }
        if ((uint32_t)(now - recover_voltage_since_ms) < BATTERY_RECOVER_STABLE_MS) {
                return;
        }

        sampe_mode = 0;
        mode_switch_flag = false;
        sample_switch = true;
        auto_recovery_attempted = true;
        power_guard_state = POWER_GUARD_RECOVERING;
        power_guard_publish_status();
        LOG_INF("Battery recovered %u mV, auto resume mode0", battery_mv);
}

static bool BatteryPower_Temp_Read(u16_t *data)
{
        uint16_t latest_data[3] = {0};
        uint16_t charging = 0;

        if (LC_getRSOC(&latest_data[0]) != NRFX_SUCCESS) {
                return false;
        }

        Charging_PG_PG_STAT_get(&charging);
        latest_data[1] = charging;

        if (LC_getCellVoltage(&latest_data[2]) != NRFX_SUCCESS) {
                return false;
        }

        memcpy(data, latest_data, sizeof(latest_data));
        return true;
}

static bool power_guard_refresh_battery(bool sampling_active)
{
        u16_t latest_lc_data[3];

        if (!BatteryPower_Temp_Read(latest_lc_data)) {
                if (!battery_gauge_soft_recover() || !BatteryPower_Temp_Read(latest_lc_data)) {
                        battery_comm_failures++;
                        if (battery_comm_failures >= BATTERY_COMM_FAILURE_LIMIT) {
                                request_cold_reboot();
                        }
                        power_guard_publish_status();
                        return false;
                }
        }

        battery_comm_failures = 0;
        memcpy(lc_data, latest_lc_data, sizeof(latest_lc_data));
        power_guard_process_voltage((uint16_t)lc_data[2], sampling_active);
        power_guard_publish_status();
        return true;
}

static bool sampling_start_allowed(void)
{
        if ((uint16_t)lc_data[2] < BATTERY_LOW_VOLTAGE_MV) {
                return false;
        }
        if (power_guard_state == POWER_GUARD_LOW_VBAT_HOLD ||
            power_guard_state == POWER_GUARD_RECOVERING) {
                return ((uint16_t)lc_data[2] > BATTERY_RECOVER_VOLTAGE_MV);
        }
        return true;
}

static bool wait_for_esb_idle(uint32_t timeout_ms)
{
        uint32_t start_ms = k_uptime_get_32();

        while (!esb_is_idle()) {
                watchdog_feed_if_ready();
                if ((uint32_t)(k_uptime_get_32() - start_ms) >= timeout_ms) {
                        return false;
                }
                k_sleep(K_MSEC(1));
        }

        return true;
}

static bool find_rf_channel_index(uint8_t requested_channel, uint8_t *index_out)
{
        for (uint8_t i = 0; i < rf_channel_num; i++) {
                if (rf_channel_list[i] == requested_channel) {
                        if (index_out != NULL) {
                                *index_out = i;
                        }
                        return true;
                }
        }
        return false;
}

void request_runtime_rf_channel_switch(uint8_t requested_channel)
{
        uint8_t requested_index = 0;

        if (!find_rf_channel_index(requested_channel, &requested_index)) {
                return;
        }

        pending_runtime_rf_channel = requested_channel;
        pending_runtime_rf_channel_index = requested_index;
        runtime_rf_channel_switch_pending = true;
}

static void process_runtime_rf_channel_switch(void)
{
        if (!runtime_rf_channel_switch_pending) {
                return;
        }

        if (!wait_for_esb_idle(ESB_IDLE_TIMEOUT_MS)) {
                return;
        }

        esb_flush_tx();
        esb_flush_rx();
        esb_set_rf_channel(pending_runtime_rf_channel);
        active_rf_channel = pending_runtime_rf_channel;
        rf_channel = pending_runtime_rf_channel_index;
        advise_channel = pending_runtime_rf_channel_index;
        runtime_rf_channel_switch_pending = false;
}

static u16_t clamp_esb_retransmit_delay_us(u16_t delay_us)
{
        if (delay_us < ESB_RETRANSMIT_DELAY_MIN_US) {
                return ESB_RETRANSMIT_DELAY_MIN_US;
        }
        if (delay_us > ESB_RETRANSMIT_DELAY_MAX_US) {
                return ESB_RETRANSMIT_DELAY_MAX_US;
        }
        return delay_us;
}

static u8_t clamp_esb_noack_percent(u8_t noack_percent)
{
        if (noack_percent > ESB_NOACK_PERCENT_MAX) {
                return ESB_NOACK_PERCENT_MAX;
        }
        return noack_percent;
}

void request_runtime_esb_mode_config(u8_t mode, u8_t tx_power_code, u8_t retransmit_count,
                                     u16_t retransmit_delay_us, u8_t noack_percent)
{
        if (mode >= ESB_MODE_CONFIG_COUNT || tx_power_code > ESB_TX_POWER_CODE_NEG40DBM) {
                return;
        }

        esb_tx_power_code_by_mode[mode] = tx_power_code;
        esb_retransmit_count_by_mode[mode] = retransmit_count & 0x0FU;
        esb_retransmit_delay_us_by_mode[mode] = clamp_esb_retransmit_delay_us(retransmit_delay_us);
        esb_noack_percent_by_mode[mode] = clamp_esb_noack_percent(noack_percent);
        esb_noack_accum_by_mode[mode] = 0U;
        if (mode == (u8_t)sampe_mode) {
                runtime_esb_mode_config_update_pending = true;
        }
}

void request_runtime_esb_current_mode_apply(void)
{
        runtime_esb_mode_config_update_pending = true;
}

static u8_t clamp_mode0_quant_bits(u8_t bit_depth)
{
        if (bit_depth < MODE0_QUANT_BITS_MIN) {
                return MODE0_QUANT_BITS_MIN;
        }
        if (bit_depth > MODE0_QUANT_BITS_MAX) {
                return MODE0_QUANT_BITS_MAX;
        }
        return bit_depth;
}

static u16_t clamp_mode0_quant_full_scale_uv(u16_t full_scale_uv)
{
        u16_t value = (full_scale_uv == 0U) ? MODE0_QUANT_FULL_SCALE_UV_DEFAULT : full_scale_uv;
        if (value < MODE0_QUANT_FULL_SCALE_UV_MIN) {
                value = MODE0_QUANT_FULL_SCALE_UV_MIN;
        } else if (value > MODE0_QUANT_FULL_SCALE_UV_MAX) {
                value = MODE0_QUANT_FULL_SCALE_UV_MAX;
        }

        const u16_t offset = (u16_t)(value - MODE0_QUANT_FULL_SCALE_UV_MIN);
        value = (u16_t)(MODE0_QUANT_FULL_SCALE_UV_MIN +
                        ((offset + (MODE0_QUANT_FULL_SCALE_UV_STEP / 2U)) /
                         MODE0_QUANT_FULL_SCALE_UV_STEP) *
                        MODE0_QUANT_FULL_SCALE_UV_STEP);
        if (value > MODE0_QUANT_FULL_SCALE_UV_MAX) {
                value = MODE0_QUANT_FULL_SCALE_UV_MAX;
        }
        return value;
}

void request_mode0_quant_config(u8_t bit_depth, u16_t full_scale_uv)
{
        mode0_quant_bits = clamp_mode0_quant_bits(bit_depth);
        mode0_quant_full_scale_uv = clamp_mode0_quant_full_scale_uv(full_scale_uv);
        mode0_processing_reset_request = true;
}

static int set_esb_tx_power_code_tracked(u8_t tx_power_code)
{
        int err = 0;

        switch (tx_power_code) {
        case ESB_TX_POWER_CODE_4DBM:
                err = esb_set_tx_power(ESB_TX_POWER_4DBM);
                break;
        case ESB_TX_POWER_CODE_NEG4DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG4DBM);
                break;
        case ESB_TX_POWER_CODE_NEG8DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG8DBM);
                break;
        case ESB_TX_POWER_CODE_NEG12DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG12DBM);
                break;
        case ESB_TX_POWER_CODE_NEG16DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG16DBM);
                break;
        case ESB_TX_POWER_CODE_NEG20DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG20DBM);
                break;
        case ESB_TX_POWER_CODE_NEG40DBM:
                err = esb_set_tx_power(ESB_TX_POWER_NEG40DBM);
                break;
        case ESB_TX_POWER_CODE_0DBM:
        default:
                tx_power_code = ESB_TX_POWER_CODE_0DBM;
                err = esb_set_tx_power(ESB_TX_POWER_0DBM);
                break;
        }
        if (err == 0) {
                current_esb_tx_power_code = tx_power_code;
        }
        return err;
}

static void set_esb_retransmit_count_tracked(uint8_t retransmit_count)
{
        esb_set_retransmit_count(retransmit_count);
        current_esb_retransmit_count = retransmit_count;
}

static void set_esb_retransmit_delay_tracked(u16_t retransmit_delay_us)
{
        retransmit_delay_us = clamp_esb_retransmit_delay_us(retransmit_delay_us);
        esb_set_retransmit_delay(retransmit_delay_us);
}

static void apply_esb_mode_config(u8_t mode)
{
        if (mode >= ESB_MODE_CONFIG_COUNT) {
                mode = 0;
        }
        (void)set_esb_tx_power_code_tracked(esb_tx_power_code_by_mode[mode]);
        set_esb_retransmit_count_tracked(esb_retransmit_count_by_mode[mode]);
        set_esb_retransmit_delay_tracked(esb_retransmit_delay_us_by_mode[mode]);
}

bool esb_data_payload_should_noack(void)
{
        u8_t mode = (u8_t)sampe_mode;
        if (mode >= ESB_MODE_CONFIG_COUNT) {
                mode = 0;
        }

        u8_t percent = esb_noack_percent_by_mode[mode];
        if (percent == 0U) {
                return false;
        }
        if (percent >= ESB_NOACK_PERCENT_MAX) {
                return true;
        }

        esb_noack_accum_by_mode[mode] += percent;
        if (esb_noack_accum_by_mode[mode] >= ESB_NOACK_PERCENT_MAX) {
                esb_noack_accum_by_mode[mode] -= ESB_NOACK_PERCENT_MAX;
                return true;
        }
        return false;
}

static void process_runtime_esb_mode_config_update(void)
{
        if (!runtime_esb_mode_config_update_pending) {
                return;
        }
        if (!wait_for_esb_idle(ESB_IDLE_TIMEOUT_MS)) {
                return;
        }
        apply_esb_mode_config((u8_t)sampe_mode);
        runtime_esb_mode_config_update_pending = false;
}

static bool timestamp_handshake_start(void)
{
        bool success = false;

        if (!wait_for_esb_idle(ESB_IDLE_TIMEOUT_MS)) {
                return false;
        }

        k_sleep(K_MSEC(1000));
        watchdog_feed_if_ready();
        esb_flush_tx();
        set_esb_retransmit_count_tracked(0);

        for (uint8_t attempt = 0; attempt < ESB_TIMESTAMP_RETRY_COUNT; attempt++) {
                packet_sent_counter[1] = 0;
                if (timestamp_payload_wrap() == 0 &&
                    wait_for_esb_idle(ESB_IDLE_TIMEOUT_MS) &&
                    packet_sent_counter[1] == 0) {
                        success = true;
                        break;
                }

                esb_flush_tx();
                advise_channel++;
                if (advise_channel >= sizeof(rf_channel_list)) {
                        advise_channel = 0;
                }
                watchdog_feed_if_ready();
        }

        apply_esb_mode_config((u8_t)sampe_mode);
        esb_set_rf_channel(active_rf_channel);
        return success;
}

void IMU_mode_switch(void){
        if(sampe_mode == 2 && (IMU_init[0] == 2 || IMU_init[0] == 3)){
        settings.accel_enable = 1;
        settings.gyro_enable = 0;
        err = LSM6DS3_init();
        return;
        }
        if(IMU_init[0] == 1){
        settings.accel_enable = 0;
        settings.gyro_enable = 0;
        }
        else if(IMU_init[0] == 2){
        settings.accel_enable = 1;
        settings.gyro_enable = 0;
        }
        else if(IMU_init[0] == 3){
        settings.accel_enable = 1;
        settings.gyro_enable = 1;
        }else{
        // default
        settings.accel_enable = 0;
        settings.gyro_enable = 0;
        }

        err = LSM6DS3_init();
}

static bool setup_sensor(void){ // 注意，nrf 通过内部上拉来驱动 lsm 会导致 发热问题，可能是gpio 上拉电流过大
	int err;
        uint16_t lc_id = 0;
        // init lsm spim
        err= lsm_spim_init();
        if (err != 0) {
                return false;
        }
        k_sleep(K_MSEC(500));
        LOG_INF("lsmspi init %d \n" ,err);
	// 初始化 LSM
	for (uint8_t attempt = 0; attempt < SENSOR_INIT_RETRY_COUNT; attempt++) {
                err = LSM6DS3_who_am_i();
                if(err == -1){
                        break;
                }
                LOG_INF("%d \n" ,err);
                watchdog_feed_if_ready();
                LED_hinting(200, 2);
                k_sleep(K_MSEC(SENSOR_INIT_RETRY_DELAY_MS));
	}
	if(err != -1){
		LOG_INF("LSM init failed\n");
                return false;
        }
        LOG_INF("LSM init success\n");
        settings.accel_enable = 0;
        settings.gyro_enable = 0; 
	err = LSM6DS3_init(); // 初始化为 低功耗模式，全部关闭
        if (err != 0) {
                return false;
        }
        /******* for LC *******/
        for (uint8_t attempt = 0; attempt < SENSOR_INIT_RETRY_COUNT; attempt++) {
                if (battery_gauge_soft_recover() &&
                    getChipID(&lc_id) == NRFX_SUCCESS &&
                    lc_id == 0x001eU) {
                        break;
                }
                watchdog_feed_if_ready();
                LED_hinting(500, 2);
                LOG_INF("LC battery init failed %x \n" ,lc_id);
                k_sleep(K_MSEC(SENSOR_INIT_RETRY_DELAY_MS));
	}
        if (lc_id != 0x001eU) {
                return false;
        }
        // charge indiciator init
        Charging_stats_init();
        return true;
}

void LSM6DS3_Read(void){ // only recording 3-axis
        /*
        * 记录 6-axis的值；关闭 角加速度测量；注意：在lsm_init中要对应开启角加速度mode和配置寄存器
        */
       if(IMU_init[0] == 2){
                LSM6DS3_read_accl_data();
       }
        else if(IMU_init[0] == 3){
                LSM6DS3_read_accl_data();
                LSM6DS3_read_gyro_data();
        }
}

static void LSM6DS3_Read_AccelOnly(void)
{
        LSM6DS3_read_accl_data();
}

/************************** main-used function **************************************/
int filter_init(void){
        arm_status status;
        //////////////////////// For LFP 2 order IIR low pass filter Fc = 300Hz
        status = init_lowpass_filter_LFP(IIR_ORDER_lowpass_LFP, IIR_CUTOFF_lowpass_LFP, ORIGINAL_FS);
        if (status != ARM_MATH_SUCCESS) {
                LOG_INF("Error: LFP low-pass filter initialization failed\n");
                return -1;
        }
        
        //////////////////////// For ESA
         // Initialize 1th order low-pass filter at 150Hz
        status = init_lowpass_filter_ESA(IIR_ORDER_lowpass_ESA, IIR_CUTOFF_lowpass_ESA, ORIGINAL_FS);
        if (status != ARM_MATH_SUCCESS) {
                LOG_INF("Error: ESA Low-pass filter initialization failed\n");
                return -1;
        }
        
        // Initialize 1th order high-pass filter at 250Hz
        status = init_highpass_filter_ESA(IIR_ORDER_highpass_ESA, IIR_CUTOFF_highpass_ESA, ORIGINAL_FS);
        if (status != ARM_MATH_SUCCESS) {
                LOG_INF("Error: ESA High-pass filter initialization failed\n");
                return -1;
        }

        LOG_INF("Filter initialization completed successfully\n");
        return 0;
}

static bool init_everything(void){
        /*************LED setup***************/
	if (!gpio_is_ready_dt(&led)) {
                LOG_INF("failed");
		return false;
	}
	err = gpio_pin_configure_dt(&led, GPIO_OUTPUT_ACTIVE);
	if (err < 0) {
		return false;
	}

        // // /****************spi_transmit & spi_init init******************/     
        err = spim_init();
        if(err != 0){
                LOG_INF("fail SPI init %d" , err);
                LOG_INF("%d" , spi.p_reg->FREQUENCY);
                return false;
        }

        /**************** lsm & lc: twi init ******************/ 
        memset(imu_data, 0, sizeof(imu_data));
        memset(lc_data, 0, sizeof(lc_data));
        
        if (!setup_sensor()) { // only accel; 208Hz
                return false;
        }
        /**************** ESB init ******************/     
        err = clocks_start();
	if (err)
	{
                LOG_INF("fail ESB clock init %d" , err);
		return false;
	}
        err = esb_initialize();
	if (err)
	{
                LOG_INF("fail ESB init %d" , err);
		return false;
	}

        empty_payload.length = 10;
        timestamp_payload.length = 12;

        tx_payload.pipe = 0; // using the pipe 0
        empty_payload.pipe = 0;
        timestamp_payload.pipe = 0;

        esb_flush_tx(); 
	esb_flush_rx();
        radio_init_complete = true;
        return true;
}

static bool init_RHD(void){
        RHD_err = -1;

        for (uint8_t attempt = 0; attempt < RHD_INIT_RETRY_COUNT; attempt++) {
                rhdspi_init();
                if(sampe_mode == 0){// mode0 shares mode3 12.5kHz sampling/register setup
                        timer_period = 5;
                        reset_ticks_value = MODE0_SPI_CHUNK_WORDS;
                        RHD_err = RHD_init(Register_config_mode3);
                        
                }else if(sampe_mode == 1){ // 20Khz 16 channels: xx ms per packets
                        timer_period = 1000 / 20 / Channel_recorded_spike; // 5: 12500 Hz ; 6: 10417 Hz ;  3: 20833 Hz; 4: 15625 Hz (0 dummy)
                        reset_ticks_value = SPIKE_SPI_RX_BUF_SIZE;
                        RHD_err = RHD_init(Register_config_spike);
                }else if(sampe_mode == 2){
                        // mode2 v2 keeps the shared 16-channel raw buffer path, sampled at ~10.417kHz.
                        timer_period = 6;
                        reset_ticks_value = MODE_3_SPI_RX_BUF_SIZE;
                        mode2_v2_reset();
                        RHD_err = RHD_init(Register_config_mode3);
                }else if(sampe_mode == 3){
                        // spike 12.5khz + 1khz lfp
                        timer_period = 5;  // 1000 / 12.5 / Channel_recorded
                        reset_ticks_value = MODE_3_SPI_RX_BUF_SIZE;
                        RHD_err = RHD_init(Register_config_mode3);
                }

                if (!RHD_err) {
                        break;
                }

                watchdog_feed_if_ready();
                k_sleep(K_MSEC(RHD_INIT_RETRY_DELAY_MS));
        }

        /****************RHD init******************/ // using cs gpiote task
        if (RHD_err)
        {
                LOG_INF("failed RHD init %x" , RHD_err);
                return false;
        }else{
                LOG_INF("RHD init success");
        }

        nrfx_spim_uninit(&spi_init); // for low-power

        /****************PPI nerual recording init******************/
        err = timer_init(timer_period, reset_ticks_value);
        if(err != 0){
                LOG_INF("failed timer init %d" , err);
		return false; 
        }
        err = CS_Gpiote_init();// 注意，之后无法再直接使用cs来做spi传输
        if(err != 0){
                LOG_INF("failed gpiote init %d" , err);
		return false; 
        }
        err = ppi_init();
        if(err != 0){
                LOG_INF("failed ppi init %d" , err);
		return false; 
        }

        rhd_pipeline_ready = true;
        LOG_INF("success all init");
        return true;
}

static void low_power(void){
        // RHD
        nrfx_gpiote_out_task_disable(&gpiote_instance, NRFX_SPIM_SS_PIN); // 避免两个spi的冲突
        rhdspi_init();
        RHD_err = RHD_init(Register_config_lowpower);
        nrfx_spim_uninit(&spi_init); // for low-power
        nrfx_gppi_channels_disable(BIT(gp_channel_1));
	nrfx_gppi_channels_disable(BIT(gp_channel_2));
        // LSM
        settings.accel_enable = 0;
        settings.gyro_enable = 0; 
        err = LSM6DS3_init();
}

static void update_impedance_timestamp(void)
{
        packet_timestamp = k_uptime_get_32();
        timestamp_LTNSRS = packet_timestamp;
}

static float adc_raw_to_uv(u16_t raw_sample)
{
        u16_t sample = swapShort16(raw_sample);
        return ((float)sample * scale_factor - RHD2132_ADC_REF_VOLTAGE);
}

static float wrap_phase_deg(float phase_deg)
{
        while (phase_deg > 180.0f) {
                phase_deg -= 360.0f;
        }
        while (phase_deg < -180.0f) {
                phase_deg += 360.0f;
        }
        return phase_deg;
}

static u16_t impedance_convert_sample(u8_t convert_channel, u8_t dac_code)
{
        u16_t r;
        r = spi_trans(Writecommand_generator(6, dac_code));
        if (r == (u16_t)0xFFFF) { impedance_spi_errors++; }
        /* Extra dummy so DAC output settles before the sampled CONVERT */
        spi_trans(Convertcommand_generator(convert_channel, 0));
        spi_trans(Convertcommand_generator(convert_channel, 0));
        spi_trans(Convertcommand_generator(convert_channel, 0));
        r = spi_trans(Convertcommand_generator(convert_channel, 0));
        if (r == (u16_t)0xFFFF) { impedance_spi_errors++; }
        return r;
}

/* Return Zcheck capacitor value in pF from Register 5 encoding */
static float impedance_zcheck_scale_pf(u8_t reg5)
{
        uint8_t s = (reg5 >> 3) & 0x03;
        if (s == 0x00) return 0.1f;
        if (s == 0x01) return 1.0f;
        return 10.0f; /* 0x03 */
}

static void impedance_log_channel_diagnostic(u8_t logical_channel,
        uint32_t test_freq_hz, u8_t selected_reg5, float zcheck_cap_pf,
        float dac_peak_v, float electrode_peak_uv,
        float impedance_ohm, float impedance_phase_deg)
{
#if IMPEDANCE_DIAGNOSTIC_LOG
        const float phase_rad = impedance_phase_deg * ((float)M_PI / 180.0f);
        const float raw_real_ohm_f = impedance_ohm * cosf(phase_rad);
        const float raw_imag_ohm_f = impedance_ohm * sinf(phase_rad);
        const int32_t raw_real_ohm = (int32_t)((raw_real_ohm_f >= 0.0f) ? (raw_real_ohm_f + 0.5f) : (raw_real_ohm_f - 0.5f));
        const int32_t raw_imag_ohm = (int32_t)((raw_imag_ohm_f >= 0.0f) ? (raw_imag_ohm_f + 0.5f) : (raw_imag_ohm_f - 0.5f));
        const int32_t dac_peak_uv = (int32_t)(((dac_peak_v * 1.0e6f) >= 0.0f) ? (dac_peak_v * 1.0e6f + 0.5f) : (dac_peak_v * 1.0e6f - 0.5f));
        const int32_t electrode_peak_uv_i = (int32_t)((electrode_peak_uv >= 0.0f) ? (electrode_peak_uv + 0.5f) : (electrode_peak_uv - 0.5f));
        const int32_t phase_cdeg = (int32_t)((impedance_phase_deg >= 0.0f) ? (impedance_phase_deg * 100.0f + 0.5f) : (impedance_phase_deg * 100.0f - 0.5f));
        const uint32_t magnitude_ohm = (impedance_ohm >= 0.0f) ? (uint32_t)(impedance_ohm + 0.5f) : 0U;
        const uint32_t zcheck_cap_ff = (zcheck_cap_pf >= 0.0f) ? (uint32_t)(zcheck_cap_pf * 1000.0f + 0.5f) : 0U;

        LOG_INF("imp diag ch%u f=%uHz reg5=0x%02x zcheck=%u fF dac=%d uV elec=%d uV raw=(%d,%d) ohm mag=%u phase=%d cdeg",
                (uint32_t)logical_channel + 1U,
                test_freq_hz,
                selected_reg5,
                zcheck_cap_ff,
                dac_peak_uv,
                electrode_peak_uv_i,
                raw_real_ohm,
                raw_imag_ohm,
                magnitude_ohm,
                phase_cdeg);
#else
        (void)logical_channel;
        (void)test_freq_hz;
        (void)selected_reg5;
        (void)zcheck_cap_pf;
        (void)dac_peak_v;
        (void)electrode_peak_uv;
        (void)impedance_ohm;
        (void)impedance_phase_deg;
#endif
}

/* Quick 1-cycle probe to pick the best Zcheck capacitor for a channel */
static u8_t impedance_auto_select_cap(u8_t convert_ch, u8_t zcheck_ch,
                                      uint32_t cycles_per_sample)
{
        spi_trans(Writecommand_generator(5, IMPEDANCE_ZCHECK_REG5_1PF));
        spi_trans(Writecommand_generator(7, zcheck_ch));
        spi_trans(Writecommand_generator(6, 128));
        for (int i = 0; i < 3; i++)
                spi_trans(Convertcommand_generator(convert_ch, 0));

        uint32_t next = k_cycle_get_32() + cycles_per_sample;
        /* settle 1 cycle */
        for (uint32_t s = 0; s < IMPEDANCE_TEST_WAVE_POINTS; s++) {
                while ((int32_t)(k_cycle_get_32() - next) < 0) {}
                next += cycles_per_sample;
                impedance_convert_sample(convert_ch,
                        impedance_dac_wave[s]);
        }
        /* measure 1 cycle, track peak */
        float peak_uv = 0.0f;
        for (uint32_t s = 0; s < IMPEDANCE_TEST_WAVE_POINTS; s++) {
                while ((int32_t)(k_cycle_get_32() - next) < 0) {}
                next += cycles_per_sample;
                u16_t raw = impedance_convert_sample(convert_ch,
                        impedance_dac_wave[s]);
                float uv = adc_raw_to_uv(raw);
                if (uv < 0) uv = -uv;
                if (uv > peak_uv) peak_uv = uv;
        }

        if (peak_uv > IMPEDANCE_RANGE_HI)
                return IMPEDANCE_ZCHECK_REG5_100FF;  /* too large -> 0.1 pF */
        if (peak_uv < IMPEDANCE_RANGE_LO)
                return IMPEDANCE_ZCHECK_REG5_10PF;   /* too small -> 10 pF  */
        return IMPEDANCE_ZCHECK_REG5_1PF;            /* 1 pF OK */
}

static void measure_channel_impedance(u8_t logical_channel,
        uint32_t *magnitude_ohm, int16_t *phase_cdeg,
        u8_t selected_reg5, uint32_t test_freq_hz)
{
        /* Electrodes sit on RHD amplifier channels 8-23 */
        const u8_t zcheck_channel  = logical_channel + IMPEDANCE_TEST_CONVERT_OFFSET;
        const u8_t convert_channel = logical_channel + IMPEDANCE_TEST_CONVERT_OFFSET;
        const float zcheck_cap_pf  = impedance_zcheck_scale_pf(selected_reg5);
        const uint32_t total_samples = IMPEDANCE_TEST_WAVE_POINTS * IMPEDANCE_TEST_MEASURE_CYCLES;
        const uint32_t cycles_per_sample =
                sys_clock_hw_cycles_per_sec() / (test_freq_hz * IMPEDANCE_TEST_WAVE_POINTS);
        const float dac_lsb_volt = RHD2132_ADC_REF_VOLTAGE_v / 256.0f;
        float meas_cos_sum = 0.0f;
        float meas_sin_sum = 0.0f;
        float ref_cos_sum = 0.0f;
        float ref_sin_sum = 0.0f;

        *magnitude_ohm = 0;
        *phase_cdeg = 0;

        if (cycles_per_sample == 0U) {
                return;
        }

        /* Configure Zcheck: select capacitor + channel */
        spi_trans(Writecommand_generator(5, selected_reg5));
        spi_trans(Writecommand_generator(7, zcheck_channel));
        spi_trans(Writecommand_generator(6, 128));
        spi_trans(Convertcommand_generator(convert_channel, 0));
        spi_trans(Convertcommand_generator(convert_channel, 0));
        spi_trans(Convertcommand_generator(convert_channel, 0));

        /* Settle phase (5 cycles) */
        uint32_t next_sample_cycle = k_cycle_get_32() + cycles_per_sample;
        for (uint32_t settle = 0; settle < IMPEDANCE_TEST_SETTLE_CYCLES * IMPEDANCE_TEST_WAVE_POINTS; settle++) {
                while ((int32_t)(k_cycle_get_32() - next_sample_cycle) < 0) {
                }
                next_sample_cycle += cycles_per_sample;
                impedance_convert_sample(convert_channel, impedance_dac_wave[settle % IMPEDANCE_TEST_WAVE_POINTS]);
        }

        /* Lock-in measurement phase */
        for (uint32_t sample_index = 0; sample_index < total_samples; sample_index++) {
                const uint32_t point = sample_index % IMPEDANCE_TEST_WAVE_POINTS;
                const float phase = 2.0f * (float)M_PI * (float)point / (float)IMPEDANCE_TEST_WAVE_POINTS;
                const float cosine = cosf(phase);
                const float sine = sinf(phase);
                const uint8_t dac_code = impedance_dac_wave[point];
                const float dac_voltage = ((float)dac_code - 128.0f) * dac_lsb_volt;

                while ((int32_t)(k_cycle_get_32() - next_sample_cycle) < 0) {
                }
                next_sample_cycle += cycles_per_sample;

                u16_t raw_sample = impedance_convert_sample(convert_channel, dac_code);
                float measured_uv = adc_raw_to_uv(raw_sample);

                meas_cos_sum += measured_uv * cosine;
                meas_sin_sum += measured_uv * sine;
                ref_cos_sum += dac_voltage * cosine;
                ref_sin_sum += dac_voltage * sine;
        }

        float electrode_peak_uv = (2.0f / (float)total_samples) *
                                  sqrtf(meas_cos_sum * meas_cos_sum + meas_sin_sum * meas_sin_sum);
        float dac_peak_v = (2.0f / (float)total_samples) *
                           sqrtf(ref_cos_sum * ref_cos_sum + ref_sin_sum * ref_sin_sum);
        if ((electrode_peak_uv <= 0.0f) || (dac_peak_v <= 0.0f)) {
                return;
        }

        float measured_phase_deg = atan2f(-meas_sin_sum, meas_cos_sum) * (180.0f / (float)M_PI);
        float dac_phase_deg = atan2f(-ref_sin_sum, ref_cos_sum) * (180.0f / (float)M_PI);
        float impedance_phase_deg = wrap_phase_deg(measured_phase_deg - dac_phase_deg - 90.0f);
        float test_current_a = 2.0f * (float)M_PI *
                               (float)test_freq_hz *
                               (zcheck_cap_pf * 1.0e-12f) *
                               dac_peak_v;

        if (test_current_a <= 0.0f) {
                return;
        }

        float impedance_ohm = (electrode_peak_uv * 1.0e-6f) / test_current_a;
        if (!isfinite(impedance_ohm) || (impedance_ohm < 0.0f)) {
                return;
        }
        impedance_log_channel_diagnostic(logical_channel,
                                         test_freq_hz,
                                         selected_reg5,
                                         zcheck_cap_pf,
                                         dac_peak_v,
                                         electrode_peak_uv,
                                         impedance_ohm,
                                         impedance_phase_deg);
        if (impedance_ohm > 4294967295.0f) {
                *magnitude_ohm = 4294967295u;
        } else {
                *magnitude_ohm = (uint32_t)(impedance_ohm + 0.5f);
        }
        *phase_cdeg = (int16_t)(wrap_phase_deg(impedance_phase_deg) * 100.0f);
}

static void run_impedance_test(void)
{
        impedance_test_request = false;
        impedance_test_active = true;
        sample_switch = false;
        impedance_spi_errors = 0;

        memset(impedance_magnitude_ohm, 0, sizeof(impedance_magnitude_ohm));
        memset(impedance_phase_cdeg, 0, sizeof(impedance_phase_cdeg));

        update_impedance_timestamp();
        err = impedance_progress_payload_wrap(0, 0, IMPEDANCE_TEST_CHANNEL_COUNT, 0);
        if (err) {
                esb_flush_tx();
        }

        nrfx_gpiote_out_task_disable(&gpiote_instance, NRFX_SPIM_SS_PIN);
        rhdspi_init();
        RHD_err = RHD_init(Register_config_impedance);
        if (RHD_err) {
                LOG_INF("impedance RHD init failed %x", RHD_err);
        } else {
                const uint32_t cps_1k = sys_clock_hw_cycles_per_sec() /
                        (IMPEDANCE_TEST_TARGET_FREQ_HZ * IMPEDANCE_TEST_WAVE_POINTS);

                /* Initial Zcheck setup: 1 pF default, DAC midpoint */
                spi_trans(Writecommand_generator(5, IMPEDANCE_ZCHECK_REG5_1PF));
                spi_trans(Writecommand_generator(6, 128));

                /* ---- Phase 1: measure all channels at 1 kHz ---- */
                for (u8_t channel = 0; channel < IMPEDANCE_TEST_CHANNEL_COUNT; channel++) {
                        const u8_t zch = channel + IMPEDANCE_TEST_CONVERT_OFFSET;
                        const u8_t cch = channel + IMPEDANCE_TEST_CONVERT_OFFSET;

                        u8_t best_reg5 = impedance_auto_select_cap(cch, zch, cps_1k);

                        measure_channel_impedance(channel,
                                                  &impedance_magnitude_ohm[channel],
                                                  &impedance_phase_cdeg[channel],
                                                  best_reg5,
                                                  IMPEDANCE_TEST_TARGET_FREQ_HZ);
                        update_impedance_timestamp();
                        err = impedance_progress_payload_wrap(1,
                                                              channel,
                                                              IMPEDANCE_TEST_CHANNEL_COUNT,
                                                              (u8_t)(((uint32_t)(channel + 1) * 80U) / IMPEDANCE_TEST_CHANNEL_COUNT));
                        if (err) {
                                esb_flush_tx();
                        }
                }

                /* ---- Phase 2: 100 Hz open-circuit check for suspect channels ---- */
                const uint32_t cps_lo = sys_clock_hw_cycles_per_sec() /
                        (IMPEDANCE_OPENCHECK_FREQ_HZ * IMPEDANCE_TEST_WAVE_POINTS);

                for (u8_t channel = 0; channel < IMPEDANCE_TEST_CHANNEL_COUNT; channel++) {
                        if (impedance_magnitude_ohm[channel] < IMPEDANCE_OPENCHECK_SUSPECT_OHM) {
                                continue; /* normal electrode, skip */
                        }
                        /* This channel hit the 1 kHz ceiling — verify at 100 Hz */
                        const u8_t zch = channel + IMPEDANCE_TEST_CONVERT_OFFSET;
                        const u8_t cch = channel + IMPEDANCE_TEST_CONVERT_OFFSET;
                        uint32_t open_mag = 0;
                        int16_t  open_phase = 0;

                        u8_t best_reg5 = impedance_auto_select_cap(cch, zch, cps_lo);
                        measure_channel_impedance(channel,
                                                  &open_mag, &open_phase,
                                                  best_reg5,
                                                  IMPEDANCE_OPENCHECK_FREQ_HZ);

                        if (open_mag >= IMPEDANCE_OPENCHECK_CONFIRM_OHM) {
                                /* Confirmed open / broken electrode */
                                impedance_magnitude_ohm[channel] = IMPEDANCE_OPEN_MARKER;
                                impedance_phase_cdeg[channel] = 0;
                        }
                        /* else: high-Z but not open, keep the 1 kHz value */

                        update_impedance_timestamp();
                        err = impedance_progress_payload_wrap(1,
                                                              channel,
                                                              IMPEDANCE_TEST_CHANNEL_COUNT,
                                                              80U + (u8_t)(((uint32_t)(channel + 1) * 20U) / IMPEDANCE_TEST_CHANNEL_COUNT));
                        if (err) {
                                esb_flush_tx();
                        }
                }

                /* Disable impedance check, restore defaults */
                spi_trans(Writecommand_generator(5, 0x00));
                spi_trans(Writecommand_generator(6, 128));
                spi_trans(Writecommand_generator(7, 0x00));

                if (impedance_spi_errors > 0) {
                        LOG_INF("impedance: %u SPI errors", impedance_spi_errors);
                }
        }

        nrfx_spim_uninit(&spi_init);
        low_power();

        update_impedance_timestamp();
        err = impedance_result_payload_wrap(impedance_magnitude_ohm,
                                            impedance_phase_cdeg,
                                            IMPEDANCE_TEST_CHANNEL_COUNT);
        if (err) {
                esb_flush_tx();
        }

        impedance_test_active = false;
        sample_switch = false;
}


void structure_rx_data(){
        bool completed_spi_buff;
        uint32_t overflow_words;

        unsigned int key = irq_lock();
        completed_spi_buff = spi_ready_buff_flag;
        overflow_words = spi_ready_overflow_flag;
        irq_unlock(key);
        last_structured_spi_overflow_words = overflow_words;

        if (overflow_words != 0U) {
                const bool next_spi_buff = !completed_spi_buff;

                if(mode_uses_mode0_raw_spi_source(sampe_mode)){
                        uint32_t copy_words = overflow_words;
                        if (copy_words > MODE0_SPI_CHUNK_WORDS) {
                                copy_words = MODE0_SPI_CHUNK_WORDS;
                        }
                        for (uint32_t j = 0; j < copy_words; j++) {
                                mode_0_m_rx_buf[next_spi_buff][j] =
                                        mode_0_m_rx_buf[completed_spi_buff][MODE0_SPI_CHUNK_WORDS + j];
                        }
                }else if(mode_uses_mode2_raw_spi_source(sampe_mode)){
                        uint32_t copy_words = overflow_words;
                        if (copy_words > MODE_3_SPI_RX_BUF_SIZE) {
                                copy_words = MODE_3_SPI_RX_BUF_SIZE;
                        }
                        for (uint32_t j = 0; j < copy_words; j++) {
                                mode_2_m_rx_buf[next_spi_buff][j] =
                                        mode_2_m_rx_buf[completed_spi_buff][MODE_3_SPI_RX_BUF_SIZE + j];
                        }
                }else if(mode_uses_shared_raw_spi_source(sampe_mode)){
                        uint32_t copy_words = overflow_words;
                        if (copy_words > MODE_3_SPI_RX_BUF_SIZE) {
                                copy_words = MODE_3_SPI_RX_BUF_SIZE;
                        }
                        for (uint32_t j = 0; j < copy_words; j++) {
                                mode_3_m_rx_buf[next_spi_buff][j] =
                                        mode_3_m_rx_buf[completed_spi_buff][MODE_3_SPI_RX_BUF_SIZE + j];
                        }
                }else if(mode_uses_20k_spike_spi_source(sampe_mode)){
                        uint32_t copy_words = overflow_words;
                        if (copy_words > SPIKE_SPI_RX_BUF_SIZE) {
                                copy_words = SPIKE_SPI_RX_BUF_SIZE;
                        }
                        for (uint32_t j = 0; j < copy_words; j++) {
                                spike_m_rx_buf[next_spi_buff][j] =
                                        spike_m_rx_buf[completed_spi_buff][SPIKE_SPI_RX_BUF_SIZE + j];
                        }
                }
        }

        // proprocess raw data
        if(mode_uses_mode0_raw_spi_source(sampe_mode)){
                const u16_t *rx_words = mode_0_m_rx_buf[completed_spi_buff];
                uint32_t chunk_index = mode0_spi_chunk_count;
                if (chunk_index >= MODE0_PACKET_CHUNKS) {
                        chunk_index = 0U;
                }
                const uint32_t mode0_sample_offset = chunk_index * MODE0_SPI_CHUNK_SAMPLES;
                for (u16_t P_size = 0; P_size < MODE0_SPI_CHUNK_SAMPLES; P_size++) {
                        const uint32_t packet_sample = mode0_sample_offset + P_size;
                        // const u16_t *sample_words = &rx_words[NUM_CHANNELS * P_size];
                        for (u16_t ch = 0; ch < NUM_CHANNELS; ch++) {
                                const u8_t physical_ch = channel_order[ch];
                                if (physical_ch < Channel_recorded) {
                                        u16_t sample = swapShort16(rx_words[NUM_CHANNELS * P_size + ch]);
                                        int16_t centered_counts = (int16_t)((int32_t)sample - 32768);
                                        mode_0_input_counts_buffer[physical_ch * SAMPLE_POINT_NUM + packet_sample] = centered_counts;
                                        mode_0_input_buffer[physical_ch * SAMPLE_POINT_NUM + packet_sample] =
                                                ((float)centered_counts * scale_factor) * Filter_scale;
                                }
                        }
                }
        }else if(mode_uses_mode2_raw_spi_source(sampe_mode) || mode_uses_shared_raw_spi_source(sampe_mode)){
                const u16_t *rx_words = mode_uses_mode2_raw_spi_source(sampe_mode) ?
                        mode_2_m_rx_buf[completed_spi_buff] :
                        mode_3_m_rx_buf[completed_spi_buff];
                for (u16_t P_size = 0; P_size < CHUNK_SIZE; P_size++)
                {
                        for (u16_t ch = 0; ch < NUM_CHANNELS; ch++)
                        {
                                // 2 steps delay converted result
                                const u8_t physical_ch = channel_order[ch];
                                if (physical_ch < Channel_recorded)
                                {
                                        mode_3_array_t[physical_ch * CHUNK_SIZE + P_size] = rx_words[NUM_CHANNELS * P_size + ch];
                                }
                        }
                }
                convert_rhd2132_samples(mode_3_array_t, input_buffer, MODE_3_SPI_RX_BUF_SIZE, Filter_scale, 1);
        }else if(mode_uses_20k_spike_spi_source(sampe_mode)){ //  spike raw data + MUA : 20Khz
                const u16_t *rx_words = spike_m_rx_buf[completed_spi_buff];
                for (u16_t g = 0; g < sizeof(spike_channel_array[0]) / 2; g++) // loop 90 times
                {       // 90 sample points each channel
                        for (u16_t u = 0; u < SPIKE_CONVERT_FASHION_NUM; u++)
                        {
                                // 2 steps delay converted result
                                const u8_t physical_ch = channel_order[u];
                                if (physical_ch < Channel_recorded_spike)
                                {       
                                        spike_channel_array[physical_ch][g] = rx_words[SPIKE_CONVERT_FASHION_NUM * g + u];
                                }
                        }
                } // spike_channel_array shape is 16 * 90 u16_t
        }
}

/*   Mode 1
* spike detection algorithm--- v0.1 only consider the noise level and get the threshold from it
* 整体的思路是: 首先将所有的sample 值大于threshold的得到index值，之后通过记录上升沿的spike来作为spike 的timestamp
* 之后去掉ISI 小于2ms的spike 和在这一段数据中边缘的过短的spike；最终得到spike 的timestamp
* L: 21 -> 21 points each spike (overlap half length in 20khz sampling) (per spike lasting 2 ms)
* ****************************************************************
注意，进行spike detection就必须要进行lfp的去除，通过rhd的dsp去除lfp导致采集到的原始信号是没有lfp的
注意，threshold 范围为 0 ~ 32768；为绝对值
*/
int L = MUA_BIN_SIZE;
int temp_ISI = SPIKE_SAMPLE_POINT_NUM * time_window + 1;
int first_spike_index = 0;
int last_spike_index = 0;
int spike_num_per_bin = 0;
u8_t channel_idx[SPIKE_SAMPLE_POINT_NUM * time_window + 1] = {0};
u8_t diff[SPIKE_SAMPLE_POINT_NUM * time_window] = {0};
u16_t threshold_temp = 0x0000;
u16_t temp_raw_data = 0;
u8_t PN_Flag[SPIKE_SAMPLE_POINT_NUM * time_window] = {0}; // 1 is positive ;0 is negative


// 这个list 用来保存当前所使用的各个通道的threshold，之后实时上传到GUI来进行显示 mode1
u16_t threshold_list[16] = {
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS,
        DEFAULT_SPIKE_THRESHOLD_COUNTS, DEFAULT_SPIKE_THRESHOLD_COUNTS
};

void get_MUA_data(u16_t channel_num)
{
        // re-init parameters
	first_spike_index = 0;
	last_spike_index = 0;
	spike_num_per_bin = 0;
	temp_ISI = SPIKE_SAMPLE_POINT_NUM * time_window + 1; 

	for (int j = 0; j < SPIKE_SAMPLE_POINT_NUM * time_window; j++)
	{
		temp_raw_data = (u16_t)(swapShort16(spike_channel_array[channel_num][j]));
                /* get absolute value */
		if (temp_raw_data < 0x8000) // unsigned encoding
		{       // negative value
			temp_raw_data = 0x8000 - temp_raw_data;
			PN_Flag[j] = 0;
		}
		else
		{       // positve value
			temp_raw_data = temp_raw_data - 0x8000;
			PN_Flag[j] = 1;
		}
                /* recording abs spike raw data of one channel */
		spike_channel_array_t[j] = temp_raw_data;
		// init the channel_idx and diff
		diff[j] = 0;
		channel_idx[j] = 0;
	}
        channel_idx[0] = 1; // defaultly drop the first data point.

	/* spike detection */
	threshold_temp = threshold_list[channel_num];
	for (int i = 1; i < sizeof(channel_idx); i++)
	{
		// 注意： 这里可以选择是使用positive value 来进行判断 spike 还是使用negative value；默认使用negative value	 
		if (spike_channel_array_t[i - 1] > threshold_temp && PN_Flag[i - 1] == 0)
		{
			channel_idx[i] = 1;
                        // 记录上升沿的spike来作为spike 的timestamp
			if (channel_idx[i - 1] == 0)
			{
				diff[i - 1] = 1;
			}
		}
	}
        /* remove bad spikes */
	// 1. delete spikes ISI < 1ms 21 sample points in 20kHz sampling
	for (int j = 0; j < sizeof(diff); j++)
	{
		if (diff[j] == 1)
		{
			if (temp_ISI == SPIKE_SAMPLE_POINT_NUM * time_window + 1) // the first value
			{
				temp_ISI = j;
				first_spike_index = j;
			}
			else
			{
				if (j - temp_ISI < L)
				{       // 这一步保证两个spike 之间至少差L个sample points, 去掉相对延迟的 spike
					diff[j] = 0;
				}
				else
				{
					temp_ISI = j;
				}
			}
		}
	}
	last_spike_index = temp_ISI;

	// 2. delete spikes on edges (the first and the last one) shorter than L
                // last one
        if (last_spike_index != SPIKE_SAMPLE_POINT_NUM*time_window + 1){ // ensure there has spike detected
                 // end edge
                if (last_spike_index + L >= SPIKE_SAMPLE_POINT_NUM*time_window)
                {
                        diff[last_spike_index] = 0;
                }
        
                // first one
                if ((first_spike_index - L) <= 0) // onset edge
                {
                        diff[first_spike_index] = 0;
                }
        }
	
        /* compress the MUA data to 1 bit/sample */
	        // 对MutiUnitActivityArray进行操作，注意对每一个元素都要操作以免数据的残余影响
	int k = 0;
	for (int i = 0; i < sizeof(diff); i += L)
	{      
		spike_num_per_bin = 0;
		for (int j = 0; j < L; j++)
		{
			spike_num_per_bin += diff[i + j];
		}
		if (spike_num_per_bin >= 1)
		{
			MutiUnitActivityArray[k] = (1 << channel_num) | MutiUnitActivityArray[k]; // set 1 to given channel bit ; lowest bit is channel 0; MSB is channel 15
		}
		else
		{
			MutiUnitActivityArray[k] = ~(1 << channel_num) & MutiUnitActivityArray[k]; // set 0
		}
                k++;
	}
}

/**
 * Other Functions
 * 
 */
void running_time_offset(uint8_t test){
        LOG_INF("%u us running time! %d test \n", k_cyc_to_us_floor32(k_cycle_get_32()) - timerecording, test);
}
void running_time_onset(void){
        timerecording = k_cyc_to_us_floor32(k_cycle_get_32());
}

static u8_t mode0_power_ratio_q8(uint32_t numerator, uint32_t denominator)
{
        if (denominator == 0U) {
                return 0U;
        }
        uint32_t q = (numerator * 255U + (denominator / 2U)) / denominator;
        if (q > 255U) {
                q = 255U;
        }
        return (u8_t)q;
}

static u16_t mode0_power_flags(void)
{
        u16_t flags = (u16_t)(current_esb_tx_power_code & MODE0_POWER_FLAG_TX_POWER_MASK);
        flags |= (u16_t)(((u16_t)current_esb_retransmit_count << MODE0_POWER_FLAG_RETX_SHIFT) &
                         MODE0_POWER_FLAG_RETX_MASK);
        return flags;
}

static void mode0_power_reset_accumulators(void)
{
        mode0_power_sleep_us_accum = 0;
        mode0_power_active_us_accum = 0;
        mode0_power_sleep_samples = 0;
        mode0_power_active_samples = 0;
}

static void mode0_power_reset(void)
{
        mode0_power_telemetry_seq = 0;
        mode0_power_last_snapshot_ms = k_uptime_get_32();
        mode0_power_last_tx_attempt_total = esb_tx_attempt_counter;
        mode0_power_last_tx_retransmit_total = esb_tx_retransmit_counter;
        mode0_power_sleep_timing_active = false;
        mode0_power_active_timing_active = false;
        mode0_power_reset_accumulators();
        memset(mode0_power_telemetry, 0, sizeof(mode0_power_telemetry));
        mode0_power_telemetry[0] = (u16_t)(((mode0_power_flags() & 0x00FFU) << 8) |
                                           (mode0_power_telemetry_seq & 0x00FFU));
}

static void mode0_power_note_sleep_enter(void)
{
        uint32_t now_cycle = k_cycle_get_32();

        if (mode0_power_active_timing_active) {
                uint32_t active_cycles = now_cycle - mode0_power_wake_cycle;
                mode0_power_active_us_accum += k_cyc_to_us_floor32(active_cycles);
                mode0_power_active_samples++;
                mode0_power_active_timing_active = false;
        }

        mode0_power_sleep_enter_cycle = now_cycle;
        mode0_power_sleep_timing_active = true;
}

static void mode0_power_note_wake(void)
{
        uint32_t now_cycle = k_cycle_get_32();

        if (mode0_power_sleep_timing_active) {
                uint32_t sleep_cycles = now_cycle - mode0_power_sleep_enter_cycle;
                uint32_t sleep_us = k_cyc_to_us_floor32(sleep_cycles);
                mode0_power_sleep_us_accum += sleep_us;
                mode0_power_sleep_samples++;
                mode0_power_sleep_timing_active = false;
        }

        mode0_power_wake_cycle = now_cycle;
        mode0_power_active_timing_active = true;
}

static void mode0_power_snapshot(void)
{
        uint32_t sleep_avg_us = 0;
        uint32_t active_avg_us = 0;
        uint32_t tx_attempt_total = esb_tx_attempt_counter;
        uint32_t tx_retransmit_total = esb_tx_retransmit_counter;
        uint32_t tx_attempt_delta = tx_attempt_total - mode0_power_last_tx_attempt_total;
        uint32_t tx_retransmit_delta = tx_retransmit_total - mode0_power_last_tx_retransmit_total;

        if (mode0_power_sleep_samples > 0) {
                sleep_avg_us = mode0_power_sleep_us_accum / mode0_power_sleep_samples;
        }
        if (mode0_power_active_samples > 0) {
                active_avg_us = mode0_power_active_us_accum / mode0_power_active_samples;
        }

        mode0_power_last_tx_attempt_total = tx_attempt_total;
        mode0_power_last_tx_retransmit_total = tx_retransmit_total;

        mode0_power_telemetry_seq++;
        u8_t seq8 = (u8_t)(mode0_power_telemetry_seq & 0x00FFU);
        u8_t tx_config = (u8_t)(mode0_power_flags() & 0x00FFU);
        u8_t retransmit_rate_q8 = mode0_power_ratio_q8(tx_retransmit_delta, tx_attempt_delta);
        u8_t sleep_ratio_q8 = mode0_power_ratio_q8(sleep_avg_us, sleep_avg_us + active_avg_us);
        mode0_power_telemetry[0] = (u16_t)(((u16_t)tx_config << 8) | (u16_t)seq8);
        mode0_power_telemetry[1] = (u16_t)(((u16_t)sleep_ratio_q8 << 8) | (u16_t)retransmit_rate_q8);

        mode0_power_reset_accumulators();
}

static bool mode0_power_maybe_snapshot(uint32_t now_ms)
{
        if ((uint32_t)(now_ms - mode0_power_last_snapshot_ms) < MODE0_POWER_TELEMETRY_PERIOD_MS) {
                return false;
        }
        mode0_power_last_snapshot_ms = now_ms;
        mode0_power_snapshot();
        return true;
}

static void mode0_spi_chunk_reset(void)
{
        mode0_spi_chunk_count = 0;
        mode0_packet_overflow_words = 0;
}

static void mode0_processing_reset(void)
{
        mode0_accel_update_pending = false;
        mode0_status_update_pending = false;
        mode0_spi_chunk_reset();
        memset(mode_0_mand_packet_buffer, 0, sizeof(mode_0_mand_packet_buffer));
        mode0_mand_reset_state();
        mode0_processing_reset_request = false;
}

static bool sensor_due_10ms(uint32_t now_ms, uint32_t *next_due_ms)
{
        if ((int32_t)(now_ms - *next_due_ms) < 0) {
                return false;
        }

        if ((uint32_t)(now_ms - *next_due_ms) > 100U) {
                *next_due_ms = now_ms + 10U;
        } else {
                uint8_t catchup = 0U;
                do {
                        *next_due_ms += 10U;
                        catchup++;
                } while ((int32_t)(now_ms - *next_due_ms) >= 0 && catchup < 8U);
        }
        return true;
}

int dynamic_retransmit(void){
        // Disabled for mode0 low-power measurements: keep TX power fixed at 0 dBm.
        return 0;
}

/****************************main**************************************/
int main(void)
{       /* power consumption */
        /* 1. 1.7mA 所有都没哟
        * 2. + init_everything: 2.2 mA (3.2mA max)
        * 3. + init_RHD(): 2.8 mA
        * 4. + led keep light: 2.95 mA
        * 5. + RHD samplerate: 1Khz 19 3.1 mA (16 channel)
        * 6. + no ksleep with empty loop: 8.5mA (+ 5.4mA)
        * 6. + sleep 0.5ms in while main loop without esb: 3.5mA
        * 7. sleep 0.1ms with esb: 8 mA
        * 8. + thread: 3.8mA ;2khz 16 channel 
        * 9. + IMU (208 Hz accel / 104 Hz gyro) 6-axis: ~4.5mA
        * 10. + spike 17khz: 38mW: 9.6mA 
        * 11. spike 17khz 4 channels raw data : 22mA -> 75mW
        */
        // static u32_t runingtime;

        /**************** main thread ******************/
        mainThread = k_sched_current_thread_query();
        detect_boot_reset_context();
        (void)watchdog_startup_init();
        u32_t packets_counter = k_uptime_get_32(); // 8192 ticks per sec 
        u32_t battery_packets_counter = packets_counter;
        u32_t mode0_sensor_next_due_ms = packets_counter + 10U;
        u32_t mode2_sensor_next_due_ms = packets_counter + 10U;
        last_statistic_timestamp = packets_counter;

        /*
        ******************************************* setup ***********************************
        */
        bool sampling = false;
        watchdog_feed_if_ready();
        if (!init_everything()) {
                request_cold_reboot();
                perform_pending_reboot(false);
        }
        (void)power_guard_refresh_battery(false);
        if (power_guard_state == POWER_GUARD_REBOOT_PENDING) {
                perform_pending_reboot(false);
        }
        // TODO debug: 这里必须先初始化一次并开启10us的采样，不然就会导致第一次开始采样得到的数据是全0；
        if (!init_RHD()) {
                request_cold_reboot();
                perform_pending_reboot(false);
        }
        timer_start();
        k_sleep(K_USEC(10));
        timer_stop(); 
        low_power();
        
        /****************************recording start******************************/
        /*********************main loop*********************/
		while (1) {
                watchdog_feed_if_ready();
                process_remote_reboot_request(sampling);
                perform_pending_reboot(sampling);
                process_runtime_rf_channel_switch();
                process_runtime_esb_mode_config_update();
                /**********system command process**********/
                if(!sample_switch){
                        /* stop sampling */
                        if(sampling){
                             timer_stop();   
                             low_power();
                             if (sampe_mode == 2) {
                                     mode2_v2_reset();
                             }
                             sampling = false;
                        }
                        if(impedance_test_request && !impedance_test_active){
                                run_impedance_test();
                                continue;
                        }
                        /* sample mode switch */
                        if(mode_switch_flag){
                                sample_switch = true;
                                continue;
                        }

                        (void)power_guard_refresh_battery(false);
	                        if (power_guard_state == POWER_GUARD_REBOOT_PENDING) {
	                                continue;
	                        }
	                        /* empty esb packets */
                        err = empty_payload_wrap(lc_data);
                        if(err){
                                esb_flush_tx();
                                LOG_INF("%d esb empty payload failed", err);
                        }
                        LED_hinting(100, 2);

                }else if(!sampling){
                        if (!sampling_start_allowed()) {
                                sample_switch = false;
                                if (power_guard_state == POWER_GUARD_RECOVERING) {
                                        cancel_auto_recovery_attempt();
                                }
                                continue;
                        }
                        /* re-configration rhd with specific sample mode */
                        // uninit gpiote re-init rhd
	                nrfx_gpiote_out_task_disable(&gpiote_instance, NRFX_SPIM_SS_PIN);
                        if (!init_RHD()) {
                                if (power_guard_state == POWER_GUARD_RECOVERING) {
                                        cancel_auto_recovery_attempt();
                                        low_power();
                                } else {
                                        request_cold_reboot();
                                }
                                continue;
                        }
                        nrfx_gpiote_out_task_enable(&gpiote_instance, NRFX_SPIM_SS_PIN);
                        nrfx_gpiote_out_set(&gpiote_instance, NRFX_SPIM_SS_PIN); // reset the CS line to disable
                        /* filter */
                        if (filter_init() != 0) {
                                sample_switch = false;
                                low_power();
                                if (power_guard_state == POWER_GUARD_RECOVERING) {
                                        cancel_auto_recovery_attempt();
                                } else {
                                        request_cold_reboot();
                                }
                                continue;
                        }
                        /* IMU init */
                        IMU_mode_switch();
                        if (err != 0) {
                                sample_switch = false;
                                low_power();
                                if (power_guard_state == POWER_GUARD_RECOVERING) {
                                        cancel_auto_recovery_attempt();
                                } else {
                                        request_cold_reboot();
                                }
                                continue;
                        }
                        
                        /* begining sample */
                        buffer_is_full = false;
                        sampling = true;
                        overflow_signal = !overflow_signal;
                        RHD_tx_buf_setup();
                        
                        // timestamp alignment
                        if(!mode_switch_flag){ // normal 
                                if (!timestamp_handshake_start()) {
                                        sample_switch = false;
                                        sampling = false;
                                        low_power();
                                        if (power_guard_state == POWER_GUARD_RECOVERING) {
                                                cancel_auto_recovery_attempt();
                                        }
                                        continue;
                                }

                        }else{ // fast mode switch
                                mode_switch_flag = false;
                        }
                        
                        // start sampling
                        apply_esb_mode_config((u8_t)sampe_mode);
                        clear_voltage_recovery_tracking();
                        power_guard_state = POWER_GUARD_NORMAL;
                        power_guard_publish_status();
                        if (sampe_mode == 0) {
                                mode0_power_reset();
                                mode0_processing_reset();
                        }
                        timer_start();
                        if (sampe_mode == 0) {
                                mode0_power_note_sleep_enter();
                        }
                        k_sem_take(&rhd_spi_ready_sem, K_FOREVER);
                        if (sampe_mode == 0) {
                                mode0_power_note_wake();
                        }
                        process_remote_reboot_request(sampling);
                }

                /**********recording package processing**********/
                /* 
                * 注意：这里的最坏情况和txfifo的大小有关；为了避免SPI_RESET(2 priority)和ESB中断(0/1 priority)之间的冲突；
                * esb整个fifo使用最多的retransimit进行发送和rx回调函数处理的时间需要小于采样得到一个包的数据的所需时间
                * 一般情况esb所需时间：2[retransmit_count] * 8[txfifo_count] * 450[retransmit_delay] = 7.2 ms > 6.1 ms
                * 单个包的数据处理时间：< 1ms TODO
                * 通过设置上述三个参数esb可以为：1 * 4 * 450 = 1.8 ms 注意：fifo 还是要设置的大一些 4；重传次数的多少感觉并不能让传输
                * 变的稳定，还是需要大的fifo来让传输时间分散开了，更有利于稳定的传输；同时这样可以同时放多个包进去来增加速率
                * 现在使用 溢出检测的方法可以不需要要求esb 的传输必须在一个时间周期之内完成
                */

               /*
               注意： 目前在esb 驱动中，设置fifo 为 8,保证 三个模式的使用；同时重发失败后会丢弃，并进入esb 休眠 （esb 驱动改写）
               避免esb 的休眠周期被打乱导致功耗上升
               */
                if(buffer_is_full && sampling){ // a package is ready! 6.1ms per package for 96 data points
                /*** 0. next package process ***/
                        buffer_is_full = false;
                        if (sampe_mode == 0) {
                                if (mode0_power_reset_request) {
                                        mode0_power_reset();
                                        mode0_power_reset_request = false;
                                }
                                if (mode0_processing_reset_request) {
                                        mode0_processing_reset();
                                }
                                if (mode0_spi_chunk_count >= MODE0_PACKET_CHUNKS) {
                                        mode0_spi_chunk_reset();
                                }

                                /*** 0.1. structured copy rx_buf data ***/
                                structure_rx_data(); // Mode0 caches one 25-sample chunk at a time.
                                mode0_packet_overflow_words |= last_structured_spi_overflow_words;
                                mode0_spi_chunk_count++;

                                if (mode0_spi_chunk_count < MODE0_PACKET_CHUNKS) {
                                        mode0_power_note_sleep_enter();
                                        k_sem_take(&rhd_spi_ready_sem, K_FOREVER);
                                        mode0_power_note_wake();
                                        process_remote_reboot_request(sampling);
                                        continue;
                                }
                                mode0_spi_chunk_count = 0;
                        } else {
                                tx_payload_wraped_num++;

                                /*** 0.1. structured copy rx_buf data ***/
                                structure_rx_data(); // direct deinterleave and ADC-to-uV conversion
                        }

                        // the timestamp is absolutely value from power up for each packages
                        packet_timestamp = k_uptime_get_32(); // ms CONFIG_SYS_CLOCK_TICKS_PER_SEC depend the time resolution
                        timestamp_LTNSRS = packet_timestamp;
                        if (sampe_mode == 0 && mode0_power_maybe_snapshot(packet_timestamp)) {
                                mode0_status_update_pending = true;
                        }
                        
                /*** 0.2. imu lc data read ***/
                        // 注意： memset 效率不高这个memset 函数
                        bool sensor_sample_updated = false;
                        bool mode2_accel_updated = false;
                        if(sampe_mode == 0){
                                if (sensor_due_10ms(packet_timestamp, &mode0_sensor_next_due_ms)) {
                                        packets_counter = packet_timestamp;
                                        mode2_sensor_next_due_ms = packet_timestamp + 10U;
                                        if (IMU_init[0] == 2 || IMU_init[0] == 3) {
                                                LSM6DS3_Read_AccelOnly(); // Mode0 sends accel only.
                                                mode0_accel_update_pending = true;
                                                sensor_sample_updated = true;
                                        }
                                        if ((uint32_t)(packet_timestamp - battery_packets_counter) >= MODE0_BATTERY_POLL_PERIOD_MS) {
                                                battery_packets_counter = packet_timestamp;
                                                (void)power_guard_refresh_battery(true);
                                                mode0_status_update_pending = true;
                                                sensor_sample_updated = true;
                                        }
                                }
                        } else if(sampe_mode == 2){
                                if (sensor_due_10ms(packet_timestamp, &mode2_sensor_next_due_ms)) {
                                        mode0_sensor_next_due_ms = packet_timestamp + 10U;
                                        if (IMU_init[0] == 2 || IMU_init[0] == 3) {
                                                LSM6DS3_Read_AccelOnly();
                                                sensor_sample_updated = true;
                                                mode2_accel_updated = true;
                                        }
                                        (void)power_guard_refresh_battery(true);
                                        battery_packets_counter = packet_timestamp;
                                        sensor_sample_updated = true;
                                }
                        } else if(packet_timestamp - packets_counter >= 10){ // ~ 100Hz imu when enabled
                                packets_counter = packet_timestamp;
                                mode0_sensor_next_due_ms = packet_timestamp + 10U;
                                mode2_sensor_next_due_ms = packet_timestamp + 10U;
                                if (IMU_init[0] == 2 || IMU_init[0] == 3) {
                                        LSM6DS3_Read(); // Legacy modes keep the configured 3/6-axis behavior.
                                        sensor_sample_updated = true;
                                }
                                (void)power_guard_refresh_battery(true);
                                battery_packets_counter = packet_timestamp;
                                sensor_sample_updated = true;
                        }
                        if (sensor_sample_updated) {
                                sensor_update_flag = 1;
                        }
                        if (mode2_accel_updated) {
                                mode2_v2_pending_accel[0] = imu_data[0];
                                mode2_v2_pending_accel[1] = imu_data[1];
                                mode2_v2_pending_accel[2] = imu_data[2];
                                mode2_v2_accel_pending = true;
                        }

                /*** 2. spike detection : MUA data ***/
                        if(sampe_mode == 1){ // cost 1648 us : 1.6ms in 17khz 16 channels
                                for (u16_t mua = 0; mua < Channel_recorded_spike; mua++)
				{	
                                        get_MUA_data(mua);
                                }
                        }
                /*** 2.1 online filtering mode 3  ***/
                        if(sampe_mode == 3){
                                // single channel Raw data recording（原始行为：每个chunk发送一次，包长34字节）
                                // 新增：缓冲4个chunk后合并为一包发送，以最大化包长度利用
                                // 1) 将当前chunk复制到缓冲区（按通道存放）
                                for (u16_t i = 0; i < CHUNK_SIZE; i++) {
                                        mode_3_array_t_raw[recorded_spike_channel][mode3_raw_chunk_count * CHUNK_SIZE + i] =
                                                mode_3_array_t[recorded_spike_channel * CHUNK_SIZE + i];
                                }
                                mode3_raw_chunk_count++;
                                // 2) 缓冲满4个chunk则一次性发送
                                if (mode3_raw_chunk_count >= MODE3_RAW_BUFFER_CHUNKS) {
                                        err = mode_3_raw_tx_payload_wrap(&mode_3_array_t_raw[recorded_spike_channel][0],
                                                                         CHUNK_SIZE * MODE3_RAW_BUFFER_CHUNKS,
                                                                         recorded_spike_channel);
                                        mode3_raw_chunk_count = 0; // 发送后清空计数
                                        // 检查是否有待执行的mode切换（等待raw数据发送完毕后执行）
                                        if (mode3_pending_mode_switch) {
                                                mode3_pending_mode_switch = false;
                                                sampe_mode = mode3_pending_mode;
                                                mode_switch_flag = true;
                                                sample_switch = false;
                                        }
                                }
                                // ~ 1100 us : 10Khz
                                process_neural_signals_mode3(input_buffer, CHUNK_SIZE, 
                                                                                    decimated_buffer_LFP, mua_output, decimated_buffer_ESA);
                                // Convert LFP & ESA data from float to uint16 for transmission
                                convert_rhd2132_samples(mode_3_array_lfp_t, decimated_buffer_LFP, MODE_3_LFP_SIZE, Filter_scale, 0);
                                convert_rhd2132_samples(mode_3_array_esa_t, decimated_buffer_ESA , MODE_3_ESA_SIZE, Filter_scale, 0);
                        }
                /*** 3. ESB package organization ***/ 
                        if(sampe_mode == 0){ // LFP + MAND + selected raw, two 25-sample SPI chunks per packet
                                uint8_t selected_raw_channel = recorded_spike_channel;
	                                if (selected_raw_channel >= Channel_recorded) {
	                                        selected_raw_channel = 0;
		                                }

		                                process_lfp_lowpass_decimate(mode_0_input_buffer, SAMPLE_POINT_NUM, mode_0_decimated_buffer);
		                                bool mand_ready = process_mode0_mand_chunk(mode_0_input_counts_buffer, SAMPLE_POINT_NUM,
		                                                                           mode_0_mand_packet_buffer);

	                                if (!mand_ready) {
	                                        memset(mode_0_mand_packet_buffer, 0, sizeof(mode_0_mand_packet_buffer));
	                                }
                                bool mode0_include_accel = mode0_accel_update_pending;
	                                bool mode0_include_status = mode0_status_update_pending &&
	                                                            (mode0_include_accel ||
	                                                             !(IMU_init[0] == 2 || IMU_init[0] == 3));
		                                sensor_update_flag = (mode0_include_accel || mode0_include_status);
		                                tx_payload_wraped_num++;
		                                err = mode0_tx_payload_wrap_quantized(mode_0_decimated_buffer,
		                                                                  mode_0_mand_packet_buffer,
		                                                                  &mode_0_input_buffer[selected_raw_channel * SAMPLE_POINT_NUM],
	                                                                  imu_data,
	                                                                  lc_data,
	                                                                  mode0_power_telemetry,
	                                                                  selected_raw_channel,
                                                                  (mode0_packet_overflow_words != 0U),
                                                                  mode0_include_accel,
                                                                  mode0_include_status);
                                mode0_packet_overflow_words = 0;
                                if (mode0_include_accel) {
                                        mode0_accel_update_pending = false;
                                }
                                if (mode0_include_status) {
                                        mode0_status_update_pending = false;
                                }

                        }else if(sampe_mode == 1){ // spike: one channel raw data + raster
                                        /* for single raw channel MUA_BIN_SIZE: 18 ;SPIKE_SAMPLE_POINT_NUM: 90 */
                                        err = spike_tx_payload_wrap(spike_channel_array[recorded_spike_channel], MutiUnitActivityArray, 
                                                                        imu_data, lc_data, SPIKE_SAMPLE_POINT_NUM, recorded_spike_channel);
                        }else if(sampe_mode == 2){
                                err = mode2_v2_append_chunk_and_flush();

                        }else if(sampe_mode == 3){ 
                                // head + timestamp + flag + imu + battery + lfp + raster : 16 * 3 + 16 * 3 + 4 + 6 + 3 + 3 = 112
                                err = mode_3_tx_payload_wrap(mode_3_array_lfp_t, mode_3_array_esa_t, mua_output, imu_data, lc_data, MODE_3_LFP_SIZE);
                        } 
                        
                /*** 4. suspend main thread and wait to be waked up ***/
                sensor_update_flag = 0;
                if(sampe_mode == 0){ // 只在mode0下做低功耗的处理
                        mode0_power_note_sleep_enter();
                        k_sem_take(&rhd_spi_ready_sem, K_FOREVER); 
                        mode0_power_note_wake();
                        process_remote_reboot_request(sampling);
                }
                //TODO 考虑使用20Hz的ESA，125Hz采样率；来实现全天候的Mode3记录;
                }
	}
        return 0;
}
