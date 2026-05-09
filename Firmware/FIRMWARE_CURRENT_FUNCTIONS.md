# Firmware Current Functions And Implementation Logic

本文档记录当前工作区中的 firmware 功能、主流程、各采样模式的数据路径，以及可以通过 GUI 或编译期常量调控的参数。

当前代码的主要入口和模块：

- `src/main.c`: 系统初始化、主循环、采样模式切换、功耗保护、阻抗检测、mode0/mode2/mode3 主处理逻辑。
- `RHD_Recording/RHDRecording.c`: RHD SPI、timer、PPI/GPIOTE、SPI double buffer 和 overflow 拉回逻辑。
- `RHD_Recording/RHDRecording.h`: 采样窗口、SPI chunk、各模式 buffer 大小和 RHD register 配置声明。
- `RHD_Recording/OnlineFilter.c/.h`: LFP、ESA、MUA、MAND 处理和 ADC counts/uV 转换。
- `ESB_wireless/ESB_wireless.c/.h`: ESB 初始化、命令解析、packet 打包、mode0/mode2 量化打包。
- `Sensor_recording/*`: 电量计、IMU、充电相关驱动。

## Overall System

Firmware 运行在 nRF 平台上，主要负责：

- 通过 RHD2132 SPI 前端采集 16 通道神经信号。
- 按不同模式处理并压缩 LFP、ESA、MAND、MUA、raw data。
- 通过 ESB 无线链路发送神经数据、传感器、电池、阻抗结果和状态信息。
- 接收 GUI/HABITS 下发的采样控制、模式切换、ESB 参数、量化参数、阻抗测试和远程重启命令。
- 通过电池保护、watchdog、低功耗等待和自动恢复逻辑提高长期运行稳定性。

默认状态：

- `sampe_mode = 0`: 默认进入 mode0。
- `sample_switch = false`: 上电后默认不直接采样，需要 GUI 下发开始采样。
- `recorded_spike_channel = 0`: 默认 selected raw/spike channel 为通道 0。
- `raw_channel[16] = 0..15`: 默认记录 16 通道。
- `channel_order = {14, 15, 0, 1, ..., 13}`: 补偿 RHD2132 convert command 的 2-step conversion latency。

## Main Loop

主循环在 `src/main.c` 中完成以下事情：

1. 处理远程重启、runtime RF channel 切换、runtime ESB mode config 更新。
2. 若当前未采样：
   - 停止 timer，进入低功耗；
   - 若有阻抗测试请求，则执行阻抗测试；
   - 若有 mode switch 请求，则重新进入采样启动流程；
   - 定期刷新电池状态，并发送 empty payload。
3. 若收到开始采样但尚未进入采样态：
   - 检查低电保护是否允许开始；
   - 初始化 RHD SPI 和 RHD register；
   - 初始化 filter；
   - 初始化 IMU 模式；
   - 填充 RHD convert command TX buffer；
   - 做 timestamp handshake；
   - 应用当前 mode 的 ESB 发送参数；
   - 启动 timer 并等待第一包 SPI buffer ready。
4. 若 SPI buffer ready：
   - 调用 `structure_rx_data()` 将 RX buffer 整理成 mode-specific source buffer；
   - 读取 IMU 和电池数据；
   - 执行对应 mode 的信号处理；
   - 调用 ESB packet wrap 函数发送数据。

mode0 额外使用 `k_sem_take(&rhd_spi_ready_sem, K_FOREVER)` 在两段 SPI chunk 之间和包发送之后进入等待，以降低 active time。

## SPI And Buffer Flow

RHD 采样使用 timer + PPI/GPIOTE + SPIM：

- RHD timer compare 触发 CS 和 SPIM START。
- SPIM 使用 repeated transfer，TX/RX buffer 通过 EasyDMA 自增。
- SPI reset timer 在达到当前模式设定的 word 数后触发 `SPI_timer_event_handler()`。
- ISR 中暂停 RHD timer、切换 double buffer、计算 overflow words、把下一个 TX/RX pointer 设置到对应偏移，然后释放 `rhd_spi_ready_sem`。

当前 ISR 的 overflow 检测逻辑以当前 TXD pointer 和该模式 expected chunk end 的差值计算：

- mode0: `mode_0_m_tx_buf[MODE0_SPI_CHUNK_WORDS]`
- mode2: `mode_2_m_tx_buf[MODE_3_SPI_RX_BUF_SIZE]`
- mode3: `mode_3_m_tx_buf[MODE_3_SPI_RX_BUF_SIZE]`
- mode1: `spike_m_tx_buf[SPIKE_SPI_TX_BUF_SIZE]`

