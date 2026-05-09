# Mode0 Single-Point Spike Resolution Summary

## Background

Mode0 previously showed random single-point spikes in raw data. The spikes:

- appeared only in mode0;
- were present in saved EDF files, so they were not a plotting artifact;
- could appear in different channels across runs;
- were single-sample events rather than continuous waveform distortion;
- could exceed the synthetic/input signal amplitude range, so they were not simply valid samples from another channel.

The issue disappeared after changing mode0 SPI TX/RX buffer sizing to use the 25-sample SPI/DMA chunk size instead of the 50-sample mode0 packet size.

## Important Correction

The fix was initially described as changing `MODE0_SPI_CHUNK_SAMPLES` to 25. That is not strictly accurate.

Before the fix, `MODE0_SPI_CHUNK_SAMPLES` already expanded to 25:

```c
#define CHUNK_SIZE 25
#define MODE0_SPI_CHUNK_SAMPLES CHUNK_SIZE
```

The actual effective change was the SPI buffer size basis.

Before:

```c
#define SAMPLE_POINT_NUM 50
#define SPI_TX_BUF_SIZE (CONVERT_FASHION_NUM * SAMPLE_POINT_NUM * time_window)
#define SPI_RX_BUF_SIZE (CONVERT_FASHION_NUM * SAMPLE_POINT_NUM * time_window)
#define LFP_TX_BUFFER_SIZE  SPI_TX_BUF_SIZE * 2
#define LFP_RX_BUFFER_SIZE  SPI_RX_BUF_SIZE * 2
```

With 16 channels and `time_window = 1`, this meant:

```text
SPI_TX_BUF_SIZE = 16 * 50 = 800 words
LFP_TX_BUFFER_SIZE = 800 * 2 = 1600 words

SPI_RX_BUF_SIZE = 16 * 50 = 800 words
LFP_RX_BUFFER_SIZE = 800 * 2 = 1600 words
mode_0_m_rx_buf[2][1600]
```

After:

```c
#define SAMPLE_POINT_NUM 50
#define MODE0_SPI_CHUNK_SAMPLES 25
#define MODE0_SPI_CHUNK_WORDS (NUM_CHANNELS * MODE0_SPI_CHUNK_SAMPLES)
#define MODE0_PACKET_CHUNKS (SAMPLE_POINT_NUM / MODE0_SPI_CHUNK_SAMPLES)
#define SPI_TX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
#define SPI_RX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
#define LFP_TX_BUFFER_SIZE  SPI_TX_BUF_SIZE * 2
#define LFP_RX_BUFFER_SIZE  SPI_RX_BUF_SIZE * 2
```

This means:

```text
SPI_TX_BUF_SIZE = 16 * 25 = 400 words
LFP_TX_BUFFER_SIZE = 400 * 2 = 800 words

SPI_RX_BUF_SIZE = 16 * 25 = 400 words
LFP_RX_BUFFER_SIZE = 400 * 2 = 800 words
mode_0_m_rx_buf[2][800]
```

So the working fix reduced mode0 SPI TX/RX buffer allocation by about 4800 bytes total.

## Correct Mode0 Layering

Mode0 has two different sizes that must not be conceptually mixed:

```c
SAMPLE_POINT_NUM = 50
```

This is the final mode0 processing/packet window.

```c
MODE0_SPI_CHUNK_SAMPLES = 25
```

This is the SPI/DMA acquisition chunk size. Two chunks are accumulated into one 50-sample mode0 packet:

```c
MODE0_PACKET_CHUNKS = SAMPLE_POINT_NUM / MODE0_SPI_CHUNK_SAMPLES
```

The SPI/DMA layer should use the 25-sample chunk size. The mode0 filter/pack layer should use the 50-sample packet size.

## Where A Single Bad Word Becomes A Spike

The first code path that turns one RX word into a saved neural sample is in `Firmware/src/main.c`:

```c
u16_t sample = swapShort16(rx_words[NUM_CHANNELS * P_size + ch]);
int16_t centered_counts = (int16_t)((int32_t)sample - 32768);
mode_0_input_counts_buffer[physical_ch * SAMPLE_POINT_NUM + packet_sample] = centered_counts;
mode_0_input_buffer[physical_ch * SAMPLE_POINT_NUM + packet_sample] =
        ((float)centered_counts * scale_factor) * Filter_scale;
```

If this expression reads one corrupted or stale word:

```c
rx_words[NUM_CHANNELS * P_size + ch]
```

then only one target sample is affected:

```c
mode_0_input_buffer[physical_ch * SAMPLE_POINT_NUM + packet_sample]
```

That exactly matches the observed symptom: one channel, one sample, random-looking occurrence.

## Why It Was Not Just A Valid ADC Word From Another Channel

The spike could exceed the synthetic/input signal range. Therefore, it should not be explained as a valid ADC sample from another channel being routed to the wrong place.

The better interpretation is:

- a word in `mode_0_m_rx_buf` was occasionally not the correct current RHD conversion result;
- it may have been stale, partially overwritten, or corrupted by another memory/layout-related issue;
- once read by `structure_rx_data()`, it was treated as a normal ADC sample and propagated into raw data and EDF.

## Most Plausible Current Explanation

Because the 25-sample SPI chunk boundary was already used by timer/ISR logic before the fix, the resolved issue is probably not caused by a simple 400-word boundary miscalculation.

The most plausible explanation is that the previous larger mode0 SPI buffers changed RAM layout and/or consumed enough RAM to expose a single-word corruption issue.

The old allocation used:

```text
mode_0_m_tx_buf[1600]
mode_0_m_rx_buf[2][1600]
```

The fixed allocation uses:

```text
mode_0_m_tx_buf[800]
mode_0_m_rx_buf[2][800]
```

This changes the physical RAM addresses of later global buffers and reduces RAM pressure. If some stack overflow, adjacent buffer overwrite, EasyDMA-visible RAM layout issue, or rare ISR/main-thread memory corruption was landing near the old mode0 RX/input data region, the new layout could prevent that corrupted word from entering `mode_0_m_rx_buf` or `mode_0_input_buffer`.

## Concrete Suspect Locations

The spike enters the data stream here:

```c
u16_t sample = swapShort16(rx_words[NUM_CHANNELS * P_size + ch]);
```

The upstream data source is:

```c
const u16_t *rx_words = mode_0_m_rx_buf[completed_spi_buff];
```

If overflow occurs, this tail-copy path can also inject a bad word into the next buffer:

```c
mode_0_m_rx_buf[next_spi_buff][j] =
        mode_0_m_rx_buf[completed_spi_buff][MODE0_SPI_CHUNK_WORDS + j];
```

However, since the spike disappeared after shrinking the buffer allocation rather than changing the chunk boundary value, the root cause is more likely RAM layout/pressure or one-word memory corruption than a simple off-by-one in the mode0 chunk math.

## Practical Takeaway

Keep mode0 SPI/DMA buffer sizing based on `MODE0_SPI_CHUNK_WORDS`, not `SAMPLE_POINT_NUM`.

Use:

```c
#define SPI_TX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
#define SPI_RX_BUF_SIZE MODE0_SPI_CHUNK_WORDS
```

Do not size mode0 SPI DMA buffers from the 50-sample final packet window. The 50-sample size should remain in the processing and packet assembly layer only.

