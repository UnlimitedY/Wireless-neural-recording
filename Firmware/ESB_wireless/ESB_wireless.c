#include "ESB_wireless.h"
#include <zephyr/logging/log.h>
#include <stdint.h>
#include <string.h>
#define LOG_MODULE_NAMEs LFP_Recording_peripherials
LOG_MODULE_REGISTER(LOG_MODULE_NAMEs);

#define DEVICE_RUNTIME_ESB_CHANNEL_CMD 0x0B00
#define DEVICE_REMOTE_REBOOT_CMD 0x0C00
#define DEVICE_REMOTE_REBOOT_MAGIC 0xA55A
#define DEVICE_RUNTIME_ESB_MODE_CONFIG_CMD 0x0D00
#define DEVICE_MODE0_QUANT_CONFIG_CMD 0x0F00

#if (MODE0_PACKET_BASE_WORDS * 2U) != 204U
#error "Mode0 default short packet layout must remain 204 bytes"
#endif

#if (MODE0_PACKET_MAX_WORDS * 2U) != 220U
#error "Mode0 default max packet layout must remain 220 bytes"
#endif

#if (MODE0_PACKET_CONFIG_MAX_WORDS * 2U) != 220U
#error "Mode0 12-bit max packet layout must remain 220 bytes"
#endif

#if (MODE0_PACKET_CONFIG_MAX_WORDS * 2U) > 252U
#error "Mode0 packet exceeds ESB maximum payload length"
#endif

#if (MODE2_V2_PACKET_WORDS * 2U) != 248U
#error "Mode2 v2 packet layout must remain 248 bytes"
#endif

#if (MODE2_V2_EXT_PACKET_WORDS * 2U) != 252U
#error "Mode2 v2 extended packet layout must remain 252 bytes"
#endif

#if (MODE2_V2_EXT_PACKET_WORDS * 2U) > 252U
#error "Mode2 v2 extended packet exceeds ESB maximum payload length"
#endif

extern k_tid_t mainThread;

int clocks_start(void)
{
	int err;
	int res;
	struct onoff_manager *clk_mgr;
	struct onoff_client clk_cli;

	clk_mgr = z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF);
	if (!clk_mgr) {
		// LOG_ERR("Unable to get the Clock manager");
		return -ENXIO;
	}

	sys_notify_init_spinwait(&clk_cli.notify);

	err = onoff_request(clk_mgr, &clk_cli);
	if (err < 0) {
		// LOG_ERR("Clock request failed: %d", err);
		return err;
	}

	do {
		err = sys_notify_fetch_result(&clk_cli.notify, &res);
		if (!err && res) {
			// LOG_ERR("Clock could not be started: %d", res);
			return res;
		}
	} while (err);

	// LOG_DBG("HF clock started");
	return 0;
}

