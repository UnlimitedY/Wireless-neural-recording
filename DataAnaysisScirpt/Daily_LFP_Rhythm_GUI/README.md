# Daily LFP Rhythm GUI

这个目录对应 daily 的 24/7 LFP 节律分析 GUI。入口是 `main.py` 或 `open_unified_gui.command`，核心界面在 `continuous_lfp_rhythm/gui_viewer.py`。

工作流：

1. 在 `Raw_Data_Overview_GUI` 中选择 BW 数据根目录、拖拽时间区间并导出 slice JSON。
2. 在本 GUI 中选择该 slice JSON，并在下拉框中选择一个 slice。
3. 点击 `Build Natural-Day H5s From Slice`。GUI 会启动独立后台进程，从 slice JSON 的 `source_root` 自动定位 `Neural/Mode0`、`Neural/Mode3` 和 `GUIBWxx/SD_card_files`，再按 slice 的绝对时间切成 `Asia/Shanghai` 自然天。
4. 每完成一个自然天，GUI 会立刻刷新 H5 列表，并自动排队执行 `Extract Features -> Classify States`。特征提取自动运行；state review 仍按 H5 一个一个弹窗等待用户确认。后台会继续构建后续自然天 H5，已构建好的文件会在当前弹窗结束后继续排队弹出。
5. `h5_schema.py` 将每个自然天内重叠的 EDF 映射到 day-slice-relative H5；H5 的 0 秒就是该自然天片段的 start，长度就是该片段 duration，因此 slice 头尾不足 24h 时会被保留为截断片段。
6. `features.py` 计算窗口级 LFP/IMU 特征。
7. `state_classification.py` 和 `state_review_wizard.py` 完成交互式状态审核。
8. `rhythm_stats.py` 汇总节律统计，`reports.py` 生成 HTML 报告。
9. `longitudinal_dynamics.py` 从每个自然天 H5 的 hourly LFP/state/behavior 数据生成 day-level research table、behavior effect summary 和快速图表。
10. `slice_statistics.py` 在同一 slice 的所有自然天 H5 都达到 `[LONG]` 后，合并 10-20 天 hourly 数据，运行 slice-level 统计分析并生成 sidecar CSV/JSON/PNG。
11. `direct_behavior_statistics.py` 提供一个少模型假设的直接统计 panel：不做回归，不控制协变量，只把 hourly LFP 按 rule/performance/bias 分组，比较原始分布差异。
12. `paired_trial_context.py` 提供独立的 `Paired Trial Context` panel：围绕每个 trial 的 sample/choice 时间，比较 trial 前后 1 小时自然状态、LFP、PLV 和 PAC 是否与 trial behavior/RT 有关。

行为状态：

- slice H5 新增 `/behavior`，保存 trial-level 行为字段和 hourly 行为状态。
- `rule` 使用 Trial.txt 中的三类训练 rule：`0=location_rule`、`1=frequency_rule`、`2=reversal_frequency_rule`。
- performance 由 trial outcome 重新计算：correct=100，error=0，其他 outcome 不参与；状态为 `<60% unlearned`、`60-70% intermediate`、`>=70% learned`。
- bias 表示动物实际选择偏向：correct trial 选择目标侧，error trial 选择相反侧，no-response/earlylick 不计入；bias score `<-0.1 left_bias`、`-0.1~0.1 neutral`、`>0.1 right_bias`。
- trial-level 趋势使用 50-trial Gaussian window、sigma=20 trials。长 slice 会先在整个 parent slice 上平滑，再切回自然天 H5，避免每天边界重启行为趋势；每小时有效 trial 不足 10 个时使用平滑趋势辅助；无 trial 的小时沿用上一小时状态，slice 开头无 trial 则保持 unknown。
- Recording Summary tab 中的 `Hourly Behavior State` 图会与 hourly LFP/state 图按同一连续时间轴显示，底部刻度为真实本地日期/小时。

状态审核：

- build 后的自动队列默认会把每个新完成的 H5 跑到第 4 步：`Extract Features` 自动执行，随后弹出 `Classify States` 人工审核窗口。state review 弹窗一次只处理一个 H5；后台 build 会继续生成后续自然天 H5，准备好的 H5 会排队等待当前弹窗结束。
- 若用户取消某个 state review，自动队列会暂停，避免后续窗口继续弹出；点击 `Resume Auto Feature/State Queue` 可继续处理剩余 H5。
- Step 2 `Review REM Thresholds` 中，REM IMU histogram 仍使用非 Working、非 NREM 的候选池来确认静止阈值；80-250 Hz 时间图、80-250 Hz histogram 和 preview 中的 `HF low candidate` 均只使用低于当前 REM IMU threshold 的静止候选池，因此该步骤只区分静止 REM 与静止 wake，减少运动和其他状态对 REM 阈值的干扰。

兼容说明：

