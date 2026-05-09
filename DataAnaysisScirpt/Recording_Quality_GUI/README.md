# Recording Quality GUI

这个目录对应记录质量检查 GUI。入口是 `main.py`，界面主体在 `gui.py`，质量分析逻辑在 `analysis.py`。

工作流：

1. 在 GUI 中选择 Sensor、Raw Data、LFP 或 LFP & ESA 文件类型并加载目录。
2. 按文件名时间分组展示 EDF 文件。
3. 为每个通道配置丢包阈值、替换值和清洗方法。
4. `AnalysisWorker` 调用 `analysis.py` 批量计算通道丢失率、min/max、CMR RMS、raw 高通 RMS、同步脉冲匹配率。
5. 额外窗口可查看 sensor timeline、LFP spectrogram、ESA correlation/baseline、raw high-pass、sync pulse details 和跨文件趋势。

共享依赖：

- 缺失值清洗统一走 `../Common_Analysis/preprocess.py::clean_data`。
- 同步脉冲检测统一走 `../Common_Analysis/sync_detection.py`。
- 后续如果修改数据丢失替换策略或 sync pulse 检测，只需要优先改共享目录。

示例数据：

- `Mode0NeuralDataDemo/`
- `Mode3NeuralDataDemo/`
- `BehaviorDataDemo/`
