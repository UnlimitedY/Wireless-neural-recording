#include "RHDRecording.h"

K_SEM_DEFINE(rhd_spi_ready_sem, 0, 1);

// GCC/Clang 内置函数
void swap_bytes_builtin(uint16_t *arr, size_t len) {
  for (size_t i = 0; i < len; i++) {
    arr[i] = __builtin_bswap16(arr[i]);
  }
}

uint32_t CS_Gpiote_init(void) {
  // 注意：zepher 自动初始化了就不需要再初始化 gpiote；后续直接使用 gpiote
  // driver 来配置 即可
  nrfx_err_t cs_err;

  nrfx_gpiote_output_config_t cs_config =
      NRFX_GPIOTE_DEFAULT_OUTPUT_CONFIG; // cs pin initial state is high(disable
                                         // cs)

  nrfx_gpiote_channel_alloc(&gpiote_instance, &gpiote_channel);
  nrfx_gpiote_task_config_t task_config = {
      .task_ch = gpiote_channel,
      .polarity = NRF_GPIOTE_POLARITY_TOGGLE,
      .init_val = 1,
  };

  cs_err = nrfx_gpiote_output_configure(&gpiote_instance, NRFX_SPIM_SS_PIN,
                                        &cs_config, &task_config);
  if (cs_err != NRFX_SUCCESS) {
    // LOG_ERR("Gpiote configure failed!\n");
    return 1;
  }

  nrfx_gpiote_out_task_enable(&gpiote_instance, NRFX_SPIM_SS_PIN);
  nrfx_gpiote_out_set(&gpiote_instance, NRFX_SPIM_SS_PIN);

  return 0;
}

u16_t swapShort16(u16_t shortValue) {
  u16_t result;
  u16_t value;
  value = shortValue;
  result = ((value & 0x00FF) << 8) | ((value & 0xFF00) >> 8);
  return result;
}

uint32_t spim_init(void) {
  uint32_t err = 0;
  // spi transmit
  nrfx_spim_config_t spi_config =
      NRFX_SPIM_DEFAULT_CONFIG(NRFX_SPIM_SCK_PIN, NRFX_SPIM_MOSI_PIN,
                               NRFX_SPIM_MISO_PIN, NRF_SPIM_PIN_NOT_CONNECTED);

  spi_config.frequency =
      NRFX_MHZ_TO_HZ(16); // 注意 这里nrfx 和nrf 的spi 定义数据类型不一样
  spi_config.bit_order = NRF_SPIM_BIT_ORDER_MSB_FIRST;

  err =
      nrfx_spim_init(&spi, &spi_config, NULL, NULL); // disable spi interruption
  if (err != NRFX_SUCCESS) {
    // LOG_ERR("nrfx err transmit: %d\n", err);
    return err;
  }
  return 0;
}

uint32_t rhdspi_init(void) {
  uint32_t err = 0;
  // spi init
  nrfx_spim_config_t spi_config_init =
      NRFX_SPIM_DEFAULT_CONFIG(NRFX_SPIM_SCK_PIN, NRFX_SPIM_MOSI_PIN,
                               NRFX_SPIM_MISO_PIN, NRFX_SPIM_SS_PIN);
  spi_config_init.frequency = NRFX_MHZ_TO_HZ(8);
  spi_config_init.bit_order = NRF_SPIM_BIT_ORDER_MSB_FIRST;

  err = nrfx_spim_init(&spi_init, &spi_config_init, NULL,
                       NULL); // disable spi interruption
  if (err != NRFX_SUCCESS) {
    // LOG_ERR("nrfx err init: %d\n", err);
    return err;
  }
}

