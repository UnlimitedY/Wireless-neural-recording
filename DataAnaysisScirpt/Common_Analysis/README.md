# Common Analysis

这个目录放三个 GUI 共用的分析脚本，目的是让预处理、同步和解析逻辑只维护一份。

模块职责：

- `edf_io.py`：EDF 读取、硬件时间戳提取、LFP firmware phase compensation、daily GUI 所需的 NaN-based EDF loader。
- `channel_map.py`：mode0/mode3 probe 通道物理顺序定义和 shallow-to-deep reorder helper，daily rhythm 和 trial-level mode3 共用。
- `behavior_analysis.py`：`Trial.txt` 行为 trial 解析、inferred choice、trial-level Gaussian smoothing、hourly rule/performance/bias 状态和 H5 `/behavior` 写入。
- `behavior_rt.py`：RT 反应时间解析和分类。默认按 `Tevent.txt` 中 `0=S/sample start` 到 sample 后第一个 `8=LickL` 或 `10=LickR` 计算 RT；每个 slice 内对有效 RT 做 log-RT 三峰 Gaussian mixture，输出 low/middle/high、hourly RT summary 和 ms 阈值，供 recording summary、slice statistics、direct stats 和 paired context 共用。
- `preprocess.py`：通道 rereference、notch/lowpass、短 NaN gap 插值、质量 GUI 的缺失值替换策略、mode3 的线性清洗。
- `sync_detection.py`：raw neural sync pulse 的 band/energy 检测和 alignment pulse 匹配；mode3 新 workflow 已改为人工 ROI anchor，不再用它自动决定 neural pulse 时间。
- `trial_parsing.py`：`Trial.txt` 和 `Tevent.txt` 解析，支持 DBRuleSwitch 新格式的 `TrialStartTimestampMs` 和基于 `nVisited` 的 state history。
- `remote_data.py`：远程数据源连接器，把联想个人云等 NAS 挂载点统一解析为本地可读数据根目录。

修改原则：

- 数据丢失处理、通道映射、相位补偿、trial 文本解析和行为状态计算优先在这里改。
- GUI 目录只保留界面逻辑和 workflow orchestration。
- 如果新增共享逻辑，先放在这里，再由对应 GUI import。
- RT/Tevent 事件编号来自当前 Python workflow 中已使用的 DBRuleSwitch 约定；如果后续把 `.ino` 文件加入仓库，应同步校验 `0/1/8/10` 的含义并更新本 README。

联想个人云数据连接：

1. 在 macOS Finder 中用 `smb://<设备IP>/<共享名>` 连接联想个人云，或使用 `remote_data.py mount` 自动挂载 SMB。
2. 复制 `remote_data_profile.example.json` 为自己的 profile，修改 `mount_point` 和 `data_root`。不要把真实密码写入仓库；需要自动挂载时用环境变量 `LENOVO_CLOUD_USER` 和 `LENOVO_CLOUD_PASSWORD`。
3. 检查连接：

```bash
python3 -m Common_Analysis.remote_data --profile Common_Analysis/remote_data_profile.example.json check
```

4. 列出远程 EDF：

```bash
python3 -m Common_Analysis.remote_data --profile Common_Analysis/remote_data_profile.example.json list --glob "*.edf" --limit 20
```

5. GUI 中选择 `check` 输出的数据根目录，后续分析仍按本地文件夹读取。
