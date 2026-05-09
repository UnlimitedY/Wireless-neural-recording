#ifndef ESBWIRELESS_H
#define ESBWIRELESS_H

#include <zephyr/drivers/clock_control.h>
#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/irq.h>
#include <nrf.h>
#include <esb.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/types.h>

#include "..\RHD_Recording\OnlineFilter.h"
/*
 * typedef
 */
typedef unsigned char u8_t;
typedef unsigned short u16_t;
typedef unsigned int u32_t;

extern bool sample_switch;
extern bool mode_switch_flag;
extern volatile bool remote_reboot_requested;
extern u16_t sampe_mode;
extern u16_t tx_payload_wraped_num;
void request_runtime_esb_mode_config(u8_t mode, u8_t tx_power_code, u8_t retransmit_count,
                                     u16_t retransmit_delay_us, u8_t noack_percent);

extern uint32_t packet_sent_counter[2]; // count the sent package number: [0] is success; [1] is fail
extern volatile uint32_t esb_tx_attempt_counter;
extern volatile uint32_t esb_tx_retransmit_counter;
#define POWER_GUARD_EMPTY_STATUS_MAGIC 0x0B01U
extern volatile u16_t power_guard_status_word;
extern volatile u16_t power_guard_low_stop_counter;
extern volatile u16_t power_guard_status_flags;
extern u8_t HABITS_events; // define the events of HABITS

extern u8_t raw_channel[16]; // define which channels used; 16
extern u8_t recorded_channel_num; // lfp raw data: channel number

extern u8_t spike_raw_channel[4]; // spike raw data: channel index: maximum 4 channels recorded 
extern u8_t recorded_spike_channel; // spike raw data: mode 1
extern u8_t mode3_raw_chunk_count;
extern bool mode3_pending_mode_switch;
extern u16_t mode3_pending_mode;
extern u8_t mode3_esa_reref_enable;
extern bool impedance_test_request;
extern bool impedance_test_active;

extern bool overflow_signal;
extern uint64_t stamp_check; // real-time timestamp from the power-up

extern bool sensor_update_flag; // flag of the sensor data is updated

extern u16_t threshold_list[16];

extern u8_t IMU_init[2];
extern volatile bool mode0_processing_reset_request;
extern volatile bool mode0_power_reset_request;
extern volatile u8_t mode0_quant_bits;
extern volatile u16_t mode0_quant_full_scale_uv;
void request_runtime_esb_current_mode_apply(void);
void request_mode0_quant_config(u8_t bit_depth, u16_t full_scale_uv);
bool esb_data_payload_should_noack(void);

