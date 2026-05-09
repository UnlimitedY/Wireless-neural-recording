#include "OnlineFilter.h"
/********
 * 
 * 
 */
/****************define**************** */
float scale_factor = RHD2132_ADC_UV_PER_COUNT;

// Global filter configurations
FilterConfig LFPlowpass_config;
FilterConfig ESAlowpass_config;
FilterConfig ESAhighpass_config;

// Channel filter states
ChannelFilterState channel_states[NUM_CHANNELS] ARM_ALIGN;

// Processing buffers
float32_t input_buffer[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
float32_t lowpass_buffer_LFP[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
float32_t decimated_buffer_LFP[NUM_CHANNELS * MODE3_DECIMATED_SAMPLES_PER_CHANNEL] ARM_ALIGN;
float32_t lowpass_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
float32_t highpass_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
float32_t rectified_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
float32_t decimated_buffer_ESA[NUM_CHANNELS * MODE3_DECIMATED_SAMPLES_PER_CHANNEL] ARM_ALIGN;
float32_t reref_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
uint16_t mua_output[CHUNK_SIZE/MUA_BIN_SIZE_MODE_3] ARM_ALIGN;
static float32_t mode3_sanitized_input_buffer[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;

static int32_t mode0_mand_delay_ring_x2[NUM_CHANNELS][MODE0_MAND_DELAY_RING_SIZE] ARM_ALIGN;
static uint32_t mode0_mand_bin_sums_x2[NUM_CHANNELS][MODE0_MAND_WINDOW_BINS] ARM_ALIGN;
static uint32_t mode0_mand_rolling_sums_x2[NUM_CHANNELS] ARM_ALIGN;
static uint32_t mode0_mand_current_bin_sums_x2[NUM_CHANNELS] ARM_ALIGN;
static uint16_t mode0_mand_delay_write_index = 0;
static uint16_t mode0_mand_samples_in_bin = 0;
static uint16_t mode0_mand_window_write_index = 0;
static uint16_t mode0_mand_bins_filled = 0;
static uint32_t mode0_mand_total_samples = 0;

/***************function***************** */
/**
 * 数值安全的饱和函数
 */
static inline float32_t saturate_float32(float32_t value) {
    if (isnan(value) || isinf(value)) {
        return 0.0f;
    }
    if (value > FLOAT32_MAX_SAFE) {
        return FLOAT32_MAX_SAFE;
    }
    if (value < FLOAT32_MIN_SAFE) {
        return FLOAT32_MIN_SAFE;
    }
    return 1;
}

static inline uint16_t rhd_float_uv_to_adc_code(float32_t value_uv, float32_t filter_scale)
{
    if (isnan(value_uv) || isinf(value_uv) || isnan(filter_scale) || isinf(filter_scale) || filter_scale == 0.0f) {
        return (uint16_t)((RHD2132_ADC_REF_VOLTAGE / RHD2132_ADC_UV_PER_COUNT) + 0.5f);
    }

    float32_t scaled_clip_uv = RHD2132_ADC_FULL_SCALE_UV * fabsf(filter_scale);
    if (scaled_clip_uv <= 0.0f || isnan(scaled_clip_uv) || isinf(scaled_clip_uv)) {
        scaled_clip_uv = RHD2132_ADC_FULL_SCALE_UV;
    }
    if (value_uv > scaled_clip_uv) {
        value_uv = scaled_clip_uv;
    } else if (value_uv < -scaled_clip_uv) {
        value_uv = -scaled_clip_uv;
    }

    float32_t adc_code = (value_uv / filter_scale + RHD2132_ADC_REF_VOLTAGE) / scale_factor;
    if (isnan(adc_code) || isinf(adc_code)) {
        return (uint16_t)((RHD2132_ADC_REF_VOLTAGE / RHD2132_ADC_UV_PER_COUNT) + 0.5f);
    }
    if (adc_code <= 0.0f) {
        return 0U;
    }
    if (adc_code >= 65535.0f) {
        return 65535U;
    }
    return (uint16_t)(adc_code + 0.5f);
}

static inline float32_t mode3_sanitize_input_uv(float32_t value)
{
    if (isnan(value) || isinf(value)) {
        return 0.0f;
    }
    if (value > RHD2132_ADC_FULL_SCALE_UV) {
        return RHD2132_ADC_FULL_SCALE_UV;
    }
    if (value < -RHD2132_ADC_FULL_SCALE_UV) {
        return -RHD2132_ADC_FULL_SCALE_UV;
    }
    return value;
}


void saturation_protection(float32_t data, int channel, int lowpass){ 
   
    // 检查低通和高通滤波器 的输出是否饱和, 使用抽查的形式
    float status = saturate_float32(data);
    // 如果溢出，清零状态之后重新初始化对应的通道的滤波器
    if(status == 0.0f){
        // overflow
        ChannelFilterState *state = &channel_states[channel];
        if(lowpass){
            memset(state->lp_forward_state_LFP, 0, sizeof(state->lp_forward_state_LFP));
            arm_biquad_cascade_df2T_init_f32(
                &state->lowpass_forward_LFP,
                LFPlowpass_config.num_stages,
                LFPlowpass_config.coeffs,
                state->lp_forward_state_LFP
            );
        }else{
            memset(state->hp_state_ESA, 0, sizeof(state->hp_state_ESA));
            arm_biquad_cascade_df2T_init_f32(
                &state->highpass_ESA,       
                ESAhighpass_config.num_stages,
                ESAhighpass_config.coeffs,
                state->hp_state_ESA
            );
        }
    }else{
        // TODO
    }
}


void convert_rhd2132_samples(u16_t* adc_data, float_t* float_data, uint32_t num_samples, float32_t filter_scale,  bool inttofloat) {
    // 所有的 filter的放缩都是在输入开始放缩，再传输数据出去的时候还原
    // 通用优化路径 (使用循环展开)
    uint32_t i = 0;
    const uint32_t unroll_count = num_samples & ~0x3; // 4的倍数
    
    if(inttofloat){
        // 注意这里需要对RHD的数据进行前后倒转一下
        swap_bytes_builtin(adc_data, num_samples);
        // convert to float
        // arm_q15_to_float((q15_t *)adc_data, float_data, num_samples);
        // 4次循环展开 转为uV
        for (; i < unroll_count; i += 4) {
            float_data[i]   = ((float)(adc_data[i])   *   scale_factor - RHD2132_ADC_REF_VOLTAGE) * filter_scale;
            float_data[i+1] = ((float)(adc_data[i+1])  * scale_factor - RHD2132_ADC_REF_VOLTAGE) * filter_scale;
            float_data[i+2] = ((float)(adc_data[i+2])   * scale_factor - RHD2132_ADC_REF_VOLTAGE) * filter_scale;
            float_data[i+3] = ((float)(adc_data[i+3])   * scale_factor - RHD2132_ADC_REF_VOLTAGE) * filter_scale;
        }
        // 处理剩余样本
        for (; i < num_samples; i++) {
            float_data[i]   = ((float)(adc_data[i])   * scale_factor - RHD2132_ADC_REF_VOLTAGE) * filter_scale;
        }
    }else{
        for (; i < unroll_count; i += 4) {
            adc_data[i]   = rhd_float_uv_to_adc_code(float_data[i], filter_scale);
            adc_data[i+1] = rhd_float_uv_to_adc_code(float_data[i+1], filter_scale);
            adc_data[i+2] = rhd_float_uv_to_adc_code(float_data[i+2], filter_scale);
            adc_data[i+3] = rhd_float_uv_to_adc_code(float_data[i+3], filter_scale);
        }
        // 处理剩余样本
        for (; i < num_samples; i++) {
            adc_data[i]   = rhd_float_uv_to_adc_code(float_data[i], filter_scale);
        }
        // 直接输出 转换为uV的数值
        // arm_float_to_q15(float_data, (q15_t *)adc_data, num_samples);
        
    }
}

/**
 * Calculate Butterworth filter coefficients for LFP (2nd order, 300Hz @ 12.5kHz)
 * @param config: Filter configuration structure
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_butterworth_coeffs_LFP(FilterConfig *config) {
    if (config->order != IIR_ORDER_lowpass_LFP || config->cutoff_freq != IIR_CUTOFF_lowpass_LFP) {
        return ARM_MATH_ARGUMENT_ERROR;
    }
    config->num_stages = config->order / 2;
    
    // 2nd order Butterworth lowpass filter coefficients for 300Hz @ 12.5kHz sampling rate  Group delay Max 1ms
    // Calculated using bilinear transform
    config->coeffs[0] = 1.0f;     // b0
	config->coeffs[1] = 2.0f;     // b1
	config->coeffs[2] = 1.0f;     // b2
	config->coeffs[3] = 1.78743251795648472324273825506679713726f;    // a1
	config->coeffs[4] = -0.807949591420913160177974532416556030512f;     // a2

    config->gain = 0.005129268366107147397725540827195800375f; 
    return ARM_MATH_SUCCESS;
}

/**
 * Calculate Butterworth filter coefficients for ESA lowpass (1st order, 150Hz @ 12.5kHz)
 * @param config: Filter configuration structure
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_butterworth_coeffs_ESA(FilterConfig *config) {
    if (config->order != IIR_ORDER_lowpass_ESA || config->cutoff_freq != IIR_CUTOFF_lowpass_ESA) {
        return ARM_MATH_ARGUMENT_ERROR;
    }
    
    config->num_stages = 1; // 1st order filter
    
    // 1st order Butterworth lowpass filter coefficients for 150Hz @ 12.5kHz sampling rate ； Group delay Max 1ms
    // Calculated using bilinear transform: H(z) = (b0 + b1*z^-1) / (1 + a1*z^-1)
    config->coeffs[0] = 1.0f;     // b0
	config->coeffs[1] = 1.0f;     // b1
	config->coeffs[2] = 0.0f;                   // b2 (not used for 1st order)
	config->coeffs[3] = 0.927307768331003257067379763611825183034f;    // a1
	config->coeffs[4] = 0.0f;                   // a2 (not used for 1st order)

    config->gain = 0.036346115834498420038567445544686052017f; 
    return ARM_MATH_SUCCESS;
}

/**
 * Calculate high-pass Butterworth filter coefficients for ESA (1st order, 250Hz @ 12.5kHz)
 * @param config: Filter configuration structure
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_highpass_butterworth_coeffs_ESA(FilterConfig *config) {
     if (config->order != IIR_ORDER_highpass_ESA || config->cutoff_freq != IIR_CUTOFF_highpass_ESA) {
        return ARM_MATH_ARGUMENT_ERROR;
    }
    
    config->num_stages = 1; // 1st order filter
    
    // 1st order Butterworth highpass filter coefficients for 250Hz @ 12.5kHz sampling rate
    // Calculated using bilinear transform: H(z) = (b0 + b1*z^-1) / (1 + a1*z^-1)
    config->coeffs[0] = 1.0f;     // b0
	config->coeffs[1] = -1.0f;    // b1
	config->coeffs[2] = 0.0f;                   // b2 (not used for 1st order)
	config->coeffs[3] = 0.881618592363189068628059885668335482478f;    // a1
	config->coeffs[4] = 0.0f;                   // a2 (not used for 1st order)

	config->gain = 0.940809296181594478802878711576340720057f; 
    
    return ARM_MATH_SUCCESS;
}

/**
 * Initialize low-pass filter for LFP (2nd order, 300Hz)
 * @param order: Filter order (must be even)
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_lowpass_filter_LFP(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate) {
    LFPlowpass_config.order = order;
    LFPlowpass_config.cutoff_freq = cutoff_freq;
    LFPlowpass_config.sampling_rate = sampling_rate;
    
    arm_status status = calculate_butterworth_coeffs_LFP(&LFPlowpass_config);
    if (status != ARM_MATH_SUCCESS) {
        return status;
    }
    
    // Initialize filter instances for all channels
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        ChannelFilterState *state = &channel_states[ch];
        memset(state->lp_forward_state_LFP, 0, sizeof(state->lp_forward_state_LFP));
        state->lowpass_gain_LFP = LFPlowpass_config.gain;
        
        // Initialize forward filter
        arm_biquad_cascade_df2T_init_f32(
            &state->lowpass_forward_LFP,
            LFPlowpass_config.num_stages,
            LFPlowpass_config.coeffs,
            state->lp_forward_state_LFP
        );
    }
    
    return ARM_MATH_SUCCESS;
}

/**
 * Initialize low-pass filter for ESA (1st order, 12Hz)
 * @param order: Filter order
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_lowpass_filter_ESA(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate) {
    ESAlowpass_config.order = order;
    ESAlowpass_config.cutoff_freq = cutoff_freq;
    ESAlowpass_config.sampling_rate = sampling_rate;
    
    arm_status status = calculate_butterworth_coeffs_ESA(&ESAlowpass_config);
    if (status != ARM_MATH_SUCCESS) {
        return status;
    }
    
    // Initialize filter instances for all channels
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        ChannelFilterState *state = &channel_states[ch];
        memset(state->lp_forward_state_ESA, 0, sizeof(state->lp_forward_state_ESA));
        state->lowpass_gain_ESA = ESAlowpass_config.gain;
        
        // Initialize ESA lowpass filter
        arm_biquad_cascade_df2T_init_f32(
            &state->lowpass_forward_ESA,
            ESAlowpass_config.num_stages,
            ESAlowpass_config.coeffs,
            state->lp_forward_state_ESA
        );
    }
    
    return ARM_MATH_SUCCESS;
}

/**
 * Initialize high-pass filter for ESA (1st order, 250Hz)
 * @param order: Filter order
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_highpass_filter_ESA(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate) {
    ESAhighpass_config.order = order;
    ESAhighpass_config.cutoff_freq = cutoff_freq;
    ESAhighpass_config.sampling_rate = sampling_rate;
    
    arm_status status = calculate_highpass_butterworth_coeffs_ESA(&ESAhighpass_config);
    if (status != ARM_MATH_SUCCESS) {
        return status;
    }
    
    // Initialize filter instances for all channels
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        ChannelFilterState *state = &channel_states[ch];
        memset(state->hp_state_ESA, 0, sizeof(state->hp_state_ESA));
        state->highpass_gain_ESA = ESAhighpass_config.gain;
        arm_biquad_cascade_df2T_init_f32(
            &state->highpass_ESA,
            ESAhighpass_config.num_stages,
            ESAhighpass_config.coeffs,
            state->hp_state_ESA
        );
        
        // Initialize spike detection parameters
        state->last_spike_time = -MIN_ISI;
        if (state->threshold <= 0.0f) {
            state->threshold = DEFAULT_SPIKE_THRESHOLD_UV;
        }
        state->prev_hp_sample = 0.0f;
        state->prev_hp_valid = 0;
    }
    
    return ARM_MATH_SUCCESS;
}

/**
 * Set spike detection threshold for a specific channel
 * @param channel: Channel index
 * @param threshold: Spike detection threshold
 */
void set_spike_threshold(uint8_t channel, float32_t threshold) {
    if (channel < NUM_CHANNELS) {
        ChannelFilterState *state = &channel_states[channel];
        state->threshold = fabsf(threshold);
    }
}

void mode3_filter_reset_state(void) {
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        ChannelFilterState *state = &channel_states[ch];

        memset(state->lp_forward_state_LFP, 0, sizeof(state->lp_forward_state_LFP));
        memset(state->lp_forward_state_ESA, 0, sizeof(state->lp_forward_state_ESA));
        memset(state->hp_state_ESA, 0, sizeof(state->hp_state_ESA));

        arm_biquad_cascade_df2T_init_f32(
            &state->lowpass_forward_LFP,
            LFPlowpass_config.num_stages,
            LFPlowpass_config.coeffs,
            state->lp_forward_state_LFP
        );
        arm_biquad_cascade_df2T_init_f32(
            &state->lowpass_forward_ESA,
            ESAlowpass_config.num_stages,
            ESAlowpass_config.coeffs,
            state->lp_forward_state_ESA
        );
        arm_biquad_cascade_df2T_init_f32(
            &state->highpass_ESA,
            ESAhighpass_config.num_stages,
            ESAhighpass_config.coeffs,
            state->hp_state_ESA
        );

        state->last_spike_time = -MIN_ISI;
        state->prev_hp_sample = 0.0f;
        state->prev_hp_valid = 0;
    }
}


/**
 * Resample signal from 12.5kHz to 1kHz
 * @param input: Input signal
 * @param output: Output resampled signal
 * @param input_length: Input signal length
 * @return: Output signal length
 */
static inline uint32_t resample_signal_12500_to_1000(const float32_t *input, float32_t *output,
                                                     uint32_t input_length) {
    uint32_t output_length = (input_length * RESAMPLE_NUMERATOR) / RESAMPLE_DENOMINATOR;
    for (uint32_t i = 0; i < output_length; i++) {
        uint32_t position_num = i * RESAMPLE_DENOMINATOR;
        uint32_t base_idx = position_num / RESAMPLE_NUMERATOR;
        if ((position_num & 0x1U) == 0U) {
            output[i] = input[base_idx];
        } else {
            output[i] = 0.5f * (input[base_idx] + input[base_idx + 1]);
        }
    }
    return output_length;
}

/**
 * Optimized spike detection with MUA extraction for Mode 3
 * Features: Adaptive thresholding, noise suppression, and efficient peak detection
 * @param signal: High-pass filtered signal (1st order 250Hz)
 * @param mua_data: Output MUA data
 * @param length: Signal length
 * @param channel: Channel index
 * @return: Number of detected spikes
 */
static inline void detect_spikes_and_extract_mua(const float32_t *signal, uint16_t *mua_data, 
                                             uint32_t length, uint8_t channel) { // 25 points 
    ChannelFilterState *state = &channel_states[channel];
    float32_t neg_threshold = -state->threshold;
    uint16_t num_bins = length / MUA_BIN_SIZE_MODE_3; // 3
    uint16_t bin = 0;
    float32_t prev_sample = state->prev_hp_valid ? state->prev_hp_sample : signal[0];
    state->last_spike_time -= length;
    for (uint32_t i = 0; i < length; i++) {
        float32_t current_sample = signal[i];
        if (prev_sample >= neg_threshold &&
            current_sample < neg_threshold &&
            (int32_t)i - state->last_spike_time >= MIN_ISI) {
            state->last_spike_time = i;
            bin = i / MUA_BIN_SIZE_MODE_3; // 0 , 1, 2, 3
            if (num_bins > 0) {
                if (bin >= num_bins) {
                    bin = num_bins - 1;
                }
                mua_data[bin] |= (1 << channel);
            }
        }
        prev_sample = current_sample;
    }
    if (length > 0) {
        state->prev_hp_sample = signal[length - 1];
        state->prev_hp_valid = 1;
    }
}

#define SWAP_SORT(a, b)            \
    do {                           \
        if ((a) > (b)) {           \
            float32_t t = (a);     \
            (a) = (b);             \
            (b) = t;               \
        }                          \
    } while (0)

static inline float32_t calculate_median_16(float32_t *values) { 
    float32_t x0  = values[0];
    float32_t x1  = values[1];
    float32_t x2  = values[2];
    float32_t x3  = values[3];
    float32_t x4  = values[4];
    float32_t x5  = values[5];
    float32_t x6  = values[6];
    float32_t x7  = values[7];
    float32_t x8  = values[8];
    float32_t x9  = values[9];
    float32_t x10 = values[10];
    float32_t x11 = values[11];
    float32_t x12 = values[12];
    float32_t x13 = values[13];
    float32_t x14 = values[14];
    float32_t x15 = values[15];

    SWAP_SORT(x0, x13);  SWAP_SORT(x1, x12);  SWAP_SORT(x2, x15);  SWAP_SORT(x3, x14);
    SWAP_SORT(x4, x8);   SWAP_SORT(x5, x6);   SWAP_SORT(x7, x11);  SWAP_SORT(x9, x10);

    SWAP_SORT(x0, x5);   SWAP_SORT(x1, x7);   SWAP_SORT(x2, x9);   SWAP_SORT(x3, x4);
    SWAP_SORT(x6, x13);  SWAP_SORT(x8, x14);  SWAP_SORT(x10, x15); SWAP_SORT(x11, x12);

    SWAP_SORT(x0, x1);   SWAP_SORT(x2, x3);   SWAP_SORT(x4, x5);   SWAP_SORT(x6, x8);
    SWAP_SORT(x7, x9);   SWAP_SORT(x10, x11); SWAP_SORT(x12, x13); SWAP_SORT(x14, x15);

    SWAP_SORT(x0, x2);   SWAP_SORT(x1, x3);   SWAP_SORT(x4, x10);  SWAP_SORT(x5, x11);
    SWAP_SORT(x6, x7);   SWAP_SORT(x8, x9);   SWAP_SORT(x12, x14); SWAP_SORT(x13, x15);

    SWAP_SORT(x1, x2);   SWAP_SORT(x3, x12);  SWAP_SORT(x4, x6);   SWAP_SORT(x5, x7);
    SWAP_SORT(x8, x10);  SWAP_SORT(x9, x11);  SWAP_SORT(x13, x14);

    SWAP_SORT(x1, x4);   SWAP_SORT(x2, x6);   SWAP_SORT(x5, x8);
    SWAP_SORT(x7, x10);  SWAP_SORT(x9, x13);  SWAP_SORT(x11, x14);

    SWAP_SORT(x2, x4);   SWAP_SORT(x3, x6);   SWAP_SORT(x9, x12);  SWAP_SORT(x11, x13);

    SWAP_SORT(x3, x5);   SWAP_SORT(x6, x8);   SWAP_SORT(x7, x9);   SWAP_SORT(x10, x12);

    SWAP_SORT(x3, x4);   SWAP_SORT(x5, x6);   SWAP_SORT(x7, x8);   SWAP_SORT(x9, x10);
    SWAP_SORT(x11, x12);

    SWAP_SORT(x6, x7);   SWAP_SORT(x8, x9);

    return 0.5f * (x7 + x8);
}

static inline float32_t calculate_median_16_safe(const float32_t *values) {
    float32_t sorted_values[NUM_CHANNELS] ARM_ALIGN;
    for (uint32_t i = 0; i < NUM_CHANNELS; i++) {
        sorted_values[i] = values[i];
    }
    for (uint32_t i = 1; i < NUM_CHANNELS; i++) {
        float32_t key = sorted_values[i];
        int32_t j = (int32_t)i - 1;
        while (j >= 0 && sorted_values[j] > key) {
            sorted_values[j + 1] = sorted_values[j];
            j--;
        }
        sorted_values[j + 1] = key;
    }
    return 0.5f * (sorted_values[(NUM_CHANNELS / 2) - 1] + sorted_values[NUM_CHANNELS / 2]);
}

#define SWAP_SORT_I32(a, b)        \
    do {                           \
        if ((a) > (b)) {           \
            int32_t t = (a);       \
            (a) = (b);             \
            (b) = t;               \
        }                          \
    } while (0)

static inline int32_t calculate_median_pair_sum_16_i16(const int16_t *values) {
    int32_t x0  = values[0];
    int32_t x1  = values[1];
    int32_t x2  = values[2];
    int32_t x3  = values[3];
    int32_t x4  = values[4];
    int32_t x5  = values[5];
    int32_t x6  = values[6];
    int32_t x7  = values[7];
    int32_t x8  = values[8];
    int32_t x9  = values[9];
    int32_t x10 = values[10];
    int32_t x11 = values[11];
    int32_t x12 = values[12];
    int32_t x13 = values[13];
    int32_t x14 = values[14];
    int32_t x15 = values[15];

    SWAP_SORT_I32(x0, x13);  SWAP_SORT_I32(x1, x12);  SWAP_SORT_I32(x2, x15);  SWAP_SORT_I32(x3, x14);
    SWAP_SORT_I32(x4, x8);   SWAP_SORT_I32(x5, x6);   SWAP_SORT_I32(x7, x11);  SWAP_SORT_I32(x9, x10);

    SWAP_SORT_I32(x0, x5);   SWAP_SORT_I32(x1, x7);   SWAP_SORT_I32(x2, x9);   SWAP_SORT_I32(x3, x4);
    SWAP_SORT_I32(x6, x13);  SWAP_SORT_I32(x8, x14);  SWAP_SORT_I32(x10, x15); SWAP_SORT_I32(x11, x12);

    SWAP_SORT_I32(x0, x1);   SWAP_SORT_I32(x2, x3);   SWAP_SORT_I32(x4, x5);   SWAP_SORT_I32(x6, x8);
    SWAP_SORT_I32(x7, x9);   SWAP_SORT_I32(x10, x11); SWAP_SORT_I32(x12, x13); SWAP_SORT_I32(x14, x15);

    SWAP_SORT_I32(x0, x2);   SWAP_SORT_I32(x1, x3);   SWAP_SORT_I32(x4, x10);  SWAP_SORT_I32(x5, x11);
    SWAP_SORT_I32(x6, x7);   SWAP_SORT_I32(x8, x9);   SWAP_SORT_I32(x12, x14); SWAP_SORT_I32(x13, x15);

    SWAP_SORT_I32(x1, x2);   SWAP_SORT_I32(x3, x12);  SWAP_SORT_I32(x4, x6);   SWAP_SORT_I32(x5, x7);
    SWAP_SORT_I32(x8, x10);  SWAP_SORT_I32(x9, x11);  SWAP_SORT_I32(x13, x14);

    SWAP_SORT_I32(x1, x4);   SWAP_SORT_I32(x2, x6);   SWAP_SORT_I32(x5, x8);
    SWAP_SORT_I32(x7, x10);  SWAP_SORT_I32(x9, x13);  SWAP_SORT_I32(x11, x14);

    SWAP_SORT_I32(x2, x4);   SWAP_SORT_I32(x3, x6);   SWAP_SORT_I32(x9, x12);  SWAP_SORT_I32(x11, x13);

    SWAP_SORT_I32(x3, x5);   SWAP_SORT_I32(x6, x8);   SWAP_SORT_I32(x7, x9);   SWAP_SORT_I32(x10, x12);

    SWAP_SORT_I32(x3, x4);   SWAP_SORT_I32(x5, x6);   SWAP_SORT_I32(x7, x8);   SWAP_SORT_I32(x9, x10);
    SWAP_SORT_I32(x11, x12);

    SWAP_SORT_I32(x6, x7);   SWAP_SORT_I32(x8, x9);

    return x7 + x8;
}

static inline int32_t calculate_median_pair_sum_16_i16_safe(const int16_t *values) {
    int16_t sorted_values[NUM_CHANNELS] ARM_ALIGN;
    for (uint32_t i = 0; i < NUM_CHANNELS; i++) {
        sorted_values[i] = values[i];
    }
    for (uint32_t i = 1; i < NUM_CHANNELS; i++) {
        int16_t key = sorted_values[i];
        int32_t j = (int32_t)i - 1;
        while (j >= 0 && sorted_values[j] > key) {
            sorted_values[j + 1] = sorted_values[j];
            j--;
        }
        sorted_values[j + 1] = key;
    }
    return (int32_t)sorted_values[(NUM_CHANNELS / 2) - 1] + (int32_t)sorted_values[NUM_CHANNELS / 2];
}

static inline void apply_common_median_reference(const float32_t *input_data,
                                                 float32_t *output_data,
                                                 uint32_t samples_per_channel) {
    float32_t sample_values[NUM_CHANNELS] ARM_ALIGN;
    for (uint32_t sample_idx = 0; sample_idx < samples_per_channel; sample_idx++) {
        for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
            sample_values[ch] = input_data[ch * samples_per_channel + sample_idx];
        }
        float32_t median;
        if (mode3_esa_reref_enable == MODE3_ESA_REREF_FAST_MEDIAN) {
            median = calculate_median_16(sample_values);
        } else {
            median = calculate_median_16_safe(sample_values);
        }
        for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
            output_data[ch * samples_per_channel + sample_idx] =
                input_data[ch * samples_per_channel + sample_idx] - median;
        }
    }
}

