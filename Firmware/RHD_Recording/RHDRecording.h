#ifndef RHDRecording_h
#define RHDRecording_h

/*
- Functions
    1. RHD initiation
    2. RHD Recording
*/
#include <stdbool.h>
#include <stdint.h>
// nrfx library
#include <nrfx.h>
#include <nrfx_timer.h>
#include <nrfx_ppi.h>
#include <helpers/nrfx_gppi.h>
#include <nrfx_gpiote.h>
#include <nrfx_spim.h>
#include <zephyr/kernel.h>

#include <zephyr/logging/log.h>

/*
 * typedef
 */
typedef unsigned char u8_t;
typedef unsigned short u16_t;
typedef unsigned int u32_t;

#define SAMPLE_MODE_MODE0_LFP          0U
#define SAMPLE_MODE_MODE1_SPIKE        1U
#define SAMPLE_MODE_MODE2_RAW_V2       2U
#define SAMPLE_MODE_MODE3_ESA_MUA      3U

static inline bool mode_uses_shared_raw_spi_source(u16_t mode)
{
	return mode == SAMPLE_MODE_MODE3_ESA_MUA;
}

static inline bool mode_uses_mode0_raw_spi_source(u16_t mode)
{
	return mode == SAMPLE_MODE_MODE0_LFP;
}

static inline bool mode_uses_mode2_raw_spi_source(u16_t mode)
{
	return mode == SAMPLE_MODE_MODE2_RAW_V2;
}

static inline bool mode_uses_20k_spike_spi_source(u16_t mode)
{
	return mode == SAMPLE_MODE_MODE1_SPIKE;
}

#define GPIOE_INST NRF_DT_GPIOTE_INST(DT_NODELABEL(led0), gpios) // 获得 gpio0 这个实例
// macro define
#define NUM_CHANNELS 16      // Number of neural signal channels
// fortest
#define TEST_CHANNEL 16 

/***************************************SPI marco define***************************************************/
#define NRFX_SPIM_SCK_PIN 14  // 14 // aviod to use Standard drive, low frequency I/O only PIN
#define NRFX_SPIM_MOSI_PIN 5 // 5 for 16 channels // 22 for 32 channels
#define NRFX_SPIM_MISO_PIN 4 // 25 for 32 channels
#define NRFX_SPIM_SS_PIN 15	  // 15 //

#define SPI_INSTANCE 3 /**< SPI instance index.SPI3 */
#define SPI_INSTANCE_INIT 2 /**< SPI instance index.SPI2 */
#define ISR_PRIORITY 2

#define time_window 1 // each time_window has 100 samples every channel;using the parameter to reduce time resolution and increase the limitation of wireless communication time.

/*********************************** mode 0 ************************************/
/* for LFP 16 channels raw data */
// 这里先做12.5Khz的采样，250Hz的upper freq，然后进行 2nd 的250Hz的低通数字滤波后重采样得到1KHz；
// 19 commands as one command packet; the bin size is 100 samples every channel
#define SAMPLE_POINT_NUM 50 // 4（1KHz）对应 4ms 原始窗口（12.5KHz 下为 50 点）
#define MODE0_SPI_CHUNK_SAMPLES 25 // mode0 DMA chunk: 2ms at 12.5kHz; two chunks form one 50-sample packet.
#define CONVERT_FASHION_NUM 16 // 16 channels; 
#define MODE0_SPI_CHUNK_WORDS (NUM_CHANNELS * MODE0_SPI_CHUNK_SAMPLES)
#define MODE0_PACKET_CHUNKS (SAMPLE_POINT_NUM / MODE0_SPI_CHUNK_SAMPLES)
#if (SAMPLE_POINT_NUM % MODE0_SPI_CHUNK_SAMPLES) != 0
#error "Mode0 packet samples must be divisible by the SPI chunk samples"
#endif
#define SPI_TX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
#define SPI_RX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
#define m_length 2