`structure_rx_data()` 会读取 ISR 记录的 completed buffer 和 overflow words。如果发生 overflow，会把 completed buffer 中超过 expected chunk end 的尾部 word 复制到下一个 buffer 起点，避免已经进入下一周期的 word 丢失。

## Mode Summary

| Mode | `sampe_mode` | Main purpose | RHD sample interval | SPI chunk | Main packet |
| --- | ---: | --- | --- | --- | --- |
| mode0 | 0 | LFP + MAND + selected raw | `timer_period = 5`, 12.5 kHz setup | 25 samples x 16 ch | `0x0100` variable-length quantized packet |
| mode1 | 1 | legacy spike raw + MUA | 20 kHz path | 90 samples x 16 ch | `0x0200 | channel` |
| mode2 | 2 | 16-channel 8-bit raw v2 | `timer_period = 6`, about 10.417 kHz | 25 samples x 16 ch | `0x0500 | 0x88` |
| mode3 | 3 | LFP + ESA + MUA + selected raw | `timer_period = 5`, 12.5 kHz setup | 25 samples x 16 ch | `0x0700 | reref_mode`, raw packet `0x0800 | channel` |

## Mode0

mode0 是当前最重要的低功耗混合数据模式。

采样层和包层要分开理解：

- `MODE0_SPI_CHUNK_SAMPLES = 25`: SPI/DMA 每次切换 double buffer 的采样点数，约 2 ms。
- `SAMPLE_POINT_NUM = 50`: mode0 最终处理和打包窗口，约 4 ms。
- `MODE0_PACKET_CHUNKS = 2`: 两个 25-sample SPI chunk 合成一个 50-sample mode0 packet。
- `SPI_TX_BUF_SIZE = MODE0_SPI_CHUNK_WORDS`
- `SPI_RX_BUF_SIZE = MODE0_SPI_CHUNK_WORDS`

这意味着 mode0 的 SPI/DMA buffer 必须按 25-sample chunk 维度计算，而不是按 50-sample final packet 维度计算。这个点和之前 mode0 单点尖峰问题直接相关，详细分析见 `MODE0_SPI_BUFFER_SPIKE_SUMMARY.md`。

mode0 数据处理路径：

1. `structure_rx_data()` 从 `mode_0_m_rx_buf[completed_spi_buff]` 读取 25-sample chunk。
2. 每个 word 经 `swapShort16()`，再减去 32768 得到 centered counts。
3. centered counts 写入 `mode_0_input_counts_buffer[ch][sample]`，同时转换为 uV 写入 `mode_0_input_buffer[ch][sample]`。
4. 两个 chunk 收满 50 samples 后：
   - `process_lfp_lowpass_decimate()` 生成 16 通道 LFP，每通道 4 点，目标 1 kHz；
   - `process_mode0_mand_chunk()` 生成 16 通道 MAND，每包每通道 1 点；
   - selected raw 使用 `recorded_spike_channel` 指定通道的 50 点 raw；
   - `mode0_tx_payload_wrap_quantized()` 打包并发送。

mode0 neural layout 顺序固定为：

1. 16 通道 LFP，`16 * 4 = 64` values。
2. 16 通道 MAND，`16` values。
3. selected raw channel，`50` values。

mode0 量化规则：

- bit width 由 GUI/runtime command 控制，范围 8 到 12 bit，当前默认 `12`。
- LFP 使用 signed quantization，full-scale 由 GUI 控制，范围 `±500 uV` 到 `±10000 uV`，步进 `500 uV`，默认 `±1000 uV`。
- raw data 使用 signed quantization，但 full-scale 固定为 `±500 uV`，不受 GUI full-scale 控制。
- MAND 使用 unsigned quantization，范围固定为 `0..1000`。

mode0 packet:

- header: `0x0100 | flags`
- flags low nibble: selected raw channel。
- `0x10`: SPI overflow flag。
- `0x20`: packet includes accel。
- `0x40`: packet includes status。
- timestamp: 32-bit `timestamp_LTNSRS`，拆成两个 `u16_t`。
- packed neural data: LFP + MAND + raw。
- optional accel: 3 words，mode0 只发送 accel-only。
- optional status: battery/status 3 words + compact power telemetry 2 words。
- final word: `tx_payload_wraped_num` packet counter。

