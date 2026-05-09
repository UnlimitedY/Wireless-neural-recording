# Raw Data Overview GUI

这个 GUI 用于查看一个 `BW*` 原始数据根目录中行为、神经 EDF 和视频文件在同一绝对时间轴上的覆盖情况，并导出可用于后续归档分析的时间切片 JSON。

## 入口

```bash
python3 Raw_Data_Overview_GUI/main.py
```

运行 GUI 的环境支持 Python 3.8，并需要安装 `PyQt6`、`pyqtgraph`、`numpy`、`pyedflib`。视频 duration 读取优先使用 `ffprobe`，Windows 下建议安装 `ffmpeg` 并把 `ffprobe.exe` 所在目录加入 `PATH`。

## 数据目录

可以在 GUI 中选择 `BWXX` 或 `GUIBWXX` 文件夹。若选择 `BWXX`，程序会自动查找其下唯一的 `GUIBWXX` 子目录。

期望结构：

```text
BWXX/
  GUIBWXX/
    SD_card_files/
      .../Trial.txt
  Neural/
    Mode0/
      ...lfp.edf
    Mode3/
      ...LFP&ESA.edf
      ...mode3_raw.edf
      ...raw_data.edf
  video/
    ...YYYY-MM-DD-HH-MM-SS*.mp4
```

`SD_card_files` 和 `Neural` 是必需目录。`video` / `Video` 缺失时 GUI 仍可扫描行为和 EDF，并在日志中提示。为了兼容旧整理方式，程序也会接受 `GUIBWXX/Neural` 和 `GUIBWXX/Video` 这种嵌套结构。

## 时间轴

- 行为时间来自 `Trial.txt` 第一列 `Teensy3Clock.get()`，按 epoch seconds 转为 `Asia/Shanghai` 绝对时间。
- EDF 时间使用文件名 `YYYY-MM-DD-HH-MM-SS` 和 EDF header hardware timestamp 估计 start/end；若 header timestamp 不可读，则回退到文件名时间加 duration。
- 视频时间优先使用文件名中的 `YYYY-MM-DD-HH-MM-SS` 作为 start；如果文件名没有时间戳，也会从完整路径/父目录名中查找。duration 优先用 `ffprobe`，若不可用则尝试 OpenCV；都不可用时显示明显的 unknown-duration 竖线 marker。

## 图表

GUI 显示 5 条共享 x-axis 的轨道：

1. 行为：protocol 变化、performance 趋势、trial marker。
2. Mode0 LFP：文件覆盖条和 `Ch0` 类通道有效数据比例。
3. Mode3 LFP&ESA：文件覆盖条和 `Ch0` 类通道有效数据比例。
4. Mode3 raw：文件覆盖条和 `RawData` 类通道有效数据比例。
5. Video：视频覆盖条或未知 duration 竖线 marker。

EDF 丢包统计只读取一个命名通道，`<= -1000` 视为 missing。Mode0 LFP 和 Mode3 `LFP&ESA.edf` 会严格寻找 `Ch0` / `Ch_0` / `Ch 0` 这类 label；Mode3 raw EDF 会严格寻找 `RawData` / `Raw Data` / `Raw0` 这类 label。找不到对应 label 时该 EDF 会被跳过并写入日志，避免误用错误通道。现在不再计算时间 bin 分布，而是为每个 EDF 文件计算一个总 missing 占比，并在对应覆盖条旁显示百分比；覆盖条高度表示该文件的总 valid fraction，颜色从原轨道色、黄色到红色提示丢包严重程度。

为了保持缩放流畅，GUI 会把 EDF/视频覆盖条、protocol 切换线和 trial marker 合并成少量批量曲线绘制；trial marker 以短竖线显示，left/right 由 y 位置区分，outcome 由颜色区分。EDF missing 百分比文字过多时会抽样显示，完整数值仍保存在索引记录中。

## 缓存

索引缓存位于：

```text
Dataset local: BWXX/.raw_data_overview_cache/index_cache.json
Windows: %LOCALAPPDATA%\wireless_24_7\raw_data_overview\<dataset_hash>\index_cache.json
macOS/Linux: ~/.cache/wireless_24_7/raw_data_overview/<dataset_hash>/index_cache.json
```

每次成功扫描后会保存分析结果。再次选择同一个 `BWXX` / `GUIBWXX` 时，GUI 会先检查 Trial/EDF/Video 文件数量；如果数量没有变化，会直接加载保存的 index 并绘图，不再重新读取 EDF 或重新计算 missing。云盘同步造成的 mtime、路径 hash、ffprobe 路径变化不会让缓存失效；日志会提示 `file-count match`。点击 `Scan Dataset` 时也会优先复用匹配的保存结果。

如果自动匹配仍然找不到之前的结果，可以点击 `Load Saved Analysis JSON`，手动选择任意 `index_cache.json`。这个入口会直接加载缓存中的行为、EDF、视频和 missing 分析结果；如果当前已经选择了 BW/GUIBW 文件夹，会把后续 slice 导出的 `source_root` 适配到当前选择的数据根目录。旧版 version 4 缓存会在内存中迁移到当前 version 5 后加载，不需要重新分析原始 EDF。

## 切片 JSON

在行为图上拖拽蓝色 region，点击 `Add Slice` 可添加时间切片。点击 `Save Slices JSON` 导出：

```json
{
  "version": 1,
  "bw_id": "BW01",
  "source_root": ".../BW01",
  "timezone": "Asia/Shanghai",
  "created_at": "...",
  "global_start_iso": "...",
  "global_end_iso": "...",
  "slices": [
    {
      "id": 1,
      "label": "slice_001",
      "start_epoch_ms": 1770000000000,
      "end_epoch_ms": 1770003600000,
      "start_iso": "...",
      "end_iso": "...",
      "duration_s": 3600.0
    }
  ]
}
```

## 测试

```bash
python3 -m compileall Raw_Data_Overview_GUI Common_Analysis
python3 Raw_Data_Overview_GUI/test_data_index.py
```