int esb_initialize(void)
{
	int err;
	/* These are arbitrary default addresses. In end user products
	 * different addresses should be used for each set of devices.
	 */
    // pipe 0 的base addr， 注意不能使用0x55和0xAA，这两个是preamble（1 byte）所使用的的地址 4bytes
	uint8_t base_addr_0[4] = {0xE7,0xE7,0xE7,0xE7};
    // pipe 1-7 的base addr 4 bytes
	uint8_t base_addr_1[4] = {0xC2, 0xC2, 0xC2, 0xC2};
    // 8个pipes 所使用的的唯一的prefix 1 byte 的地址
	uint8_t addr_prefix[8] = {0xE7, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8};

	struct esb_config config = ESB_DEFAULT_CONFIG;

    // 注意： 使用ESB 模式，ACK不能附带payload，不能进行全双工的数据传输
	config.protocol = ESB_PROTOCOL_ESB_DPL; // 设置 传输数据的protocol ,可以选择固定长度的payload length还是动态变化的长度
	config.retransmit_delay = 1200; // 重传的时间延迟 us； 在接收端失效下，450 和600的值产生的failed tx events数量是一样的; 不能太短也不能太长
    /*
    * 注意： 必须要使用2mbps，否则会导致esb 占用cpu资源过多导致 休眠时间不足而功耗剧烈上升；即使1mbps 能增加稳定性
    */
	config.bitrate = ESB_BITRATE_2MBPS; // 设置 传输速率
	config.event_handler = event_handler; // 设置传送的事件的回调函数
	config.mode = ESB_MODE_PTX; // 设置这台机子是工作在上面mode上 ，ptx or prx ,一般对应多台ptx ,一台prx
	config.selective_auto_ack = true; // enable ack 
    // enable fast ramp up: 40us: 
    // 注意这里必须要保证接收双方要是nrf52以上系列的；如果不是，或者接收端ack离线，会导致ptx一直处于ESB_STATE_PTX_RX_ACK状态
    config.use_fast_ramp_up = false;  // 为了保证 稳定这里 disable fast ramp up
    config.retransmit_count = 1; // the times of retransmit
    // 注意：在ack 模式下，retransmit 失败以后，并不会把fifo清空。这相当于是一个绝对的fifo
    // 同时tx suspended，需要等待下一个发送命令;(注意这里需要esb_start_tx()命令，auto模式下只会返回错误)
    // TODO 最好使用esb_flush_tx()来构建一个相对的fifo；并且 合理使用esb_start_tx()来避免发送时间的浪费
    config.tx_mode = ESB_TXMODE_AUTO; 

	err = esb_init(&config);
	if (err) {
		return err;
	}

	err = esb_set_base_address_0(base_addr_0); // 设置esb的传输地址，一个esb可以同时与8个tx进行通信 ,即8个pipes
	if (err) {
		return err;
	}

	err = esb_set_base_address_1(base_addr_1);
	if (err) {
		return err;
	}

	err = esb_set_prefixes(addr_prefix, ARRAY_SIZE(addr_prefix));
	if (err) {
		return err;
	}

    // 这个会影响传输重发率和通信距离；在极限的设置下，目前custom board的esb通信距离在20cm左右；而且不能有障碍物；
	esb_set_tx_power(ESB_TX_POWER_0DBM);

    for (uint8_t i = 0; i < rf_channel_num; i++) {
        if (rf_channel_list[i] == active_rf_channel) {
            rf_channel = i;
            break;
        }
    }
    active_rf_channel = rf_channel_list[rf_channel];
    esb_set_rf_channel(active_rf_channel); 
	return 0;
}