mode0 status 和 power telemetry：

- battery/status 约 1 Hz 更新。
- accel 约 100 Hz 更新，仅在 IMU accel 或 accel+gyro 模式下启用，但 mode0 packet 中只放 3-axis accel。
- compact power telemetry 两个 word：
  - word0 包含 telemetry sequence、ESB TX power、retransmit count 等 flags。
  - word1 高字节为 sleep ratio q8，低字节为 retransmit rate q8。

## Mode1

mode1 是 legacy spike 模式：

- 使用 `SPIKE_SAMPLE_POINT_NUM = 90`。
- `SPIKE_SPI_TX_BUF_SIZE = 16 * 90`。
- `MUA_BIN_SIZE = 18`。
- RHD register 使用 `Register_config_spike`。
- raw data 只发送 `recorded_spike_channel` 这一通道。
- MUA 对所有通道做 threshold detection，结果压缩为 raster shorts。

mode1 packet:

- header: `0x0200 | selected_channel`
- timestamp: 32-bit。
- flag word: sensor update flag + overflow signal。
- IMU: 6 words。
- battery: 3 words。
- selected raw: 90 words。
- MUA raster: 5 words。
- final word: packet counter。

## Mode2

mode2 当前使用 v2 raw path：

- RHD register 使用 `Register_config_mode3`。
- `timer_period = 6`，采样率约 10.417 kHz。
- SPI chunk 使用 `CHUNK_SIZE = 25`，16 通道。
- IMU 在 mode2 中关闭。
- firmware 把 16 通道 raw uV 量化为 int8。
- 每包发送 `MODE2_V2_PACKET_SAMPLES = 15` 个 sample，每个 sample 含 16 通道。
- raw full-scale 固定为 `±500 uV`。
- bit width 固定为 8 bit。

mode2 packet:

- header: `0x0500 | 0x88`
- timestamp: 32-bit。
- packet counter。
- payload: `15 * 16 = 240` bytes int8 raw data。
- packet length: 248 bytes。

## Mode3

mode3 是 ESA/MUA 常开记录模式：

- RHD register 使用 `Register_config_mode3`。
- `timer_period = 5`，12.5 kHz。
- SPI chunk 为 25 samples x 16 channels。
- `process_neural_signals_mode3()` 同时生成：
  - LFP: 每通道 2 点，1 kHz。
  - ESA: 高通、整流、低通后，每通道 2 点，1 kHz。
  - MUA raster。
- selected raw channel 使用 `recorded_spike_channel`。
- mode3 raw data 不每个 chunk 立即发，而是累积 `MODE3_RAW_BUFFER_CHUNKS = 4` 个 chunk 后，合并为 100-sample raw packet。

mode3 packet:

- main packet header: `0x0700 | mode3_esa_reref_enable`
- timestamp: 32-bit。
- flag word: sensor update flag + overflow signal。
- IMU: 6 words。
- battery: 3 words。
- LFP: `16 * 2` words。
- ESA: `16 * 2` words。
- MUA raster: 3 words。
- final word: packet counter。

mode3 raw packet:

- header: `0x0800 | selected_channel`
- timestamp: 32-bit。
- overflow flag。
- selected raw: `25 * 4 = 100` words。
- final word: packet counter。

从 mode3 切到其他模式时，如果 raw buffer 中还有未发送 chunk，firmware 会延迟 mode switch，等 4-chunk raw packet 发完后再切换。

## Online Filters

核心采样和滤波参数：

- `ORIGINAL_FS = 12500`
- `TARGET_FS = 1000`
- resample ratio: `2 / 25`
- LFP lowpass: 2nd-order IIR, cutoff `300 Hz`
- ESA highpass: 1st-order IIR, cutoff `250 Hz`
- ESA lowpass: 1st-order IIR, cutoff `150 Hz`
- spike/MUA minimum ISI: `MIN_ISI = 10` samples
- mode3 MUA bin size: `MUA_BIN_SIZE_MODE_3 = 8`
- default spike threshold: `60 uV`

ADC conversion：

- RHD2132 reference: `1.225 V`
- gain: `192`
- `RHD2132_ADC_UV_PER_COUNT` 在 `OnlineFilter.h` 中计算。
- raw ADC word 先中心化到 `sample - 32768`，再乘以 `scale_factor` 转为 uV。

## Impedance Test

阻抗检测通过 command `0x0900` 触发，只在采样停止后执行。