void SPI_timer_event_handler(nrf_timer_event_t event_type,
                             void *p_context) // <1 us process time
{                                             // 6 ms
  unsigned int key = irq_lock();
  // 这里会短暂的暂停 < 1us
  // *(volatile uint32_t *)((uint8_t *)((&RHD_timer_nRFX)->p_reg +
  // (uint32_t)NRF_TIMER_TASK_STOP)) = 0x1UL; // 注意 使用这个会有bug，会导致
  // fatal error在一定时间后
  nrfx_timer_pause(
      &RHD_timer_nRFX); // 这里通过短暂的暂停 sampling 来保证sample 和 数据处理
                        // 不会发送 冲突；对于timer
                        // 的时间太短的时候，读取相关的值可能会导致读与写的冲突

  spi_buff_flag = !spi_buff_flag;
  bool completed_spi_buff = !spi_buff_flag;

  if (sampe_mode == SAMPLE_MODE_MODE0_LFP) {
    spi_overflow_flag = (((&spi)->p_reg->TXD.PTR -
                          (uint32_t)&mode_0_m_tx_buf[MODE0_SPI_CHUNK_WORDS]) /
                         2U);
    // tx buffer pointer
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_0_m_tx_buf[spi_overflow_flag];
    // rx double buffer pointer
    (&spi)->p_reg->RXD.PTR =
        (uint32_t)&mode_0_m_rx_buf[spi_buff_flag][spi_overflow_flag];
  } else if (sampe_mode == SAMPLE_MODE_MODE2_RAW_V2) {
    spi_overflow_flag = (((&spi)->p_reg->TXD.PTR -
                          (uint32_t)&mode_2_m_tx_buf[MODE_3_SPI_RX_BUF_SIZE]) /
                         2U);
    // tx buffer pointer
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_2_m_tx_buf[spi_overflow_flag];
    // rx double buffer pointer
    (&spi)->p_reg->RXD.PTR =
        (uint32_t)&mode_2_m_rx_buf[spi_buff_flag][spi_overflow_flag];
  } else if (sampe_mode == SAMPLE_MODE_MODE3_ESA_MUA) {
    spi_overflow_flag = (((&spi)->p_reg->TXD.PTR -
                          (uint32_t)&mode_3_m_tx_buf[MODE_3_SPI_RX_BUF_SIZE]) /
                         2U);
    // tx buffer pointer
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_3_m_tx_buf[spi_overflow_flag];
    // rx double buffer pointer
    (&spi)->p_reg->RXD.PTR =
        (uint32_t)&mode_3_m_rx_buf[spi_buff_flag][spi_overflow_flag];
  } else if (sampe_mode < SAMPLE_MODE_MODE3_ESA_MUA) {
    spi_overflow_flag = (((&spi)->p_reg->TXD.PTR -
                          (uint32_t)&spike_m_tx_buf[SPIKE_SPI_TX_BUF_SIZE]) /
                         2U); // mode 1下，溢出固定为14 ，也就是7个 sample
    // 	// tx buffer pointer
    (&spi)->p_reg->TXD.PTR = (uint32_t)&spike_m_tx_buf[spi_overflow_flag];
    // 	// rx double buffer pointer
    (&spi)->p_reg->RXD.PTR =
        (uint32_t)&spike_m_rx_buf[spi_buff_flag][spi_overflow_flag];
  }

  spi_ready_buff_flag = completed_spi_buff;
  spi_ready_overflow_flag = spi_overflow_flag;

  buffer_is_full = true;
  nrfx_timer_resume(&RHD_timer_nRFX);
  k_sem_give(&rhd_spi_ready_sem);
  irq_unlock(key);
}

uint32_t timer_init(uint32_t timer_period, uint32_t reset_ticks_value) {
  uint32_t timer_ticks;
  uint32_t timer_err;

  /* Recording timer */
  nrfx_timer_uninit(&RHD_timer_nRFX);
  nrfx_timer_config_t RHD_timer_config =
      NRFX_TIMER_DEFAULT_CONFIG(1000000); // 1 us units

  timer_err = nrfx_timer_init(&RHD_timer_nRFX, &RHD_timer_config, NULL);
  if (timer_err == NRFX_SUCCESS) {
    timer_ticks = nrfx_timer_us_to_ticks(&RHD_timer_nRFX, timer_period);
    // timer interrupt disable
    nrfx_timer_extended_compare(&RHD_timer_nRFX, NRF_TIMER_CC_CHANNEL0,
                                timer_ticks,
                                NRF_TIMER_SHORT_COMPARE0_CLEAR_MASK,
                                false); // 每一个定时器都有4-6个通道
    // nrfx_timer_enable(&RHD_timer_nRFX);
    nrfx_timer_disable(&RHD_timer_nRFX);
  } else {
    // LOG_ERR("timer initate failed! \n");
    // LOG_ERR("%d \n", timer_err);
    return 1;
  }

  /* Reset timer/counter */
  nrfx_timer_uninit(&SPI_timer_RESET);
  nrfx_timer_config_t SPI_timer_config = NRFX_TIMER_DEFAULT_CONFIG(1000000);

  SPI_timer_config.mode = NRF_TIMER_MODE_COUNTER;
  SPI_timer_config.interrupt_priority = 2;
  timer_err = nrfx_timer_init(&SPI_timer_RESET, &SPI_timer_config,
                              SPI_timer_event_handler);
  if (timer_err == NRFX_SUCCESS) {
    // prioity 2 to ensure no conflict between esb and spiRESET
    IRQ_CONNECT(
        NRFX_IRQ_NUMBER_GET(NRF_TIMER3), 2, nrfx_isr, nrfx_timer_3_irq_handler,
        0); // 注意在zephyr下，nrfx的中断都需要通过IRQ_connect与中断驱动连起来
            // timer interrupt disable
    nrfx_timer_extended_compare(&SPI_timer_RESET, NRF_TIMER_CC_CHANNEL0,
                                reset_ticks_value,
                                NRF_TIMER_SHORT_COMPARE0_CLEAR_MASK, true);
    // 初始化完成后，直接把timer暂停来等待上位机的指令
    nrfx_timer_disable(&SPI_timer_RESET);
  }
  return 0;
}