#define MODE0_COMPACT_POWER_WORDS        2U
#define MODE0_QUANT_BITS_MIN             8U
#define MODE0_QUANT_BITS_MAX             12U
#define MODE0_QUANT_BITS_DEFAULT         12U
#define MODE0_QUANT_FULL_SCALE_UV_MIN    500U
#define MODE0_QUANT_FULL_SCALE_UV_MAX    10000U
#define MODE0_QUANT_FULL_SCALE_UV_STEP   500U
#define MODE0_QUANT_FULL_SCALE_UV_DEFAULT 1000U
#define MODE0_RAW_QUANT_FULL_SCALE_UV    500U
#define MODE0_LFP_SAMPLES_PER_CHANNEL    ((SAMPLE_POINT_NUM * TARGET_FS) / ORIGINAL_FS)
#define MODE0_LFP_POINTS_PER_PACKET      (NUM_CHANNELS * MODE0_LFP_SAMPLES_PER_CHANNEL)
#define MODE0_MAND_POINTS_PER_PACKET     NUM_CHANNELS
#define MODE0_RAW_POINTS_PER_PACKET      SAMPLE_POINT_NUM
#define MODE0_NEURAL_VALUES_PER_PACKET   (MODE0_LFP_POINTS_PER_PACKET + MODE0_MAND_POINTS_PER_PACKET + MODE0_RAW_POINTS_PER_PACKET)
#define MODE0_NEURAL_PACKED_BITS         MODE0_QUANT_BITS_DEFAULT
#define MODE0_NEURAL_PACKED_BYTES        (((MODE0_NEURAL_VALUES_PER_PACKET * MODE0_NEURAL_PACKED_BITS) + 7U) / 8U)
#define MODE0_NEURAL_PACKED_WORDS        ((MODE0_NEURAL_PACKED_BYTES + 1U) / 2U)
#define MODE0_NEURAL_PACKED_MAX_BITS     MODE0_QUANT_BITS_MAX
#define MODE0_NEURAL_PACKED_MAX_BYTES    (((MODE0_NEURAL_VALUES_PER_PACKET * MODE0_NEURAL_PACKED_MAX_BITS) + 7U) / 8U)
#define MODE0_NEURAL_PACKED_MAX_WORDS    ((MODE0_NEURAL_PACKED_MAX_BYTES + 1U) / 2U)
#define MODE0_HEADER_RAW_CHANNEL_MASK    0x0FU
#define MODE0_HEADER_OVERFLOW_FLAG       0x10U
#define MODE0_HEADER_ACCEL_FLAG          0x20U
#define MODE0_HEADER_STATUS_FLAG         0x40U
#define MODE0_ACCEL_WORDS                3U
#define MODE0_STATUS_WORDS               (3U + MODE0_COMPACT_POWER_WORDS)
#define MODE0_PACKET_BASE_WORDS          (1U + 2U + MODE0_NEURAL_PACKED_WORDS + 1U)
#define MODE0_PACKET_MAX_WORDS           (MODE0_PACKET_BASE_WORDS + MODE0_ACCEL_WORDS + MODE0_STATUS_WORDS)
#define MODE0_PACKET_BASE_MAX_WORDS      (1U + 2U + MODE0_NEURAL_PACKED_MAX_WORDS + 1U)
#define MODE0_PACKET_CONFIG_MAX_WORDS    (MODE0_PACKET_BASE_MAX_WORDS + MODE0_ACCEL_WORDS + MODE0_STATUS_WORDS)

#define MODE2_V2_FORMAT_BYTE             0x88U
#define MODE2_V2_BIT_DEPTH               8U
#define MODE2_V2_PACKET_SAMPLES          15U
#define MODE2_V2_CHANNELS                NUM_CHANNELS
#define MODE2_V2_PAYLOAD_BYTES           (MODE2_V2_PACKET_SAMPLES * MODE2_V2_CHANNELS)
#define MODE2_V2_PAYLOAD_WORDS           ((MODE2_V2_PAYLOAD_BYTES + 1U) / 2U)
#define MODE2_V2_HEADER_WORDS            4U
#define MODE2_V2_PACKET_WORDS            (MODE2_V2_HEADER_WORDS + MODE2_V2_PAYLOAD_WORDS)
#define MODE2_V2_EXT_IMU_WORDS           2U
#define MODE2_V2_EXT_PACKET_WORDS        (MODE2_V2_PACKET_WORDS + MODE2_V2_EXT_IMU_WORDS)
#define MODE2_V2_SCALE_UV                500.0f
#define MODE2_V2_QUANT_MAX               127

#define rf_channel_num 6
extern uint8_t rf_channel_list[rf_channel_num]; // list of channels
extern uint8_t rf_channel_rssi_list[rf_channel_num]; // list of channels
extern uint8_t bitrate; 
extern uint8_t rf_channel;
extern uint8_t active_rf_channel;

#define _RADIO_SHORTS_COMMON                                       \
	(RADIO_SHORTS_READY_START_Msk | RADIO_SHORTS_END_DISABLE_Msk | \
	 RADIO_SHORTS_ADDRESS_RSSISTART_Msk |                          \
	 RADIO_SHORTS_DISABLED_RSSISTOP_Msk)

// note: struct esb_payload.data needs to be changed to u16_t, and the length parameter still represents bytes
extern struct esb_payload rx_payload; // command & behavioral events --rx
extern struct esb_payload tx_payload; // neural signal -- tx