void mode0_mand_reset_state(void) {
    memset(mode0_mand_delay_ring_x2, 0, sizeof(mode0_mand_delay_ring_x2));
    memset(mode0_mand_bin_sums_x2, 0, sizeof(mode0_mand_bin_sums_x2));
    memset(mode0_mand_rolling_sums_x2, 0, sizeof(mode0_mand_rolling_sums_x2));
    memset(mode0_mand_current_bin_sums_x2, 0, sizeof(mode0_mand_current_bin_sums_x2));
    mode0_mand_delay_write_index = 0;
    mode0_mand_samples_in_bin = 0;
    mode0_mand_window_write_index = 0;
    mode0_mand_bins_filled = 0;
    mode0_mand_total_samples = 0;
}

bool process_mode0_mand_chunk(const int16_t *input_counts, uint32_t samples_per_channel,
                              float32_t *mand_output) {
    if (input_counts == NULL || mand_output == NULL || samples_per_channel == 0U) {
        return false;
    }

    int16_t sample_values[NUM_CHANNELS] ARM_ALIGN;
    const uint16_t delay_ring_size = MODE0_MAND_DELAY_RING_SIZE;
    const uint8_t lag = MODE0_MAND_DEFAULT_LAG_SAMPLES;
    const u8_t reref_mode = mode3_esa_reref_enable;
    bool output_ready = false;

    for (uint32_t sample_idx = 0; sample_idx < samples_per_channel; sample_idx++) {
        const uint16_t write_index = mode0_mand_delay_write_index;
        const bool have_delayed_sample = mode0_mand_total_samples >= lag;
        uint16_t delayed_index = 0U;
        if (have_delayed_sample) {
            delayed_index = (write_index >= lag) ?
                            (uint16_t)(write_index - lag) :
                            (uint16_t)(write_index + delay_ring_size - lag);
        }

        if (reref_mode == MODE3_ESA_REREF_DISABLED) {
            for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
                int32_t current_value_x2 = (int32_t)input_counts[ch * samples_per_channel + sample_idx] * 2;
                if (have_delayed_sample) {
                    uint32_t diff_x2 = (uint32_t)abs(current_value_x2 - mode0_mand_delay_ring_x2[ch][delayed_index]);
                    mode0_mand_current_bin_sums_x2[ch] += diff_x2;
                }
                mode0_mand_delay_ring_x2[ch][write_index] = current_value_x2;
            }
        } else {
            for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
                sample_values[ch] = input_counts[ch * samples_per_channel + sample_idx];
            }
            int32_t median_pair_sum;
            if (reref_mode == MODE3_ESA_REREF_SAFE_MEDIAN) {
                median_pair_sum = calculate_median_pair_sum_16_i16_safe(sample_values);
            } else {
                median_pair_sum = calculate_median_pair_sum_16_i16(sample_values);
            }
            for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
                int32_t current_value_x2 = ((int32_t)sample_values[ch] * 2) - median_pair_sum;
                if (have_delayed_sample) {
                    uint32_t diff_x2 = (uint32_t)abs(current_value_x2 - mode0_mand_delay_ring_x2[ch][delayed_index]);
                    mode0_mand_current_bin_sums_x2[ch] += diff_x2;
                }
                mode0_mand_delay_ring_x2[ch][write_index] = current_value_x2;
            }
        }

        mode0_mand_delay_write_index++;
        if (mode0_mand_delay_write_index >= delay_ring_size) {
            mode0_mand_delay_write_index = 0U;
        }
        mode0_mand_total_samples++;
        mode0_mand_samples_in_bin++;

        if (mode0_mand_samples_in_bin >= MODE0_MAND_SAMPLES_PER_BIN) {
            uint16_t divisor_bins = mode0_mand_bins_filled;
            if (divisor_bins < MODE0_MAND_WINDOW_BINS) {
                divisor_bins++;
            }
            for (uint32_t ch = 0; ch < NUM_CHANNELS; ch++) {
                uint32_t old_sum = mode0_mand_bin_sums_x2[ch][mode0_mand_window_write_index];
                mode0_mand_bin_sums_x2[ch][mode0_mand_window_write_index] = mode0_mand_current_bin_sums_x2[ch];
                mode0_mand_rolling_sums_x2[ch] += mode0_mand_current_bin_sums_x2[ch] - old_sum;
                mand_output[ch] = ((float32_t)mode0_mand_rolling_sums_x2[ch] * RHD2132_ADC_UV_PER_COUNT * Filter_scale) /
                                  (2.0f * (float32_t)divisor_bins * (float32_t)MODE0_MAND_SAMPLES_PER_BIN);
                mode0_mand_current_bin_sums_x2[ch] = 0U;
            }
            if (mode0_mand_bins_filled < MODE0_MAND_WINDOW_BINS) {
                mode0_mand_bins_filled++;
            }
            mode0_mand_window_write_index++;
            if (mode0_mand_window_write_index >= MODE0_MAND_WINDOW_BINS) {
                mode0_mand_window_write_index = 0U;
            }
            mode0_mand_samples_in_bin = 0;
            output_ready = true;
        }
    }

    return output_ready;
}