uint32_t ppi_init() {
  // 注意： 在v2.6 版本后，不能再直接去改变spi
  // 实例的参数来开启一个DMA+ppi的传输；使用 flag
  nrfx_spim_xfer_desc_t xfer_desc;
  spi_buff_flag = 0;
  if (mode_uses_mode0_raw_spi_source(sampe_mode)) {
    xfer_desc.p_tx_buffer = (uint8_t const *)mode_0_m_tx_buf;
    xfer_desc.tx_length = m_length;
    xfer_desc.p_rx_buffer = (uint8_t const *)mode_0_m_rx_buf[0];
    xfer_desc.rx_length = m_length;
  } else if (mode_uses_mode2_raw_spi_source(sampe_mode)) {
    xfer_desc.p_tx_buffer = (uint8_t const *)mode_2_m_tx_buf;
    xfer_desc.tx_length = m_length;
    xfer_desc.p_rx_buffer = (uint8_t const *)mode_2_m_rx_buf[0];
    xfer_desc.rx_length = m_length;
  } else if (mode_uses_shared_raw_spi_source(sampe_mode)) {
    xfer_desc.p_tx_buffer = (uint8_t const *)mode_3_m_tx_buf;
    xfer_desc.tx_length = m_length;
    xfer_desc.p_rx_buffer = (uint8_t const *)mode_3_m_rx_buf[0];
    xfer_desc.rx_length = m_length;
  } else if (mode_uses_20k_spike_spi_source(sampe_mode)) {
    // spike
    // nrfx_spim_xfer_desc_t xfer_desc = NRFX_SPIM_XFER_TRX(spike_m_tx_buf,
    // m_length, spike_m_rx_buf[0], m_length);
    xfer_desc.p_tx_buffer = (uint8_t const *)spike_m_tx_buf;
    xfer_desc.tx_length = m_length;
    xfer_desc.p_rx_buffer = (uint8_t const *)spike_m_rx_buf[0];
    xfer_desc.rx_length = m_length;
  }

  nrfx_spim_xfer(&spi, &xfer_desc,
                 NRFX_SPIM_FLAG_RX_POSTINC | NRFX_SPIM_FLAG_TX_POSTINC |
                     NRFX_SPIM_FLAG_NO_XFER_EVT_HANDLER |
                     NRFX_SPIM_FLAG_REPEATED_XFER | NRFX_SPIM_FLAG_HOLD_XFER);

  uint32_t timer_compare_event_addr = nrfx_timer_compare_event_address_get(
      &RHD_timer_nRFX, NRF_TIMER_CC_CHANNEL0);
  // get spim start event address
  uint32_t spi_start_task_addr = nrfx_spim_start_task_address_get(&spi);
  // get spim end event address
  uint32_t spi_end_event_addr = nrfx_spim_end_event_address_get(&spi);
  // get CS gpiote event address clr
  uint32_t gpiote_out_task_addr =
      nrfx_gpiote_out_task_address_get(&gpiote_instance, NRFX_SPIM_SS_PIN);
  // get counter task address
  uint32_t timer_counter_increment_addr =
      nrfx_timer_task_address_get(&SPI_timer_RESET, NRF_TIMER_TASK_COUNT);
  // // get counter task current value
  uint32_t timer_counter_capture_addr = nrfx_timer_capture_task_address_get(
      &SPI_timer_RESET, NRF_TIMER_CC_CHANNEL0);

  nrfx_gppi_channel_alloc(&gp_channel_1);
  nrfx_gppi_channel_alloc(&gp_channel_2);
  // nrfx_gppi_channel_alloc(&gp_channel_3);

  nrfx_gppi_channel_endpoints_setup(gp_channel_1, timer_compare_event_addr,
                                    gpiote_out_task_addr);
  nrfx_gppi_fork_endpoint_setup(gp_channel_1, spi_start_task_addr);
  nrfx_gppi_channel_endpoints_setup(gp_channel_2, spi_end_event_addr,
                                    gpiote_out_task_addr);
  nrfx_gppi_fork_endpoint_setup(gp_channel_2, timer_counter_increment_addr);
  // nrfx_gppi_channel_endpoints_setup(gp_channel_3, spi_end_event_addr,
  // timer_counter_capture_addr);

  nrfx_gppi_channels_enable(BIT(gp_channel_1));
  nrfx_gppi_channels_enable(BIT(gp_channel_2));
  // nrfx_gppi_channels_enable(BIT(gp_channel_3));

  return 0;
}