void command_process(uint8_t length, uint16_t *data)
{
    /*
    * 1. RX_data : head (1 u16_t) + data (n u16_t)
    * 2. head includes: 
    *   - system commands:
    *   1. (Launch sampling): 0001
    *   2. (Dummary): 0000
    *   3. (disable sampling): 0002
    *   4. (low power): 0003
    * 
    *   - behavioral events
    *   1. (spike recording): 0100 (TODO) + data (RHD reconfigration)
    *   2. (LFP channels switch): 0200 + data (recording channels indice) 
    *       [config the registers of RHD to disable not used channels]
    *   3. (Impedance check): 0400 (TODO) 
    *   - alignment signal
    *   1. (HABITS events): 0300 (switch sample mode)
    */
	
    u16_t command_head = data[0];

    if (impedance_test_active || impedance_test_request) {
        if (command_head != 0x0900 &&
            command_head != DEVICE_RUNTIME_ESB_CHANNEL_CMD &&
            command_head != DEVICE_REMOTE_REBOOT_CMD &&
            command_head != DEVICE_RUNTIME_ESB_MODE_CONFIG_CMD &&
            command_head != DEVICE_MODE0_QUANT_CONFIG_CMD) {
            return;
        }
    }

    switch (command_head)
    {
    case 0x0001: // begin sampling
    {
        if (!sample_switch) // when the sample is paused
        {
            sample_switch = true;
        }
    }
        break;
    case 0x0002: // disable sampling
    {
        if (sample_switch)
        {
            sample_switch = false;
        }
    }
        break;
    case 0x0003: // IMU mode switch
    {
        IMU_init[1] = 1;
        IMU_init[0] =  (u8_t)data[1];
    }
        break;

    case 0x0200: //  channels switch
    {
        recorded_channel_num = 0;
        for (int i=0;i<16;i++){
            if((u8_t)data[i + 1] < 16){
                raw_channel[i] = (u8_t)data[i + 1];
                recorded_channel_num++;
            }
        }
    }
        break;
    case 0x0300: // behavioral event-triggered tasks (change mode): GUI & HABITS
    {
        if(data[1] != sampe_mode){
            u16_t target_mode = data[1];
            if((u8_t)target_mode <= 3){
                // 从mode3切出且raw chunk缓冲未清空：延迟切换，等待数据发完
                if(sampe_mode == 3 && mode3_raw_chunk_count > 0){
                    mode3_pending_mode_switch = true;
                    mode3_pending_mode = target_mode;
                } else {
                    // 非mode3或缓冲已空：立即切换
                    mode_switch_flag = true;
                    sample_switch = false;
                    sampe_mode = target_mode;
                }
            }
        }
    }
        break;
    case 0x0400: // spike mode 1: raw channel selection
    { // 注意，这里下发的threshold 是绝对值，也就是相对0V 位的值；
        if((u8_t)data[1] < 16){
            recorded_spike_channel = (u8_t)data[1];
            mode3_raw_chunk_count = 0;
            mode0_processing_reset_request = true;
            threshold_list[recorded_spike_channel] = data[2];
            // copy the value to mode3
            float temp_threshold_download = ((float)(data[2]) * scale_factor);
            set_spike_threshold(recorded_spike_channel, temp_threshold_download);
        }
    }
        break;
    case 0x0500: // spike mode 2: 4 raw channel selection
    {
        for(int i=1; i<5 ; i++){
            if((u8_t)data[i] < 16){
                spike_raw_channel[i-1] = (u8_t)data[i];
            }
        }
    }
        break;
    case 0x0600:
    {
        if ((u8_t)data[1] <= MODE3_ESA_REREF_SAFE_MEDIAN) {
            u8_t next_reref_mode = (u8_t)data[1];
            if (mode3_esa_reref_enable != next_reref_mode) {
                mode3_esa_reref_enable = next_reref_mode;
                mode0_processing_reset_request = true;
            }
        }
    }
        break;
    case 0x0900: // start impedance test
    {
        mode_switch_flag = false;
        impedance_test_request = true;
        sample_switch = false;
    }
        break;
    case DEVICE_RUNTIME_ESB_CHANNEL_CMD: // runtime ESB channel switch
    {
        request_runtime_rf_channel_switch((u8_t)data[1]);
        k_wakeup(mainThread);
    }
        break;
    case DEVICE_REMOTE_REBOOT_CMD: // GUI-requested peripheral firmware reboot
    {
        if (length >= 4 && data[1] == DEVICE_REMOTE_REBOOT_MAGIC) {
            remote_reboot_requested = true;
            sample_switch = false;
            k_wakeup(mainThread);
        }
    }
        break;
    case DEVICE_RUNTIME_ESB_MODE_CONFIG_CMD: // per-mode TX power, ACK window, and noack ratio
    {
        if (length >= 6) {
            u8_t mode = (u8_t)data[1];
            u8_t tx_power_code = (u8_t)(data[1] >> 8);
            u8_t retransmit_count = (u8_t)data[2];
            u16_t retransmit_delay_us = 1200U;
            u8_t noack_percent = 0U;

            if (length >= 10) {
                retransmit_delay_us = (u16_t)data[3];
                noack_percent = (u8_t)data[4];
            }

            request_runtime_esb_mode_config(mode,
                                            tx_power_code,
                                            retransmit_count,
                                            retransmit_delay_us,
                                            noack_percent);
            k_wakeup(mainThread);
        }
    }
        break;
    case DEVICE_MODE0_QUANT_CONFIG_CMD: // runtime mode0 quantization bit depth/full-scale
    {
        if (length >= 8) {
            request_mode0_quant_config((u8_t)data[1], data[2]);
            k_wakeup(mainThread);
        }
    }
        break;
    default:
        break;
    }

}

void event_handler(struct esb_evt const *event)
{
    uint32_t tx_attempts = event->tx_attempts;

	switch (event->evt_id) {
	case ESB_EVENT_TX_SUCCESS:
		packet_sent_counter[0]++;
        if (tx_attempts == 0U) {
            tx_attempts = 1U;
        }
        esb_tx_attempt_counter += tx_attempts;
        esb_tx_retransmit_counter += (tx_attempts - 1U);
		break;
	case ESB_EVENT_TX_FAILED:
		packet_sent_counter[1]++;
        if (tx_attempts == 0U) {
            tx_attempts = 1U;
        }
        esb_tx_attempt_counter += tx_attempts;
        esb_tx_retransmit_counter += (tx_attempts - 1U);
		break;
	case ESB_EVENT_RX_RECEIVED:
        {
        packet_sent_counter[0]++;
		while (esb_read_rx_payload(&rx_payload) == 0) {
            command_process(rx_payload.length, rx_payload.data);
		}
        }
		break;
	}
}