- 旧的按 Mode0 / Mode3 目录构建 daily H5 的底层函数仍保留，但 GUI 主工作流改为 slice JSON 驱动。
- GUI 主流程会把长 slice 拆成多个自然天 H5，并在后台继续构建，避免 10-20 天数据全部处理完之前界面一直等待。
- 每次点击 `Build Natural-Day H5s From Slice` 时，后台会先按当前 slice 预计算所有自然天 H5 的目标文件名，并检查当前 Output Dir、配置里的 `outputs/BWxx`、以及 H5 list 中是否已经存在；如果已有文件集中在另一个 `outputs` 目录，GUI 会自动切到该目录。已存在的自然天会被跳过，只有缺失的自然天才会进入 EDF indexing、coverage 和写 H5 流程。若全部已存在，会直接刷新列表并结束。
- 顶部有两条独立进度条：`Build Slice` 只显示后台自然天 H5 构建进度，`Current H5` 只显示当前选中 H5 的 feature/state/summary/report 等操作进度；后台 build 不会再覆盖单个 H5 操作的进度，也不会在单个 H5 操作时自动切换当前加载文件。
- 自动 feature/state 队列会跳过已经存在 `features/band_power` 或已经完成 `states/review` 的 H5，避免重复处理；手动的 `Extract Features`、`Classify States`、`Summarize Rhythm`、`Longitudinal Dynamics` 仍然只作用于当前选中的单个 H5。
- H5 文件仍按自然天分开保存，但 `Recording Summary` 会自动把同一 slice 的已完成自然天 H5 拼成连续时间轴显示；底部刻度显示真实本地日期/小时，内部仍用“从首个 summary H5 起算的小时”作为轻量坐标，归一化参考可在当天均值和整个 slice 均值之间切换。该 summary 现在也会显示 RT class/median RT，以及已有 sidecar 中的 hourly PLV/PAC summary。
- `Recording Summary` 的 `Reference` 下拉框可切换归一化参考，默认 `Daily mean`，即每个自然天 H5 用当天均值作为参考；`Whole-slice mean` 会把已完成的同一 slice H5 合并为一个参考池，用于观察跨天整体漂移。
- H5 列表会用 `[BUILT]`、`[FEATURES]`、`[STATE]`、`[REVIEW]`、`[SUMMARY]`、`[LONG]` 标记每个自然天 H5 的当前分析进度；Windows/HDF5 文件仍在写入或被锁定时显示 `[BUSY]`，等后台 build 或分析写入完成后刷新即可继续加载和分析。
- H5 列表在对应 slice statistics sidecar 已生成后会显示 `[STATS]`。该标记来自 output dir 中的 slice-level summary JSON，不会改写每个 daily H5。
- `Run Full Analysis (All Listed H5)` 会只处理当前 H5 所属的同一 slice 文件：逐个执行 `Extract Features -> State Review -> Summarize Rhythm -> Longitudinal Dynamics`，state review 仍逐个弹窗；所有 H5 到 `[LONG]` 后自动运行 slice statistics。
- `Slice Statistics` tab 可手动或自动运行最终统计，默认 `Daily mean` 参考、1000 次 day-block permutation、Benjamini-Hochberg FDR。可切换 `Whole-slice mean` 后重跑，用于观察跨天整体漂移参考下的结果。
- Slice statistics 会把研究问题拆成三层输出：全局/状态分层 LFP 是否被 rule、learning stage、bias、RT class 调制；这种调制是否只在 Wake/NREM/REM/Working 等特定自然状态中出现；自然状态占比本身是否也随行为状态变化。若 PLV/PAC sidecar 已存在，hourly table 会把 mean/group-level PLV/PAC 作为 connectivity targets 一起进入模型；完整 16x16 channel-pair matrix 保留在 paired trial context 侧使用。输出包括 `<slice_id>_slice_statistics_hourly_table.csv`、`effect_summary.csv`、`lfp_behavior_modulation.csv`、`state_specific_lfp_summary.csv`、`state_occupancy_tests.csv`、`interaction_tests.csv`、`partial_correlations.csv`、`summary.json` 和快速图。统计结果是单动物单 slice 的探索性证据，不作为跨动物最终因果结论。
- `Direct Behavior Stats` tab 用于不依赖模型地检查同一个问题：它直接比较不同 rule、performance state、bias state、RT class 下的 hourly LFP/PLV/PAC 分布，输出 median/mean 差异、eta2、pairwise Mann-Whitney、标签置换 p 值和 FDR q 值。它不控制 elapsed day、ZT、state occupancy，因此适合回答“原始分布是否真的不同”，并与 `Slice Statistics` 的模型控制结果互相验证。
- Direct behavior stats 表格上方提供 `Region`、`State`、`Behavior` 三个过滤器；`State=LFP` 表示不分自然状态的全局 LFP target，`State=Wake/NREM/REM/Working` 表示状态分层 LFP target；每个过滤器选择 `None` 表示不启用该维度过滤。`Hourly LFP value` 分布区会自动把当前条件下的所有频段分别画成多个图，因此不再需要单独选择 band。
- Direct behavior stats 的 hourly LFP value 分布图会读取 pairwise 检验结果，在类别之间标注显著性：`* p<0.05`、`** p<0.01`、`*** p<0.001`、`ns` 表示不显著。
- Direct behavior stats 输出包括 `<slice_id>_direct_behavior_hourly_table.csv`、`lfp_group_tests.csv`、`lfp_pairwise_tests.csv`、`state_occupancy_tests.csv`、`continuous_associations.csv`、`summary.json` 和分布/效应快速图。
- `Paired Trial Context` tab 用于 trial-level temporal context，而不是 hourly group comparison。RT 定义为 `Tevent event 0=S/sample start` 到 sample 后第一个 `event 8=LickL` 或 `event 10=LickR`；每个 slice 内用 log-RT 3-component Gaussian mixture 自动分成 low/middle/high，并把阈值写入 `<slice_id>_rt_behavior_features.json`。
- Paired context 默认取每个 trial 前后各 1 小时：pre window 是 `sample_start - 1h` 到 `sample_start`，post window 是 `choice` 到 `choice + 1h`。panel 会输出 pre context 是否预测 trial behavior、trial behavior 后是否出现特定 post context、以及同一 trial 的 post-pre paired delta 是否显著。为了避免 10-20 天 slice 过慢，Paired context 默认也使用 PLV/PAC 的 mean 和 channel-group pair targets；完整 16x16 channel-pair targets 需要显式设置 `connectivity.include_channel_pair_targets: true`。
- PLV/PAC 不写回 H5，而是按每个 daily H5 生成 `<h5_base>_connectivity_features.npz/json` sidecar。PLV 为每小时每个 band 的 16x16 channel pair 矩阵，包含 self-self；PAC 只计算低频 phase 到高频 amplitude 的 band pair，同样保存 16x16 channel pair。
- `Recording Summary` 的 PLV/PAC 图默认显示 PLV off-diagonal mean 和 PAC all-pair mean；如果 sidecar 不存在，会显示 missing，并提示先运行 `Paired Trial Context` 或 connectivity feature extraction。
- Slice/direct 的 hourly 统计默认使用 PLV/PAC 的 mean 和 channel-group pair targets，控制多重比较数量；如确实要把所有 16x16 channel-pair targets 也加入 slice-level table，可在 config 中设置 `connectivity.include_channel_pair_targets: true`，但运行时间和 FDR 比较数会明显增加。
- Paired context 输出包括 `<slice_id>_paired_trial_context_table.csv`、`paired_pre_context_behavior_tests.csv`、`paired_post_context_behavior_tests.csv`、`paired_pre_post_delta_tests.csv`、`paired_summary.json` 和快速图。旧 H5 不含 raw LFP 时会跳过 PLV/PAC sidecar，但仍可做 behavior/state/LFP hourly context 分析。
- build 时会先扫描 EDF 文件名，再按 slice 时间加 `slice_build.filename_prefilter_margin_hours` 默认前后各 36 小时做 coverage 预筛；只有 slice 附近的 EDF 会读 header 计算精确 coverage。若文件名没有时间戳或预筛为空，会保守保留以避免漏数据。
- slice H5 会在 `/metadata` 中写入 `recording_scope="slice"`、`slice_start_epoch_ms`、`slice_end_epoch_ms`、`slice_label` 和 `slice_json_path`。
- 旧 slice H5 不会自动补写 `/behavior`；需要重新从 slice JSON 构建一次才会包含行为状态。
- GUI 时间轴读取 `/time.attrs["time_origin_epoch_ms"]`，因此 slice H5 中显示的是 slice 对应的真实绝对时钟时间，而不是从当天 00:00 开始。
- Windows + Python 3.8 可运行；时区优先使用内置或 `backports.zoneinfo`，缺少依赖时仍支持 `Asia/Shanghai` / `UTC`。

共享依赖：

- EDF 读取、硬件时间戳、LFP firmware phase compensation 来自 `../Common_Analysis/edf_io.py`。
- Trial.txt 行为解析、inferred choice、hourly performance/bias/rule 状态来自 `../Common_Analysis/behavior_analysis.py`。
- RT/Tevent 解析和 log-RT Gaussian mixture 分类来自 `../Common_Analysis/behavior_rt.py`。
- CMR、bipolar rereference、notch/lowpass、缺失片段插值来自 `../Common_Analysis/preprocess.py`。
- `continuous_lfp_rhythm/edf_io.py` 和 `continuous_lfp_rhythm/preprocess.py` 只是兼容入口，实际实现集中在共享目录。

补充内容：

- `legacy_mode0_analysis/` 保留原 Mode0 notebook 和示例数据。
- `reference_paper/` 保留参考文献。
- `outputs/` 和 `outputs_test/` 是 daily H5 / report 输出位置。
- `LONGITUDINAL_COGNITIVE_NEURAL_DYNAMICS.md` 说明 10-20 天 slice 的研究问题、变量、模型和输出文件。