// RHD
u16_t Writecommand_generator(u8_t regnum, u8_t data) {
  // write command : have been swap the u16_t
  u16_t mask = data << 8;
  mask = 0b0000000010000000 | mask | regnum;
  return mask;
}

u16_t Readcommand_generator(u8_t regnum) {
  // Read command :  have been swap the u16_t
  u16_t mask = 0b0000000011000000;
  mask = regnum | mask;
  return mask;
}

u16_t Convertcommand_generator(u8_t channel, u8_t reset) {
  // channel 0-15; reset: 0/1
  // convert command :  have been swap the u16_t
  // TODO
  /*If the LSB (bit H) [reset] of a CONVERT(C) command is set to 1 when DSP
offset removal is enabled (see “DSP High-Pass Filter for Offset Removal”
section), then the output of the digital high-pass filter associated with
amplifier channel C is reset to zero. This can be used to rapidly recover from a
large transient and settle to baseline.*/

  u16_t mask = reset << 8;
  mask = mask | channel;
  return mask;
}

u16_t spi_trans(u16_t command) // SPI trans in the timer period ,param is RHD
                               // command ,return is SPI recieve data
{
  init_tx_buf[0] = command;
  nrfx_spim_xfer_desc_t xfer_desc =
      NRFX_SPIM_XFER_TRX(init_tx_buf, m_length, init_rx_buf, m_length);
  // nrfx_gpiote_out_toggle(&gpiote_instance, NRFX_SPIM_SS_PIN);
  u32_t ret_nRF = nrfx_spim_xfer(&spi_init, &xfer_desc, 0);
  // nrfx_gpiote_out_toggle(&gpiote_instance, NRFX_SPIM_SS_PIN);
  if (ret_nRF == NRFX_SUCCESS) {
    return init_rx_buf[0];
  } else {
    // LOG_ERR("spi_init error");
    return -1;
  }
}

u16_t Calibrate() {
  // Sends a CALIBRATE command to the RHD chip through the SPI interface
  // Initiates an ADC self-calibration routine that should be performed after
  // chip power-up and register configuration Takes several clock cycles to
  // execute, and requires 9 clock cycles of dummy commands that will be ignored
  // until the calibration is complete
  u16_t calibrate_r;
  calibrate_r = spi_trans(CALIBRATE);
  return calibrate_r;
}

int RHD_init(const u16_t *Register_config) {
  u16_t result[18 + 1 + 9] = {0}; // init results
  u16_t err = 0;

  /*
  To settle the amplifiers, the amp fast settle bit
  in Register 0 should be set high momentarily and then
  returned to zero. It is recommended (though not required)
  to hold amp fast settle high momentarily after powering up
  the chip if low values of fL (< 1 Hz) are used. The
  recommended duration of a fast settle pulse is 2.5/fH;
  */

  // do some dummy commands to ensure the RHD chip is in the proper state
  for (int j = 0; j < 2; j++) {
    err = spi_trans(NINE_DUMMPY[j]);
  }

  for (int i = 0; i < 18; i++) {
    result[i] = spi_trans(*(Register_config + i));
  }
  k_sleep(K_USEC(500)); // 100us delay before ADC calibrate
  result[18] = Calibrate();
  for (int j = 0; j < 9; j++) { // using generate nine SCLK to calibrate ADC
    result[19 + j] = spi_trans(NINE_DUMMPY[j]);
  }

  // check
  for (int u = 0; u < 18 + 1 + 9; u++) {
    // register setup check
    if (u < 18) // register return check
    {
      if (result[u + 2] != (0x00ff | *(Register_config + u))) {
        err = result[u + 2] + 0x1000;
        return err;
      }
    } else if (u == 18) // calibrate check
    {
      /*
      During the entire calibration cycle, the results returned by the RHD2000
      consist of all zeros except for the MSB. The MSB will be zero if two’s
      complement mode is enabled (see Register 4 description below); otherwise
      it will be one.
      */
      if (result[u + 2] != 0x0080) // "unsigned" mode;
      {                            // error
        err = result[u + 2] + 0x1000;
        return err;
      }
    }
  }
  err = 0;
  return err;
}