/*
* scan : return rssi value (weights) of channel list
*/
int esb_rf_channel_scan(void){
    if(!esb_is_idle()){
        return -1;
    }
    int rssi_reading = 0;
    uint8_t minimum_rssi_index = 0;
    uint8_t minimum_rssi = 0;

    for(int i=0;i<sizeof(rf_channel_list);i++){
        esb_set_rf_channel(rf_channel_list[i]);
        esb_start_rx();
        k_sleep(K_USEC(300)); // wait for booting up

        rssi_reading = 0;
        for(int j = 0; j < 10; j ++)
        {
            NRF_RADIO->TASKS_RSSISTART = 1;
            while(NRF_RADIO->EVENTS_RSSIEND == 0);
            rssi_reading += NRF_RADIO->RSSISAMPLE;
             k_sleep(K_USEC(100));
        }
        esb_stop_rx();
        k_sleep(K_USEC(300));
        rf_channel_rssi_list[i] = rssi_reading/10;

        if(minimum_rssi < rf_channel_rssi_list[i]){
            minimum_rssi_index = i;
            minimum_rssi = rf_channel_rssi_list[i];
        }
        
    }

    esb_set_rf_channel(rf_channel_list[rf_channel]);
    return minimum_rssi_index; 
}

/*
* select channel and bitrate
*/
int esb_shake_hand_request(void){
    // check if the esb is idle but fifo not empty, which means the retranmition failed
    if(esb_is_idle() && !esb_tx_empty()){
        // send a packet (request) with params    
        // change the front packets
        u8_t channel_temp;
        if(rf_channel < 5){
            channel_temp = rf_channel_list[rf_channel + 1];
        }else{
            channel_temp = rf_channel_list[0];
        }
        esb_tx_front_channel(channel_temp);
    }
}

int esb_shake_hand_received(void){
    // check if request success, change the params
    if(!esb_tx_empty()){
    // TODO
    }
}


int timestamp_payload_wrap(void){
    timestamp_payload.noack = 0;
    stamp_check = k_uptime_get();
    // stamp_check = (uint64_t)k_cyc_to_ms_near32(k_cycle_get_32());
    for (int i = 0; i < 4; i++)
    {
        timestamp_payload.data[4 - i] = (stamp_check >> (i * 16)) & 0xFFFF;
    }
    timestamp_payload.data[0] = 0x0300;	// payload type		
    timestamp_payload.data[5] = ((u16_t)rf_channel_rssi_list[rf_channel]) << 8 | (u16_t)active_rf_channel;
    return esb_write_payload(&timestamp_payload); 
}

int empty_payload_wrap(int16_t *lc_data){
    empty_payload.data[0] = 0x0400; // empty type
    empty_payload.data[1]++;		   // some information needed to upload
    // battery
    empty_payload.data[2] = (int16_t)*(lc_data);
    empty_payload.data[3] = (int16_t)*(lc_data + 1);
    empty_payload.data[4] = (int16_t)*(lc_data + 2);
    if (power_guard_status_word != 0U || power_guard_status_flags != 0U) {
        // Optional low-battery guard status. The first five words remain legacy-compatible.
        empty_payload.data[5] = POWER_GUARD_EMPTY_STATUS_MAGIC;
        empty_payload.data[6] = power_guard_status_word;
        empty_payload.data[7] = power_guard_low_stop_counter;
        empty_payload.data[8] = power_guard_status_flags;
        empty_payload.data[9] = 0;
        empty_payload.length = 20;
    } else {
        empty_payload.length = 10;
    }

    empty_payload.noack = 0;
    return esb_write_payload(&empty_payload); // wirte 对应 tx ,将packet加入到tx buffer中
}

/**************************mode 0**********************/
static inline int32_t mode0_round_float_to_i32(float32_t value)
{
    return (int32_t)(value >= 0.0f ? value + 0.5f : value - 0.5f);
}

static inline u8_t mode0_clamp_quant_bits_local(u8_t bit_depth)
{
    if (bit_depth < MODE0_QUANT_BITS_MIN) {
        return MODE0_QUANT_BITS_MIN;
    }
    if (bit_depth > MODE0_QUANT_BITS_MAX) {
        return MODE0_QUANT_BITS_MAX;
    }
    return bit_depth;
}