extern u16_t init_tx_buf[1];
extern u16_t init_rx_buf[1];
// 注意： 这里*2，来防止在采样过程中偶尔出现的数据溢出问题
#define LFP_TX_BUFFER_SIZE  SPI_TX_BUF_SIZE * 2
#define LFP_RX_BUFFER_SIZE  SPI_RX_BUF_SIZE * 2
extern u16_t mode_0_m_tx_buf[LFP_TX_BUFFER_SIZE];
extern u16_t mode_0_m_rx_buf[2][LFP_RX_BUFFER_SIZE];

/*********************************** mode 1 legacy spike ************************************/
/* Mode1 keeps the 20kHz spike buffer path. Mode2 v2 uses the shared 16-channel raw buffer path. */
#define SPIKE_SAMPLE_POINT_NUM 90 
#define SPIKE_CONVERT_FASHION_NUM 16
#define SPIKE_SPI_TX_BUF_SIZE (SPIKE_CONVERT_FASHION_NUM * SPIKE_SAMPLE_POINT_NUM)
#define SPIKE_SPI_RX_BUF_SIZE (SPIKE_CONVERT_FASHION_NUM * SPIKE_SAMPLE_POINT_NUM)

#define MUA_BIN_SIZE 18 // must be the factor of @SPIKE_SAMPLE_POINT_NUM : ~ 1ms in 20khz sampling

#define SPIKE_TX_BUFFER_SIZE  SPIKE_SPI_TX_BUF_SIZE * 2
#define SPIKE_RX_BUFFER_SIZE  SPIKE_SPI_RX_BUF_SIZE * 2

extern u16_t spike_m_tx_buf[SPIKE_TX_BUFFER_SIZE]; // ppi convert command; /**< TX buffer. */
extern u16_t spike_m_rx_buf[2][SPIKE_RX_BUFFER_SIZE]; /*< RX buffer. double buffer >*/

extern u16_t spike_channel_array_t[SPIKE_SAMPLE_POINT_NUM];
extern u16_t spike_channel_array[16][SPIKE_SAMPLE_POINT_NUM];	

extern u16_t MutiUnitActivityArray[(SPIKE_SAMPLE_POINT_NUM / MUA_BIN_SIZE)]; // 21 sample each MUA bin. 105 sample is equal to 5 bins and 16 channels correspond to 5 shorts ,given that 1bit/sample

/*********************************** mode 3 ************************************/
// 2025.10.23：增加 mode3，只做所有通道的spike detection，根据之间的研究结果，使用12.5Khz的采样对detection的影响很小【ref】；
// 使用mode1 来更新threshold； mode3常开来同时获得 ESA 和 spike events
// LFP: 使用 2阶 IIR 分离出250Hz low pass, resample to 1Khz
// ESA: 使用 1阶 IIR 250Hz 高通，之后整流，再12 hz 1阶 IIR 低通 之后重采样到1Khz之后上传到 host；
// MUA: raw data 高通后数据online spike detection，压缩为MUA 上传到host
// 使用3-axis IMU数据，功耗期望控制在30-40 mW左右
// 一个包：16 * 2 （LFP 1Khz）+ 16 * 2 （ESA 1Khz） + 1 （head）+ 2 (timestamp) + 1 (flag) + 6 (IMU) + 3 (battery) + 3 (raster)
#define CHUNK_SIZE  MODE0_SPI_CHUNK_SAMPLES // mode0/mode3: 2ms at 12.5kHz; mode2 v2: 25 raw points at its active SPI interval.
#define MODE_3_OUTPUT_POINTS_PER_CHANNEL 2
#define MODE_3_LFP_SIZE (NUM_CHANNELS * MODE_3_OUTPUT_POINTS_PER_CHANNEL)
#define MODE_3_ESA_SIZE (NUM_CHANNELS * MODE_3_OUTPUT_POINTS_PER_CHANNEL)
#define MODE_3_SPI_TX_BUF_SIZE (NUM_CHANNELS * CHUNK_SIZE)
#define MODE_3_SPI_RX_BUF_SIZE (NUM_CHANNELS * CHUNK_SIZE)