当前实现是 two-phase：

1. phase 1: 所有通道用 1 kHz zcheck 测量标准电极阻抗。
2. phase 2: 对 1 kHz 下超过 `2.5 MOhm` 的疑似高阻通道，用 100 Hz 再测一次判断是否 open。

关键参数：

- `IMPEDANCE_TEST_CHANNEL_COUNT = 16`
- `IMPEDANCE_TEST_WAVE_POINTS = 20`
- `IMPEDANCE_TEST_SETTLE_CYCLES = 5`
- `IMPEDANCE_TEST_MEASURE_CYCLES = 8`
- primary frequency: `1000 Hz`
- open-check frequency: `100 Hz`
- suspect threshold: `2.5 MOhm`
- open confirm threshold: `15 MOhm`
- open marker: `0xFFFFFFFF`

firmware 会自动选择 zcheck capacitor scale，使测试峰值落在 ADC full-scale 的 5% 到 70% 区间。

阻抗 packet：

- progress: `0x0900 | stage`
- result: `0x0A00 | channel_count`

## ESB And Runtime RF

ESB 默认：

- bitrate: `1 Mbps`
- RF channel list: `{2, 17, 33, 50, 67, 84}`
- default active RF channel: `84`
- TX FIFO 和 retry 由 ESB 驱动配置。

每个 mode 可以单独配置：

- TX power code。
- retransmit count。
- retransmit delay。
- noack percentage。

当前默认 per-mode ESB 参数：

| Mode | TX power code | Retransmit count | Retransmit delay | Noack percent |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0 dBm | 1 | 1200 us | 0 |
| 1 | 0 dBm | 3 | 1200 us | 0 |
| 2 | 0 dBm | 1 | 1200 us | 0 |
| 3 | 0 dBm | 3 | 1200 us | 0 |

`noack_percent` 是按包的累计比例控制，0 表示全部 ACK，100 表示全部 noack。

## Battery Guard And Watchdog

电池保护当前参数：

- low voltage threshold: `3250 mV`
- recover voltage threshold: `3300 mV`
- low voltage hold time: `100 ms`
- recover stable time: `2000 ms`
- battery communication failure limit: `3`

watchdog：

- `WATCHDOG_TIMEOUT_MS = 5000`
- 主循环中周期性 feed watchdog。
- 初始化失败、恢复失败、远程重启请求等情况下会进入受控 reboot 流程。

mode0 会额外统计 sleep/active 时间，用 compact power telemetry 上报低功耗效果和 ESB retransmit 情况。

## Runtime Commands

| Command | Payload | Effect | Notes |
| --- | --- | --- | --- |
| `0x0001` | none | begin sampling | `sample_switch = true` |
| `0x0002` | none | stop sampling | `sample_switch = false` |
| `0x0003` | `data[1]` | IMU mode switch | `1` off, `2` accel, `3` accel+gyro；mode0 只发送 accel；mode2 关闭 IMU |
| `0x0200` | `data[1..16]` | raw channel list | 有效通道值 `<16` |
| `0x0300` | `data[1]` | sample mode switch | mode 0..3；从 mode3 切出时可能等待 raw buffer flush |
| `0x0400` | `data[1]`, `data[2]` | selected raw/spike channel and threshold | `data[1] < 16`；会重置 mode0 processing；threshold 按 ADC count/uV 转换 |
| `0x0500` | `data[1..4]` | legacy mode2 4-channel selection | 当前 mode2 v2 发送 16 通道 8-bit raw，此命令主要保留兼容 |
| `0x0600` | `data[1]` | mode3 ESA reref mode | `0` off, `1` fast median, `2` safe median；也会触发 mode0 processing reset |
| `0x0900` | none | start impedance test | 停止采样后执行 |
| `0x0B00` | `data[1]` | runtime RF channel switch | 只接受 RF channel list 中的值 |
| `0x0C00` | `data[1] = 0xA55A` | remote reboot | 设置 reboot pending 并停止采样 |
| `0x0D00` | mode/config words | per-mode ESB config | TX power, retransmit count, retransmit delay, noack percent |
| `0x0F00` | bit depth, LFP full-scale | mode0 quant config | bit 8..12；LFP full-scale 500..10000 uV，500 uV step；mode0 raw 固定 ±500 uV |

## Adjustable Parameters

### Runtime Adjustable From GUI

这些参数可以通过 GUI/command 调整，不需要重新编译 firmware：