static inline u16_t mode0_clamp_quant_full_scale_uv_local(u16_t full_scale_uv)
{
    uint16_t value = (full_scale_uv == 0U) ? MODE0_QUANT_FULL_SCALE_UV_DEFAULT : full_scale_uv;
    if (value < MODE0_QUANT_FULL_SCALE_UV_MIN) {
        value = MODE0_QUANT_FULL_SCALE_UV_MIN;
    } else if (value > MODE0_QUANT_FULL_SCALE_UV_MAX) {
        value = MODE0_QUANT_FULL_SCALE_UV_MAX;
    }

    const uint16_t offset = (uint16_t)(value - MODE0_QUANT_FULL_SCALE_UV_MIN);
    const uint16_t step = MODE0_QUANT_FULL_SCALE_UV_STEP;
    value = (uint16_t)(MODE0_QUANT_FULL_SCALE_UV_MIN + ((offset + (step / 2U)) / step) * step);
    if (value > MODE0_QUANT_FULL_SCALE_UV_MAX) {
        value = MODE0_QUANT_FULL_SCALE_UV_MAX;
    }
    return value;
}

static inline uint16_t mode0_packed_words_for_bits(u8_t bit_depth)
{
    return (uint16_t)(((MODE0_NEURAL_VALUES_PER_PACKET * (uint16_t)mode0_clamp_quant_bits_local(bit_depth)) + 15U) / 16U);
}

static inline uint16_t mode0_quantize_signed_bits(float32_t uv, u8_t bit_depth, u16_t full_scale_uv)
{
    const u8_t bits = mode0_clamp_quant_bits_local(bit_depth);
    const float32_t full_scale = (float32_t)mode0_clamp_quant_full_scale_uv_local(full_scale_uv);
    const int32_t q_max = ((int32_t)1 << (bits - 1U)) - 1;
    if (uv > full_scale) {
        uv = full_scale;
    } else if (uv < -full_scale) {
        uv = -full_scale;
    }
    int32_t q = mode0_round_float_to_i32((uv * (float32_t)q_max) / full_scale);
    if (q > q_max) {
        q = q_max;
    } else if (q < -q_max) {
        q = -q_max;
    }
    if (q < 0) {
        q += ((int32_t)1 << bits);
    }
    return (uint16_t)q & (uint16_t)(((uint16_t)1U << bits) - 1U);
}

static inline uint16_t mode0_quantize_unsigned_bits(float32_t uv, u8_t bit_depth)
{
    const u8_t bits = mode0_clamp_quant_bits_local(bit_depth);
    const int32_t q_max = ((int32_t)1 << bits) - 1;
    if (uv > 1000.0f) {
        uv = 1000.0f;
    } else if (uv < 0.0f) {
        uv = 0.0f;
    }
    int32_t q = mode0_round_float_to_i32((uv * (float32_t)q_max) / 1000.0f);
    if (q > q_max) {
        q = q_max;
    } else if (q < 0) {
        q = 0;
    }
    return (uint16_t)q;
}

static inline void mode0_pack_quantized_word_stream(u16_t *packed_words, uint16_t *word_index,
                                                    uint32_t *bit_buffer, u8_t *bit_count,
                                                    u16_t value, u8_t bit_depth)
{
    const u8_t bits = mode0_clamp_quant_bits_local(bit_depth);
    const uint32_t mask = ((uint32_t)1U << bits) - 1U;
    *bit_buffer |= ((uint32_t)value & mask) << *bit_count;
    *bit_count = (u8_t)(*bit_count + bits);
    while (*bit_count >= 16U) {
        packed_words[*word_index] = (u16_t)(*bit_buffer & 0xFFFFU);
        (*word_index)++;
        *bit_buffer >>= 16U;
        *bit_count = (u8_t)(*bit_count - 16U);
    }
}

