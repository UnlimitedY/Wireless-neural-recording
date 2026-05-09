/*
 * ARM CMSIS DSP Optimized Neural Signal Processing
 * - Utilizes ARM CMSIS DSP library for maximum performance
 * - Multi-stage filtering with decimation (3 stages)
 * - SIMD optimizations for ARM Cortex-M processors
 * - Memory-aligned data structures for cache efficiency
 */
#ifndef OnlineFilter_h
#define OnlineFilter_h

#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

#include "RHDRecording.h"
#include "arm_math.h"
#define FLOAT32_MAX_SAFE 1e6f      
#define FLOAT32_MIN_SAFE -1e6f 
#define Filter_scale 1.0f // 给所有输出filer的值在转为uV后乘以 0.1

#define ORIGINAL_FS 12500    // Original sampling rate 12.5kHz
#define TARGET_FS 1000       // Target sampling rate after low-pass filtering
#define RESAMPLE_NUMERATOR 2
#define RESAMPLE_DENOMINATOR 25
#define MODE3_DECIMATED_SAMPLES_PER_CHANNEL ((CHUNK_SIZE * RESAMPLE_NUMERATOR) / RESAMPLE_DENOMINATOR)
#define MAX_FILTER_ORDER 4   // Maximum filter order
#define MAX_SAMPLES_PER_CHANNEL SPIKE_SAMPLE_POINT_NUM

// Spike detection parameters
#define MIN_ISI 10           // Minimum inter-spike interval (samples)
#define SPIKE_WINDOW 3       // Spike detection window size
#define MUA_BIN_SIZE_MODE_3 8      // MUA binning size (samples)

#define IIR_ORDER_lowpass_LFP 2          // IIR filter order for LFP
#define IIR_ORDER_lowpass_ESA 1          // IIR filter order for ESA
#define IIR_ORDER_highpass_ESA 1         // IIR filter order for ESA

#define IIR_CUTOFF_lowpass_LFP 300.0f          // IIR filter cutoff 相位延迟 最大1ms （12.5kHz 输入采样）
#define IIR_CUTOFF_lowpass_ESA 150.0f          // IIR filter cutoff
#define IIR_CUTOFF_highpass_ESA 250.0f          // IIR filter cutoff

// Memory alignment for ARM NEON/SIMD
#define ARM_ALIGN __attribute__((aligned(16)))

#define RHD2132_ADC_REF_VOLTAGE_v   1.225f    // 参考电压
#define RHD2132_ADC_GAIN    192.f      // 16位ADC
#define RHD2132_ADC_REF_VOLTAGE    RHD2132_ADC_REF_VOLTAGE_v / RHD2132_ADC_GAIN * 1000000.f
#define RHD2132_ADC_UV_PER_COUNT   (RHD2132_ADC_REF_VOLTAGE_v * 2.0f / 65536.0f / RHD2132_ADC_GAIN * 1000000.0f)
#define RHD2132_ADC_FULL_SCALE_UV RHD2132_ADC_REF_VOLTAGE
#define DEFAULT_SPIKE_THRESHOLD_UV 60.0f
#define DEFAULT_SPIKE_THRESHOLD_COUNTS ((uint16_t)((DEFAULT_SPIKE_THRESHOLD_UV / RHD2132_ADC_UV_PER_COUNT) + 0.5f))
#define MODE0_MAND_DEFAULT_LAG_SAMPLES 7U
#define MODE0_MAND_DEFAULT_WINDOW_MS 4U
#define MODE0_MAND_STEP_MS 4U
#define MODE0_MAND_SAMPLES_PER_BIN ((ORIGINAL_FS * MODE0_MAND_STEP_MS) / 1000U)
#define MODE0_MAND_WINDOW_BINS (MODE0_MAND_DEFAULT_WINDOW_MS / MODE0_MAND_STEP_MS)
#define MODE0_MAND_DELAY_RING_SIZE (MODE0_MAND_DEFAULT_LAG_SAMPLES + 1U)

extern float scale_factor; // uv
#define MODE3_ESA_REREF_DISABLED 0
#define MODE3_ESA_REREF_FAST_MEDIAN 1
#define MODE3_ESA_REREF_SAFE_MEDIAN 2
extern u8_t mode3_esa_reref_enable;

// Filter configuration structure
typedef struct {
    uint8_t order;           // Filter order
    float32_t cutoff_freq;   // Cutoff frequency in Hz
    float32_t sampling_rate; // Sampling rate in Hz
    float32_t coeffs[MAX_FILTER_ORDER * 5]; // Biquad coefficients [b0,b1,b2,a1,a2] per stage
    uint8_t num_stages;      // Number of biquad stages
    // filter gain
    float32_t gain; 
} FilterConfig;

// Filter state structure for each channel
typedef struct {
    arm_biquad_casd_df1_inst_f32 lowpass_forward_LFP;   // Forward low-pass filter

    arm_biquad_casd_df1_inst_f32 lowpass_forward_ESA;   // Forward low-pass filter
    arm_biquad_casd_df1_inst_f32 highpass_ESA;          // High-pass filter
    // filter gain
    float32_t lowpass_gain_LFP;
    float32_t lowpass_gain_ESA;
    float32_t highpass_gain_ESA;
    
    // State buffers
    float32_t lp_forward_state_LFP[MAX_FILTER_ORDER * 2] ARM_ALIGN;
    float32_t lp_forward_state_ESA[MAX_FILTER_ORDER * 2] ARM_ALIGN;
    float32_t hp_state_ESA[MAX_FILTER_ORDER * 2] ARM_ALIGN;
    
    // Spike detection state
    int32_t last_spike_time;
    float32_t threshold;
    float32_t prev_hp_sample;
    uint8_t prev_hp_valid;
} ChannelFilterState;