| Parameter | Current range/default | Affects |
| --- | --- | --- |
| sampling start/stop | start or stop | 是否采样 |
| sample mode | 0, 1, 2, 3 | mode0/mode1/mode2/mode3 |
| IMU mode | off, accel, accel+gyro | mode0 只发 accel，mode2 关闭 IMU |
| selected raw channel | 0..15, default 0 | mode0 raw, mode1 raw, mode3 raw |
| spike threshold | command value converted by ADC scale | mode1/mode3 MUA detection |
| mode3 ESA reref | 0, 1, 2 | mode3 ESA 和 mode0 MAND/reref related processing |
| RF channel | 2, 17, 33, 50, 67, 84 | ESB RF channel |
| ESB TX power | code 0..7 | per-mode TX power |
| ESB retransmit count | low 4 bits | per-mode retransmit count |
| ESB retransmit delay | 450..4000 us | per-mode ACK retry window |
| ESB noack percent | 0..100 | per-mode noack ratio |
| mode0 quant bit width | 8..12, default 12 | LFP, MAND, raw all use this bit width |
| mode0 LFP full-scale | 500..10000 uV, 500 uV step, default 1000 uV | only mode0 LFP signed quantization |

Important fixed runtime behavior：

- mode0 raw full-scale is fixed at `±500 uV` by `MODE0_RAW_QUANT_FULL_SCALE_UV`。
- mode2 raw full-scale is fixed at `±500 uV` by `MODE2_V2_SCALE_UV`。
- mode0 bit width still affects LFP, MAND, and raw together。
- mode0 LFP full-scale does not affect raw full-scale。

### Compile-Time Parameters

这些参数在 firmware 中以 `#define` 或静态变量形式存在，修改后需要重新编译和同步 GUI/解析逻辑。

| Parameter | Current value | File | Notes |
| --- | --- | --- | --- |
| `Channel_recorded` | 16 | `src/main.c` | RHD 通道数 |
| `SAMPLE_POINT_NUM` | 50 | `RHD_Recording/RHDRecording.h` | mode0 final packet window |
| `MODE0_SPI_CHUNK_SAMPLES` | 25 | `RHD_Recording/RHDRecording.h` | mode0 SPI/DMA chunk window |
| `MODE0_SPI_CHUNK_WORDS` | `16 * 25 = 400` | `RHD_Recording/RHDRecording.h` | mode0 expected SPI chunk words |
| `SPI_TX_BUF_SIZE` | `MODE0_SPI_CHUNK_WORDS` | `RHD_Recording/RHDRecording.h` | mode0 TX DMA size basis |
| `SPI_RX_BUF_SIZE` | `MODE0_SPI_CHUNK_WORDS` | `RHD_Recording/RHDRecording.h` | mode0 RX DMA size basis |
| `LFP_TX_BUFFER_SIZE` | `SPI_TX_BUF_SIZE * 2` | `RHD_Recording/RHDRecording.h` | mode0 TX overflow reserve |
| `LFP_RX_BUFFER_SIZE` | `SPI_RX_BUF_SIZE * 2` | `RHD_Recording/RHDRecording.h` | mode0 RX overflow reserve |
| `CHUNK_SIZE` | `MODE0_SPI_CHUNK_SAMPLES` | `RHD_Recording/RHDRecording.h` | shared by mode2/mode3 chunk |
| `MODE3_RAW_BUFFER_CHUNKS` | 4 | `src/main.c` | mode3 selected raw 合并 4 个 chunk 发送 |
| `SPIKE_SAMPLE_POINT_NUM` | 90 | `RHD_Recording/RHDRecording.h` | mode1 raw window |
| `MUA_BIN_SIZE` | 18 | `RHD_Recording/RHDRecording.h` | mode1 MUA bin |
| `MODE2_V2_PACKET_SAMPLES` | 15 | `ESB_wireless/ESB_wireless.h` | mode2 每包 sample 数 |
| `MODE2_V2_SCALE_UV` | 500.0 | `ESB_wireless/ESB_wireless.h` | mode2 raw ±500 uV |
| `MODE0_RAW_QUANT_FULL_SCALE_UV` | 500 | `ESB_wireless/ESB_wireless.h` | mode0 raw ±500 uV |
| `MODE0_QUANT_BITS_DEFAULT` | 12 | `ESB_wireless/ESB_wireless.h` | mode0 默认 bit width |
| `MODE0_QUANT_FULL_SCALE_UV_DEFAULT` | 1000 | `ESB_wireless/ESB_wireless.h` | mode0 LFP 默认 full-scale |
| `ORIGINAL_FS` | 12500 | `RHD_Recording/OnlineFilter.h` | RHD 原始采样率 |
| `TARGET_FS` | 1000 | `RHD_Recording/OnlineFilter.h` | LFP/ESA 输出目标采样率 |
| `IIR_CUTOFF_lowpass_LFP` | 300 Hz | `RHD_Recording/OnlineFilter.h` | LFP lowpass |
| `IIR_CUTOFF_highpass_ESA` | 250 Hz | `RHD_Recording/OnlineFilter.h` | ESA highpass |
| `IIR_CUTOFF_lowpass_ESA` | 150 Hz | `RHD_Recording/OnlineFilter.h` | ESA lowpass |
| `MODE0_MAND_DEFAULT_LAG_SAMPLES` | 7 | `RHD_Recording/OnlineFilter.h` | mode0 MAND delay |
| `MODE0_MAND_STEP_MS` | 4 | `RHD_Recording/OnlineFilter.h` | mode0 MAND bin step |
| `BATTERY_LOW_VOLTAGE_MV` | 3250 | `src/main.c` | low battery hold threshold |
| `BATTERY_RECOVER_VOLTAGE_MV` | 3300 | `src/main.c` | recovery threshold |
| `WATCHDOG_TIMEOUT_MS` | 5000 | `src/main.c` | watchdog timeout |
| `ESB_RETRANSMIT_DELAY_MIN_US` | 450 | `src/main.c` | runtime ESB delay clamp min |
| `ESB_RETRANSMIT_DELAY_MAX_US` | 4000 | `src/main.c` | runtime ESB delay clamp max |