int mode0_tx_payload_wrap_quantized(const float32_t *lfp_data, const float32_t *mand_data,
                                    const float32_t *raw_data, const int16_t *imu_data, const int16_t *lc_data,
                                    const u16_t *compact_power_data, u8_t raw_channel_index,
                                    bool spi_overflow, bool include_accel, bool include_status)
{
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    const u8_t quant_bits = mode0_clamp_quant_bits_local(mode0_quant_bits);
    const u16_t quant_full_scale_uv = mode0_clamp_quant_full_scale_uv_local(mode0_quant_full_scale_uv);
    const uint16_t packed_words_used = mode0_packed_words_for_bits(quant_bits);

    // 1. Header low byte merges selected raw channel and variable-packet flags.
    u8_t header_flags = (u8_t)(raw_channel_index & MODE0_HEADER_RAW_CHANNEL_MASK);
    if (spi_overflow) {
        header_flags |= MODE0_HEADER_OVERFLOW_FLAG;
    }
    if (include_accel) {
        header_flags |= MODE0_HEADER_ACCEL_FLAG;
    }
    if (include_status) {
        header_flags |= MODE0_HEADER_STATUS_FLAG;
    }
    tx_payload.data[0] = 0x0100 | (u16_t)header_flags;

    // 2. Real-time timestamp.
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    txbufIndex += 3;

    // 3. Packed neural data: LFP, MAND, selected raw.
    uint16_t packed_word_index = txbufIndex;
    uint32_t packed_bit_buffer = 0U;
    u8_t packed_bit_count = 0U;
    memset(&tx_payload.data[packed_word_index], 0, MODE0_NEURAL_PACKED_MAX_WORDS * sizeof(tx_payload.data[0]));

    for (uint16_t i = 0; i < MODE0_LFP_POINTS_PER_PACKET; i++) {
        mode0_pack_quantized_word_stream(tx_payload.data, &packed_word_index, &packed_bit_buffer, &packed_bit_count,
                                         mode0_quantize_signed_bits(lfp_data[i], quant_bits, quant_full_scale_uv),
                                         quant_bits);
    }
    for (uint16_t i = 0; i < MODE0_MAND_POINTS_PER_PACKET; i++) {
        mode0_pack_quantized_word_stream(tx_payload.data, &packed_word_index, &packed_bit_buffer, &packed_bit_count,
                                         mode0_quantize_unsigned_bits(mand_data[i], quant_bits),
                                         quant_bits);
    }
    for (uint16_t i = 0; i < MODE0_RAW_POINTS_PER_PACKET; i++) {
        mode0_pack_quantized_word_stream(tx_payload.data, &packed_word_index, &packed_bit_buffer, &packed_bit_count,
                                         mode0_quantize_signed_bits(raw_data[i], quant_bits, MODE0_RAW_QUANT_FULL_SCALE_UV),
                                         quant_bits);
    }
    if (packed_bit_count != 0U) {
        tx_payload.data[packed_word_index] = (u16_t)(packed_bit_buffer & 0xFFFFU);
        packed_word_index++;
    }
    txbufIndex += packed_words_used;

    // 4. Optional 100 Hz accel-only IMU words.
    if (include_accel) {
        for (int i = 0; i < 3; i++)
        {
            tx_payload.data[txbufIndex + i] =
                (imu_data == NULL) ? 0U : (u16_t)*(imu_data + i);
        }
        txbufIndex += MODE0_ACCEL_WORDS;
    }

    // 5. Optional 1 Hz battery/status plus compact power telemetry.
    if (include_status) {
        for (int i = 0; i < 3; i++)
        {
            tx_payload.data[txbufIndex + i] =
                (lc_data == NULL) ? 0U : (u16_t)*(lc_data + i);
        }
        txbufIndex += 3U;

        for (int i = 0; i < MODE0_COMPACT_POWER_WORDS; i++)
        {
            tx_payload.data[txbufIndex + i] =
                (compact_power_data == NULL) ? 0U : (u16_t)*(compact_power_data + i);
        }
        txbufIndex += MODE0_COMPACT_POWER_WORDS;
    }

    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;

    tx_payload.length = txbufIndex * 2; // Mode0 variable packet: 8-bit..12-bit, optional accel/status.
    return esb_write_payload(&tx_payload);
}

/**************************mode 1**********************/
int spike_tx_payload_wrap(u16_t *Spike_Raw_data, u16_t *Spike_raster_data, int16_t *imu_data, int16_t *lc_data, u16_t spike_raw_length, u8_t packet_index){
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    // 1. pre-head of packet 0xXXYY---XX is type; YY is the index of recorded channels
    tx_payload.data[0] = 0x0200 | packet_index; 
    // 2. package signal including: real-time timestamp; overflow_signal
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = ((u16_t)sensor_update_flag << 8) | (u16_t)overflow_signal; 
    txbufIndex += 4;

    // 3. twi data:  6-acc or 3-acc data 
    for (int i = 0; i < 6; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(imu_data + i);
    }
    txbufIndex += 6;

    // 4. BatterPower data: fixed 3 u16_t
    for (int i = 0; i < 3; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(lc_data + i);
    }
    txbufIndex += 3;

    // 5. spike raw data 1 channel length: 105
    for (int i = 0; i < spike_raw_length; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(Spike_Raw_data + i);
    }
    txbufIndex += spike_raw_length;
    
    // 6. spike raster data 5 shorts
    for (int i = 0; i < 5; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(Spike_raster_data + i);
    }
    txbufIndex += 5;
    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;

    tx_payload.length = txbufIndex * 2; //  90 * 2 + 5 * 2 + 13 * 2 == 216 bytes; maximum 252 bytes
    return esb_write_payload(&tx_payload); 
}