/**
 * Main neural signal processing function for Mode 3 (LFP, MUA, ESA)
 * @param input_data: Input neural data [channels x samples] at 12.5kHz
 * @param samples_per_channel: Number of samples per channel
 * @param lfp_output: Output LFP data (2nd order lowpass 300Hz + resample to 1kHz)
 * @param mua_data: Output MUA data (spike detection after 1st order highpass 250Hz and optional median reref)
 * @param esa_output: Output ESA data (1st order highpass 250Hz -> optional median reref -> rectify -> 1st order lowpass 150Hz + resample to 1kHz)
 * @return: Number of output samples after resampling for LFP and ESA
 */
uint32_t process_neural_signals_mode3(const float32_t *input_data, uint32_t samples_per_channel,
                                     float32_t *lfp_output, uint16_t *mua_data, float32_t *esa_output) {
    if (input_data == NULL || lfp_output == NULL || mua_data == NULL || esa_output == NULL ||
        samples_per_channel == 0 || samples_per_channel > CHUNK_SIZE) {
        return 0;
    }

    // Clear MUA output
    uint32_t mua_bins = samples_per_channel / MUA_BIN_SIZE_MODE_3;
    memset(mua_data, 0, mua_bins * sizeof(uint16_t));

    const uint32_t sample_count = NUM_CHANNELS * samples_per_channel;
    for (uint32_t i = 0; i < sample_count; i++) {
        mode3_sanitized_input_buffer[i] = mode3_sanitize_input_uv(input_data[i]);
    }

    uint32_t decimated_length = process_lfp_lowpass_decimate(mode3_sanitized_input_buffer, samples_per_channel, lfp_output);

    // First high-pass every channel, then optionally common-median reference the high-pass output.
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        const float32_t *ch_esa_input = &mode3_sanitized_input_buffer[ch * samples_per_channel];
        ChannelFilterState *state = &channel_states[ch];

        // =============================== HIGH-PASS FILTERING for MUA and ESA: 1st order IIR highpass 250Hz ===
        arm_biquad_cascade_df2T_f32(
            &state->highpass_ESA,
            ch_esa_input,
            &highpass_buffer_ESA[ch * samples_per_channel],
            samples_per_channel
        );

        // Apply highpass filter gain
        for(int i = 0; i < samples_per_channel; i++){
            highpass_buffer_ESA[ch * samples_per_channel + i] *= state->highpass_gain_ESA;
        }
    }

    const float32_t *esa_input = highpass_buffer_ESA;
    if (mode3_esa_reref_enable != MODE3_ESA_REREF_DISABLED) {
        apply_common_median_reference(highpass_buffer_ESA, reref_buffer_ESA, samples_per_channel);
        esa_input = reref_buffer_ESA;
    }

    // Process each channel after optional post-highpass reref.
    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        const float32_t *ch_esa_input = &esa_input[ch * samples_per_channel];
        ChannelFilterState *state = &channel_states[ch];

        // // === MUA PROCESSING: Spike detection on high-pass filtered data ===  2/3 ms 一个 bin
        detect_spikes_and_extract_mua(
            ch_esa_input,
            mua_data,
            samples_per_channel,
            ch
        );
        
        // === ESA PROCESSING: Rectification + 1st order lowpass 150Hz + resample to 1kHz ===
        // Rectification (absolute value)
        for(int i = 0; i < samples_per_channel; i++){
            rectified_buffer_ESA[ch * samples_per_channel + i] = fabsf(ch_esa_input[i]);
        }
        
        // ESA lowpass filtering: 1st order IIR lowpass 150Hz
        arm_biquad_cascade_df2T_f32(
            &state->lowpass_forward_ESA,
            &rectified_buffer_ESA[ch * samples_per_channel],
            &lowpass_buffer_ESA[ch * samples_per_channel],
            samples_per_channel
        );
        
        // Resample ESA to target sampling rate (1kHz)
        resample_signal_12500_to_1000(&lowpass_buffer_ESA[ch * samples_per_channel],
                                      &esa_output[ch * decimated_length],
                                      samples_per_channel);
        
        // Apply ESA filter gain
        for(int i = 0; i < decimated_length; i++){
            esa_output[ch * decimated_length + i] *= state->lowpass_gain_ESA;
        }
    }
    return decimated_length;
}

uint32_t process_lfp_lowpass_decimate(const float32_t *input_data, uint32_t samples_per_channel,
                                     float32_t *lfp_output) {
    if (samples_per_channel > MAX_SAMPLES_PER_CHANNEL || samples_per_channel == 0) {
        return 0;
    }

    float32_t channel_lfp_buffer[MAX_SAMPLES_PER_CHANNEL] ARM_ALIGN;
    uint32_t decimated_length = (samples_per_channel * RESAMPLE_NUMERATOR) / RESAMPLE_DENOMINATOR;

    for (int ch = 0; ch < NUM_CHANNELS; ch++) {
        const float32_t *ch_input = &input_data[ch * samples_per_channel];
        ChannelFilterState *state = &channel_states[ch];

        arm_biquad_cascade_df2T_f32(
            &state->lowpass_forward_LFP,
            ch_input,
            channel_lfp_buffer,
            samples_per_channel
        );

        resample_signal_12500_to_1000(channel_lfp_buffer,
                                      &lfp_output[ch * decimated_length],
                                      samples_per_channel);

        for (uint32_t i = 0; i < decimated_length; i++) {
            lfp_output[ch * decimated_length + i] *= state->lowpass_gain_LFP;
        }
    }
    return decimated_length;
}