Do not casually change mode0 SPI buffer sizing：

- `SAMPLE_POINT_NUM = 50` 是 mode0 processing/packet window。
- `MODE0_SPI_CHUNK_SAMPLES = 25` 是 mode0 SPI/DMA switch window。
- `SPI_TX_BUF_SIZE` 和 `SPI_RX_BUF_SIZE` 应继续使用 `MODE0_SPI_CHUNK_WORDS`。
- 如果把 mode0 DMA buffer 重新按 `SAMPLE_POINT_NUM` 放大，可能重新引入之前的单点尖峰或 RAM layout/pressure 问题。

## Packet Type Summary

| Header | Source | Meaning |
| --- | --- | --- |
| `0x0100 | flags` | mode0 | quantized LFP + MAND + selected raw, optional accel/status |
| `0x0200 | channel` | mode1 | selected raw + MUA raster |
| `0x0300` | timestamp payload | sampling start alignment |
| `0x0400` | empty payload | idle/status packet |
| `0x0500 | 0x88` | mode2 v2 | 16-channel int8 raw |
| `0x0600` | sensor packet | legacy sensor packet |
| `0x0700 | reref_mode` | mode3 | LFP + ESA + MUA |
| `0x0800 | channel` | mode3 | selected raw buffered packet |
| `0x0900 | stage` | impedance | progress |
| `0x0A00 | channel_count` | impedance | result |

## Maintenance Notes

- mode0 的 neural packed layout 顺序当前是 LFP、MAND、selected raw。GUI 解包和 EDF 保存必须保持同一顺序。
- mode0 packet 是 variable length：bit width 8..12 会改变 neural packed word 数，accel/status flag 也会改变包长。
- mode0 raw full-scale 和 mode2 raw full-scale 当前都固定为 `±500 uV`，GUI 侧不要再把 mode0 LFP full-scale 套用到 raw。
- `0x0F00` 只改变 mode0 bit width 和 LFP full-scale。
- 修改 mode0 bit width 或 LFP full-scale 时，GUI、EDF metadata、parser 和 tests 需要保持一致。
- 修改 mode2 packet samples、mode2 bit depth 或 full-scale 时，GUI mode2 parser 和 EDF 保存也需要同步。
- 修改 mode3 raw buffer 合并数量时，需要同步上位机对 `0x0800` raw packet sample count 的解析。
- 阻抗测试期间大部分采样命令会被忽略，只允许阻抗、RF/ESB 配置、mode0 quant config 和 reboot 等安全命令通过。
- 当前 firmware 中仍保留一些 legacy mode2/mode1 命令和 wrapper，改动前需要确认 GUI 是否仍有兼容路径依赖。