/**************************mode 2**********************/
int spike_multi_tx_payload_wrap(u16_t *Spike_Raw_data, u16_t spike_raw_length, u8_t *packet_index, u8_t counter){
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0; // ack; 注意，没有ack 会导致大约1% 的丢包 

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    // 1. pre-head of packet 0xXXYY---XX is type; YY is the index of recorded channels
    tx_payload.data[0] = 0x0500 | counter;  
    // 2. package signal including: real-time timestamp; overflow_signal
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = (u16_t)overflow_signal; 
    txbufIndex += 4;

    tx_payload.data[txbufIndex] = (((u16_t)*(packet_index) & 0x0F) << 12) |
                                  (((u16_t)*(packet_index + 1) & 0x0F) << 8) |
                                  (((u16_t)*(packet_index + 2) & 0x0F) << 4) |
                                  (((u16_t)*(packet_index + 3) & 0x0F));
    txbufIndex += 1;

    // 4. spike raw data 4 channels length: 120
    for (int i = 0; i < spike_raw_length; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(Spike_Raw_data + i);
    }
    txbufIndex += spike_raw_length;
    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;

    tx_payload.length = txbufIndex * 2; //  (4 + 2 + 120) * 2 == 252 bytes; maximum 252 bytes
    return esb_write_payload(&tx_payload); 
}

static uint64_t mode2_v2_pack_accel_q13(const int16_t *accel_data)
{
    uint64_t qx = ((uint16_t)accel_data[0]) >> 3;
    uint64_t qy = ((uint16_t)accel_data[1]) >> 3;
    uint64_t qz = ((uint16_t)accel_data[2]) >> 3;
    return qx | (qy << 13U) | (qz << 26U);
}

int mode2_v2_tx_payload_wrap_8bit(const int8_t *packed_samples, u16_t sample_count, u16_t packet_counter,
                                  const int16_t *accel_data, bool include_accel)
{
    if (packed_samples == NULL || sample_count != MODE2_V2_PACKET_SAMPLES) {
        return -EINVAL;
    }
    if (include_accel && accel_data == NULL) {
        return -EINVAL;
    }

    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;
    uint64_t accel_bits = include_accel ? mode2_v2_pack_accel_q13(accel_data) : 0U;
    tx_payload.data[0] = 0x0500 | (include_accel ? (u16_t)(accel_bits & 0xFFU) : MODE2_V2_FORMAT_BYTE);
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS;
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = packet_counter;

    const uint8_t *payload = (const uint8_t *)packed_samples;
    for (u16_t i = 0; i < MODE2_V2_PAYLOAD_WORDS; i++) {
        tx_payload.data[MODE2_V2_HEADER_WORDS + i] =
            ((u16_t)payload[2U * i]) | ((u16_t)payload[2U * i + 1U] << 8);
    }

    if (include_accel) {
        tx_payload.data[MODE2_V2_PACKET_WORDS] = (u16_t)((accel_bits >> 8U) & 0xFFFFU);
        tx_payload.data[MODE2_V2_PACKET_WORDS + 1U] = (u16_t)((accel_bits >> 24U) & 0xFFFFU);
        tx_payload.length = MODE2_V2_EXT_PACKET_WORDS * 2U;
    } else {
        tx_payload.length = MODE2_V2_PACKET_WORDS * 2U;
    }
    return esb_write_payload(&tx_payload);
}

int spike_sensor_tx_payload_wrap(int16_t *imu_data, int16_t *lc_data){
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    // 1. pre-head of packet 0xXXYY---XX is type; YY is the index of recorded channels
    tx_payload.data[0] = 0x0600; 
    // 2. package signal including: real-time timestamp; overflow_signal
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = ((u16_t)sensor_update_flag << 8) | (u16_t)overflow_signal; 
    txbufIndex += 4;

    // 3. twi data:  6-acc or 3-acc data 
    for (int i = 0; i < 6; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(imu_data + i);
    }
    txbufIndex += 6;

    // 4. BatterPower data: fixed 3 u16_t
    for (int i = 0; i < 3; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(lc_data + i);
    }
    txbufIndex += 3;
    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;

    tx_payload.length = txbufIndex * 2; // (4 + 6 + 3 + 1 counter) * 2 == 28 bytes; maximum 252 bytes
    return esb_write_payload(&tx_payload); 

}