extern struct esb_payload empty_payload; // when sampling disable 
extern struct esb_payload timestamp_payload; // neural signal alignment required


/****************************************alignment with HABITS****************************************/
/*
* 软件对齐系统 （基于ESB）: 相当于每一次重启都是一个新的trial：每次重新开始sample的时候，做一次对齐；发送一个下位机这一次重启经历的时间，上位机将这个时间
* 在数据收集过程中，无法对齐实际时间和收到包的时间由于数据发送的延时和丢包问题，需要每次重启的时候，做一次单独的时间对齐到重启的实际时间
* 当GUI 重启的时候，会丢失这个对齐的值，需要保存一个文件来单独记录这个对齐的值
* HABITS 使用实际物理时间，同时LTNSRS 也使用实际物理时间 来对齐
* 注意：必须每次重启后都需要通过上位机来打开sample，才能实现时间的对齐而不出现bug
*/
extern u32_t packet_timestamp; // define the unique packet index: timestamp
extern uint32_t timestamp_LTNSRS; // packed timestamp from LTNSRS (long-term neural signal recording system)
extern u8_t led_align_state; // packed timestamp from LTNSRS (long-term neural signal recording system)
// not use
extern uint32_t timestamp_HABITS; // received timestamp from HABITS
extern uint32_t timestamp_baseline; // baseline timestamp updated by alignment events

/*****************************ESB function***************************************/
// esb init
int clocks_start(void);
int esb_initialize(void);

// esb params alignment
int esb_shake_hand_request(void);
int esb_shake_hand_received(void);

int esb_rf_channel_scan(void);
void request_runtime_rf_channel_switch(uint8_t requested_channel);

// esb callback
void event_handler(struct esb_evt const *event);
void command_process(uint8_t length, uint16_t *data); // command process & behavioral events & behavioral timestamps

// timestamp_payload wrap function: every sample onset
int timestamp_payload_wrap(void);
// empty_payload wrap function: when sample stopping
int empty_payload_wrap(int16_t *lc_data);
// tx_payload wrap function: when sample working mode 0
int mode0_tx_payload_wrap_quantized(const float32_t *lfp_data, const float32_t *mand_data,
                                    const float32_t *raw_data, const int16_t *imu_data, const int16_t *lc_data,
                                    const u16_t *compact_power_data, u8_t raw_channel_index,
                                    bool spi_overflow, bool include_accel, bool include_status);
// tx_payload wrap function: when sample working: mode 1
int spike_tx_payload_wrap(u16_t *Spike_Raw_data, u16_t *Spike_raster_data, int16_t *imu_data, int16_t *lc_data,  u16_t spike_raw_length, u8_t packet_index);
// mode 2
int spike_multi_tx_payload_wrap(u16_t *Spike_Raw_data, u16_t spike_raw_length, u8_t *packet_index, u8_t counter);
int mode2_v2_tx_payload_wrap_8bit(const int8_t *packed_samples, u16_t sample_count, u16_t packet_counter,
                                  const int16_t *accel_data, bool include_accel);
int spike_sensor_tx_payload_wrap(int16_t *imu_data, int16_t *lc_data);
// mode 3
// tx_payload wrap function: when sample working: mode 1
int mode_3_tx_payload_wrap(u16_t *lfp_Raw_data, u16_t *ESA_Raw_data, u16_t *Spike_raster_data, int16_t *imu_data, int16_t *lc_data,  u16_t lfp_raw_length);
int mode_3_raw_tx_payload_wrap(u16_t *Raw_data,  u16_t RawData_length,  u8_t Channel_index);
int impedance_progress_payload_wrap(u8_t stage, u8_t current_channel, u8_t total_channels, u8_t percent);
int impedance_result_payload_wrap(const uint32_t *impedance_magnitude_ohm,
                                  const int16_t *impedance_phase_cdeg,
                                  u8_t channel_count);
#endif