#define MODE_3_TX_BUFFER_SIZE  MODE_3_SPI_TX_BUF_SIZE * 2
#define MODE_3_RX_BUFFER_SIZE  MODE_3_SPI_RX_BUF_SIZE * 2

extern u16_t mode_3_m_tx_buf[MODE_3_TX_BUFFER_SIZE]; // ppi convert command; /**< TX buffer. */
extern u16_t mode_3_m_rx_buf[2][MODE_3_RX_BUFFER_SIZE]; /*< RX buffer. double buffer >*/
extern u16_t mode_2_m_tx_buf[MODE_3_TX_BUFFER_SIZE];
extern u16_t mode_2_m_rx_buf[2][MODE_3_RX_BUFFER_SIZE];

extern u16_t mode_3_array_t[MODE_3_SPI_RX_BUF_SIZE]; // raw data array, 25 points; 12.5Khz
extern u16_t mode_3_array_lfp_t[MODE_3_LFP_SIZE]; // downsampled lfp data array
extern u16_t mode_3_array_ESA_t[MODE_3_ESA_SIZE]; // downsampled ESA data array
/*************spi init***************/
extern const nrfx_spim_t spi; /**< SPI instance. */
extern const nrfx_spim_t spi_init; /**< SPI instance. */

extern volatile uint32_t spi_overflow_flag;
//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
/***************************************RHD marco define***************************************************/
// initiation: 1KHz; LFP recording 
extern u16_t RHD_command;
extern const u16_t NINE_DUMMPY[9];
// ADC self-calibration command ,this command needs nine dummy command to generate the necessary clock cycles to run.
// default register setting and must be reinitiated by GUI
#define CALIBRATE 0x0055
#define CLEAR 0x006A	 // not necessary to use this command
						 // Registers configuration using write command
#define Register0_disable 0xc280 // amp fast settle is 0  ,disable ADC AND amp to reduce power
/************************ mode0: lfp 12.5khz sampling source setting ************************/                         
#define lfp_Register0_enable 0xde80 // amp fast settle is 0, enable ADC

#define lfp_Register1 0x0881 // VDD sense disable ,using 16 * 12.5 KS/s ADC
#define lfp_Register2 0x2082 // MUX bias current, configuration as above

#define lfp_Register3 0x0083 // diable tempS and digout
// absmode disable + unsigned offset binary notation ADC + weak MISO + DSP high-pass filter enable(1.2Hz upper bandwidth) // 0x9884
// #define lfp_Register4 0x8084 // disable DSP 
#define lfp_Register4 0x9b84 // DSP cutoff freq: 11 -> 0.9Hz at 12500 Sampling rate
// Impedance check
#define lfp_Register5 0x0085 // Impedance check control ,which is disable
#define lfp_Register6 0x0086 // DAC output voltage ,there is 0
#define lfp_Register7 0x0087 // Impedance check electrode select, this is 0
// on-chip Amplifier bandwidth Select
//     //  using 250 Hz upper bandwidth; use on-chip programmable resistors
#define lfp_Register8 0x2a88
#define lfp_Register9 0x0a89
#define lfp_Register10 0x058a
#define lfp_Register11 0x0d8b
//     //  using 1 Hz lower bandwidth; use on-chip programmable resistors
#define lfp_Register12 0x2c8c 
#define lfp_Register13 0x068d
// 0.1Hz
// #define lfp_Register12 0x108c
// #define lfp_Register13 0x7c8d
// indicidual Amplifier Power ,channel 0-31 set to one for using these channels' Amplifier
#define lfp_Register14 0x008e
#define lfp_Register15 0xff8f
#define lfp_Register16 0xff90
#define lfp_Register17 0x0091

/************************ mode3: spike 12.5khz sampling setting with DSP*******************/ 
#define spike_mode3_Register1 0x0881 // VDD sense disable ,using 16 * 12.5KS/s ADC  8
#define spike_mode3_Register2 0x2082 // MUX bias current, configuration as above    32
// #define spike_mode3_Register1 0x0481 
// #define spike_mode3_Register2 0x1282 
// DSP high-pass filter for DC offset removal
#define spike_mode3_Register4 0x9b84  // DSP cutoff freq: 11 -> 0.9Hz at 12500 Sampling rate
// DSP disable
// #define spike_mode3_Register4 0x8084 
#define impedance_Register4 0x8084