/**************************mode 3**********************/
int mode_3_tx_payload_wrap(u16_t *lfp_Raw_data, u16_t *ESA_Raw_data, u16_t *Spike_raster_data, int16_t *imu_data, int16_t *lc_data,  u16_t lfp_raw_length){
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    // 1. pre-head of packet
    tx_payload.data[0] = 0x0700 | mode3_esa_reref_enable; 
    // 2. package signal including: real-time timestamp; overflow_signal
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = ((u16_t)sensor_update_flag << 8) | (u16_t)overflow_signal; 
    txbufIndex += 4;

    // 3. twi data:  6-acc or 3-acc data 
    for (int i = 0; i < 6; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(imu_data + i);
    }
    txbufIndex += 6;

    // 4. BatterPower data: fixed 3 u16_t
    for (int i = 0; i < 3; i++)
    {
        tx_payload.data[txbufIndex + i] = (int16_t)*(lc_data + i);
    }
    txbufIndex += 3;

    // 5. lfp raw data 1 channel length: 2 * 16
    for (int i = 0; i < lfp_raw_length; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(lfp_Raw_data + i);
    }
    txbufIndex += lfp_raw_length;

    // 6. ESA raw data 1
    for (int i = 0; i < lfp_raw_length; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(ESA_Raw_data + i);
    }
    txbufIndex += lfp_raw_length;
    
    // 6. spike raster data
    for (int i = 0; i < 3; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(Spike_raster_data + i);
    }
    txbufIndex += 3;
    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;

    tx_payload.length = txbufIndex * 2; //  16 * 5 （lfp） + 1 （head）+ 2 (timestamp) + 1 (flag) + 6 (IMU) + 3 (battery) + 3 (raster, 1 ms per short) == 98
    return esb_write_payload(&tx_payload); 
}

int mode_3_raw_tx_payload_wrap(u16_t *Raw_data,  u16_t RawData_length,  u8_t Channel_index){
    tx_payload.noack = esb_data_payload_should_noack() ? 1 : 0;

    u16_t txbufIndex = 0; // count the length of one tx_payload package
    // 1. pre-head of packet 0xXXYY---XX is type; YY is the index of recorded channels
    tx_payload.data[0] = 0x0800 | Channel_index; 
    // 2. package signal including: real-time timestamp; overflow_signal
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS; 
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[3] = (u16_t)overflow_signal; 
    txbufIndex += 4;
    // 3. spike raw data 1 channel length: 30
    for (int i = 0; i < RawData_length; i++)
    { 
        tx_payload.data[txbufIndex + i] = (u16_t)*(Raw_data + i);
    }
    txbufIndex += RawData_length;
    tx_payload.data[txbufIndex] = tx_payload_wraped_num;
    txbufIndex++;
    
    tx_payload.length = txbufIndex * 2; //  4 + 30 == 34 bytes; maximum 252 bytes
    return esb_write_payload(&tx_payload); 
}

int impedance_progress_payload_wrap(u8_t stage, u8_t current_channel, u8_t total_channels, u8_t percent)
{
    tx_payload.noack = 0;

    tx_payload.data[0] = 0x0900 | (u16_t)stage;
    tx_payload.data[1] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[2] = (u16_t)timestamp_LTNSRS;
    tx_payload.data[3] = (((u16_t)percent) << 8) | (u16_t)current_channel;
    tx_payload.data[4] = (u16_t)total_channels;
    tx_payload.length = 10;

    return esb_write_payload(&tx_payload);
}

int impedance_result_payload_wrap(const uint32_t *impedance_magnitude_ohm,
                                  const int16_t *impedance_phase_cdeg,
                                  u8_t channel_count)
{
    tx_payload.noack = 0;

    u16_t txbufIndex = 0;
    tx_payload.data[txbufIndex++] = 0x0A00 | (u16_t)channel_count;
    tx_payload.data[txbufIndex++] = (u16_t)(timestamp_LTNSRS >> 16);
    tx_payload.data[txbufIndex++] = (u16_t)timestamp_LTNSRS;
    tx_payload.data[txbufIndex++] = (u16_t)sampe_mode;

    for (u8_t i = 0; i < channel_count; i++) {
        uint32_t magnitude = impedance_magnitude_ohm[i];
        tx_payload.data[txbufIndex++] = (u16_t)(magnitude >> 16);
        tx_payload.data[txbufIndex++] = (u16_t)(magnitude & 0xFFFF);
        tx_payload.data[txbufIndex++] = (u16_t)impedance_phase_cdeg[i];
    }

    tx_payload.length = txbufIndex * 2;
    return esb_write_payload(&tx_payload);
}