// Global filter configurations
extern FilterConfig LFPlowpass_config;
extern FilterConfig ESAlowpass_config;
extern FilterConfig ESAhighpass_config;

// Channel filter states
extern ChannelFilterState channel_states[NUM_CHANNELS] ARM_ALIGN;

// Processing buffers
extern float32_t input_buffer[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;

extern float32_t lowpass_buffer_LFP[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
extern float32_t decimated_buffer_LFP[NUM_CHANNELS * MODE3_DECIMATED_SAMPLES_PER_CHANNEL] ARM_ALIGN;

extern float32_t lowpass_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
extern float32_t highpass_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
extern float32_t rectified_buffer_ESA[NUM_CHANNELS * CHUNK_SIZE] ARM_ALIGN;
extern float32_t decimated_buffer_ESA[NUM_CHANNELS * MODE3_DECIMATED_SAMPLES_PER_CHANNEL] ARM_ALIGN;

extern uint16_t mua_output[CHUNK_SIZE/MUA_BIN_SIZE_MODE_3] ARM_ALIGN; // mode3 raster bins

/******************************************************************************************* */
// Function prototypes
/**
 * Calculate Butterworth filter coefficients Low pass for LFP (2nd order, 250Hz)
 * @param config: Filter configuration structure 
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_butterworth_coeffs_LFP(FilterConfig *config);

/**
 * Calculate Butterworth filter coefficients Low pass for ESA (1st order, 12Hz)
 * @param config: Filter configuration structure 
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_butterworth_coeffs_ESA(FilterConfig *config);

/**
 * Calculate high-pass Butterworth filter coefficients for ESA (1st order, 250Hz)
 * @param config: Filter configuration structure
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status calculate_highpass_butterworth_coeffs_ESA(FilterConfig *config);

/**
 * Initialize low-pass filter for LFP (2nd order, 250Hz)
 * @param order: Filter order (must be even)
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_lowpass_filter_LFP(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate);

/**
 * Initialize low-pass filter for ESA (1st order, 12Hz)
 * @param order: Filter order
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_lowpass_filter_ESA(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate);

/**
 * Initialize high-pass filter for ESA (1st order, 250Hz)
 * @param order: Filter order
 * @param cutoff_freq: Cutoff frequency in Hz
 * @param sampling_rate: Sampling rate in Hz
 * @return: ARM_MATH_SUCCESS on success
 */
arm_status init_highpass_filter_ESA(uint8_t order, float32_t cutoff_freq, float32_t sampling_rate);

/**
 * Set spike detection threshold for a specific channel
 * @param channel: Channel index
 * @param threshold: Spike detection threshold
 */
void set_spike_threshold(uint8_t channel, float32_t threshold);

/**
 * Resample 12.5kHz signal to 1kHz
 * @param input: Input signal
 * @param output: Output resampled signal
 * @param input_length: Input signal length
 * @return: Output signal length
 */
static inline uint32_t resample_signal_12500_to_1000(const float32_t *input, float32_t *output, uint32_t input_length);

/**
 * ARM CMSIS DSP optimized spike detection with MUA extraction
 * @param signal: High-pass filtered signal
 * @param mua_data: Output MUA data
 * @param length: Signal length
 * @param channel: Channel index
 * @return: Number of detected spikes
 */
static inline void detect_spikes_and_extract_mua(const float32_t *signal, uint16_t *mua_data, uint32_t length, uint8_t channel);

/**
 * Main neural signal processing function for 12.5kHz input
 * Generates three outputs:
 * 1. LFP: 2nd order IIR lowpass (250Hz) + resample -> 1kHz
 * 2. ESA: 1st order IIR highpass (250Hz) -> rectify -> 1st order IIR lowpass (12Hz) + resample -> 1kHz  
 * 3. MUA: 1st order IIR highpass (250Hz) -> spike detection -> MUA bins
 * @param input_data: Input neural data [channels x samples] at 12.5kHz
 * @param samples_per_channel: Number of samples per channel
 * @param lfp_output: Output LFP data (resampled to 1kHz)
 * @param esa_output: Output ESA data (resampled to 1kHz)
 * @param mua_data: Output MUA data (spike events in bins)
 * @return: Number of output samples after decimation
 */
uint32_t process_neural_signals_mode3(const float32_t *input_data, uint32_t samples_per_channel,
                                     float32_t *lfp_output, uint16_t *mua_data, float32_t *esa_output);

uint32_t process_lfp_lowpass_decimate(const float32_t *input_data, uint32_t samples_per_channel,
                                     float32_t *lfp_output);

void mode0_mand_reset_state(void);
bool process_mode0_mand_chunk(const int16_t *input_counts, uint32_t samples_per_channel,
                              float32_t *mand_output);
void mode3_filter_reset_state(void);

// ADC converter int16 -> float
void convert_rhd2132_samples(u16_t* adc_data, float_t* float_data, uint32_t num_samples, float32_t filter_scale, bool inttofloat);

// saturation protection: 检查 filter state： 出现溢出就清零
void saturation_protection(float32_t data ,int channel, int lowpass);

#endif