void spi_transmit_buffer_reset() {
  unsigned int key = irq_lock();

  spi_buff_flag = 0;
  spi_ready_buff_flag = 0;
  spi_ready_overflow_flag = 0;
  spi_overflow_flag = 0;
  buffer_is_full = false;
  k_sem_reset(&rhd_spi_ready_sem);
  if (mode_uses_mode0_raw_spi_source(sampe_mode)) {
    (&spi)->p_reg->RXD.PTR = (uint32_t)&mode_0_m_rx_buf[0];
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_0_m_tx_buf;
  } else if (mode_uses_mode2_raw_spi_source(sampe_mode)) {
    (&spi)->p_reg->RXD.PTR = (uint32_t)&mode_2_m_rx_buf[0];
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_2_m_tx_buf;
  } else if (mode_uses_shared_raw_spi_source(sampe_mode)) {
    (&spi)->p_reg->RXD.PTR = (uint32_t)&mode_3_m_rx_buf[0];
    (&spi)->p_reg->TXD.PTR = (uint32_t)&mode_3_m_tx_buf;
  } else if (mode_uses_20k_spike_spi_source(sampe_mode)) {
    (&spi)->p_reg->RXD.PTR = (uint32_t)&spike_m_rx_buf[0];
    (&spi)->p_reg->TXD.PTR = (uint32_t)&spike_m_tx_buf;
  }

  irq_unlock(key);
}

void timer_stop() {
  nrfx_timer_disable(&RHD_timer_nRFX);
  nrfx_timer_disable(&SPI_timer_RESET);
  spi_transmit_buffer_reset();
}

void timer_start() {
  spi_transmit_buffer_reset();
  nrfx_timer_enable(&SPI_timer_RESET);
  nrfx_timer_enable(&RHD_timer_nRFX); // resume the timer to sample
}

void RHD_tx_buf_setup() {
  // comm : 0/1
  u16_t convert_block[NUM_CHANNELS];
  // recording: convert
  // cconvert channels
  for (int j = 0; j < NUM_CHANNELS; j++) {
    convert_block[j] = Convertcommand_generator(j + 8, 0);
  }

  // for test
  if (TEST_CHANNEL < NUM_CHANNELS) {
    convert_block[TEST_CHANNEL] = Readcommand_generator(62);
  }

  /* load the convert commands to tx_buf */
  if (mode_uses_mode0_raw_spi_source(sampe_mode)) {
    for (int i = 0; i < LFP_TX_BUFFER_SIZE; i += NUM_CHANNELS) {
      for (int j = 0; j < NUM_CHANNELS; j++) {
        mode_0_m_tx_buf[i + j] = convert_block[j];
      }
    }
  } else if (mode_uses_mode2_raw_spi_source(sampe_mode)) {
    for (int i = 0; i < MODE_3_TX_BUFFER_SIZE; i += NUM_CHANNELS) {
      for (int j = 0; j < NUM_CHANNELS; j++) {
        mode_2_m_tx_buf[i + j] = convert_block[j];
      }
    }
  } else if (mode_uses_shared_raw_spi_source(sampe_mode)) {
    // Mode3 keeps the legacy raw conversion buffer path.
    for (int i = 0; i < MODE_3_TX_BUFFER_SIZE; i += NUM_CHANNELS) {
      for (int j = 0; j < NUM_CHANNELS; j++) {
        mode_3_m_tx_buf[i + j] = convert_block[j];
      }
    }
  } else if (sampe_mode == 1) {
    // spike 20khz
    for (int i = 0; i < SPIKE_TX_BUFFER_SIZE; i += SPIKE_CONVERT_FASHION_NUM) {
      for (int j = 0; j < SPIKE_CONVERT_FASHION_NUM; j++) {
        spike_m_tx_buf[i + j] = convert_block[j];
      }
    }
  }
}