/************************ mode1: spike 20khz sampling setting *******************/ 
#define spike_Register0_enable 0xde80
#define spike_Register1 0x0481 // VDD sense disable ,using 16 * 20KS/s ADC
#define spike_Register2 0x1282 // MUX bias current, configuration as above
#define spike_Register3 0x0083 // diable tempS and digout
// #define spike_Register4 0x8084
#define spike_Register4 0x9384 // absmode disable + unsigned offset binary notation ADC + weak MISO + DSP high-pass filter enable(373Hz upper bandwidth at 17544 Hz)
// Impedance check
#define spike_Register5 0x0085 // Impedance check control ,which is disable
#define spike_Register6 0x0086 // DAC output voltage ,there is 0
#define spike_Register7 0x0087 // Impedance check electrode select, this is 0
// on-chip Amplifier bandwidth Select
#define spike_Register8 0x2188 //  using 1Hz -5K Hz bandwidth
#define spike_Register9 0x8089
#define spike_Register10 0x258a
#define spike_Register11 0x808b
#define spike_Register12 0x2c8c
#define spike_Register13 0x068d
// indicidual Amplifier Power ,all set to one for using all channels' Amplifier
#define spike_Register14 0x008e
#define spike_Register15 0xff8f
#define spike_Register16 0xff90
#define spike_Register17 0x0091
/************************ mode2: spike 20khz sampling setting without DSP*******************/ 
#define spike_raw_Register4 0x9b84 // DSP cutoff freq: 11 -> 1.5Hz at 20000 Sampling rate for DC offset removal

extern const u16_t Register_config_lfp[18];

extern const u16_t Register_config_spike[18];

extern const nrfx_gpiote_t gpiote_instance; 

extern uint8_t gpiote_channel;

/***************************************Timer marco define***************************************************/
extern const nrfx_timer_t RHD_timer_nRFX;
extern const nrfx_timer_t SPI_timer_RESET;

extern uint32_t timer_period; 
extern uint32_t reset_ticks_value;

extern volatile bool spi_buff_flag;
extern volatile bool buffer_is_full;
extern volatile bool spi_ready_buff_flag;
extern volatile uint32_t spi_ready_overflow_flag;
extern struct k_sem rhd_spi_ready_sem;

extern u16_t sampe_mode; // lfp or spike

/***************************************PPI DMA marco define***************************************************/
extern uint8_t gp_channel_1;
extern uint8_t gp_channel_2;
extern uint8_t gp_channel_3;

extern k_tid_t mainThread;

extern bool sample_switch;
/*************************************** function ***************************************************/
uint32_t CS_Gpiote_init(void); // init gpiote: control spi cs line with ppi

u16_t swapShort16(u16_t shortValue); // swap upper byte and lower byte in a u16_t

void swap_bytes_builtin(uint16_t* arr, size_t len);

// spi
uint32_t spim_init(void); // init spim3 

uint32_t rhdspi_init(void);

void spi_transmit_buffer_reset();

u16_t spi_trans(u16_t command);

// timer
void SPI_timer_event_handler(nrf_timer_event_t event_type, void *p_context);

uint32_t timer_init(uint32_t timer_period, uint32_t reset_ticks_value);

void timer_stop();

void timer_start();

// ppi
uint32_t ppi_init(); // TimerRHD -> SPIM3 -> GPIOTE -> TimerReset

// RHD init
u16_t Writecommand_generator(u8_t regnum, u8_t data);

u16_t Convertcommand_generator(u8_t channel, u8_t reset);

u16_t Readcommand_generator(u8_t regnum);

u16_t Calibrate();

void RHD_tx_buf_setup();

int RHD_init(const u16_t *Register_config);

#endif
