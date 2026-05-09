# Trial-Level Mode3 GUI

这个目录对应 mode3 中按 trial 对齐的数据检查、展示和 H5 导出。入口是 `main.py`，界面主体在 `gui.py`，核心数据处理在 `data_processing.py`。

## 当前工作流

1. 单文件模式选择 `LFP&ESA.edf`、`mode3_raw.edf`、`sensor.edf`、`Trial.txt`、`Tevent.txt`。
2. GUI 从 raw EDF 的 `Alignment` channel 找 candidate alignment rising edge，并为每个 candidate 显示 alignment edge 前 `-300~-100 ms` 的 ROI。
3. 用户在 ROI 内点击 DBRuleSwitch time-alignment neural pulse 的起点。每个 ROI 最多保存一个 marker，可以跳过任意 ROI。
4. 每个 raw 文件至少需要 1 个 anchor：1 个 anchor 使用固定斜率 `fs_raw / 1000`；2 个及以上 anchor 使用 `sample = a * TrialStartTimestampMs + b` 线性拟合。
5. 拟合窗口显示 RMS/max residual；max residual 超过 `5 ms` 会提示 warning，但用户仍可确认。
6. 处理完成后，GUI 保持原有布局展示 raw sync、LFP spectrogram、band power、lick events、raster、ESA 和 sensor。

## Trial.txt 格式

共享解析器位于 `../Common_Analysis/trial_parsing.py`。DBRuleSwitch 新格式在 `PendingProtocolIndex` 后新增 `TrialStartTimestampMs`，再接 `nVisited` 和 state/time pairs：

```text
... PendingProtocolIndex TrialStartTimestampMs nVisited State1 Time1 State2 Time2 ...
```

也就是新扩展格式的第 23 列为 `TrialStartTimestampMs`（0-based index 23）。解析器使用 `nVisited` 明确读取 state history，不再只从行尾反向猜测。旧格式仍可读取基础字段和 state history，但没有有效 `TrialStartTimestampMs` 的 trial 会在新导出中被跳过并写入 warning。

## 对齐定义

DBRuleSwitch `ino` 中的 neural time-alignment pulse 已固化为：

- pulse frequency: `4 kHz`
- chip count: `9`
- chip width: `1 ms`
- pattern: `111001011`
- ROI: alignment rising edge 前 `-300~-100 ms`

GUI 的 marker 表示 9-chip 序列的起点。raw neural ROI 显示使用 `3.5-4.5 kHz` bandpass trace；该滤波只用于人工检查和展示。

## Batch 导出

Batch 页选择：

- Mode3 neural folder
- `Trial.txt`
- `Tevent.txt`
- output directory

程序递归扫描 neural folder，按 `YYYY-MM-DD-HH-MM-SS` 前缀组合：

- `LFP&ESA.edf`
- `mode3_raw.edf`
- `sensor.edf`

导出按天分文件，命名为：

```text
YYYY-MM-DD_Batch_Aligned_Trials.h5
```

## Sidecar

输出目录会保存 `mode3_alignment_annotations.json`。sidecar 按 raw 文件绝对路径记录：

- file fingerprint: path、size、mtime
- manual anchors
- fit result
- ROI window
- DBRuleSwitch pulse metadata
- raw sample rate

再次 batch 时，如果 raw 文件 fingerprint 未变化，会自动复用对应 anchors 和 fit；文件发生变化则需要重新确认。

## H5 兼容性

H5 dataset 名称保持旧 GUI 兼容：

- `raw_t`
- `raw_v`
- `raw_align_v`
- `lfp_t`
- `lfp_v`
- `esa_t`
- `esa_v`
- `raster_v`
- `sen_t`
- `sen_v`

新增 metadata 通过 attrs 保存，包括 alignment fit、`TrialStartTimestampMs`、pulse metadata、NaN 策略和 channel order。旧 loader 路径仍可读取上述 dataset；当前 loader 会额外读取新增 attrs。

## NaN 与通道顺序

连续信号缺失值策略已改为：`<= -1000` 的 raw neural、LFP、ESA、sensor 数据保存为 `NaN`。alignment 和 raster 这类离散通道保留原始值或置 0。

LFP、ESA、raster 通道顺序统一使用 daily rhythm 已使用的 mode3 probe shallow-to-deep 顺序，公共定义在 `../Common_Analysis/channel_map.py`：

```python
MODE3_SHALLOW_TO_DEEP_CHANNELS
```

GUI 展示中的 median rereference 使用 `nanmedian`；spectrogram 和 band power 只在显示时临时填补 NaN，不会写回 H5。
