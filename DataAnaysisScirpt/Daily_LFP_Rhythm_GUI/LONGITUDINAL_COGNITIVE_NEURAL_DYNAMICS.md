# Longitudinal Cognitive Neural Dynamics Analysis

这个分析层面向 10-20 天连续 slice，目标是在 PFC LFP 中寻找被昼夜节律、睡眠/清醒/working 状态、rule 学习阶段和 choice bias 调制的长期特征。

## 研究问题

核心问题：

- 在控制昼夜相位和睡眠/清醒/working 状态后，PFC LFP 的哪些频段、通道组、状态分层特征仍然随认知学习变量变化？
- 不同训练 rule、performance stage、left/right bias 是否解释了额外的长期 LFP 变化？
- 这些变化是否集中在特定 ZT 时间、特定 sleep/wake/working 状态、或特定学习阶段？
- Wake、NREM、REM、Working 等自然状态占比本身是否也会被 rule、learning stage、bias 调制，从而成为解释 LFP 变化时必须控制的中介变量？

## 数据层级

长 slice 不应该在 GUI 中反复直接操作原始 1 kHz LFP。GUI 会把 10-20 天 slice 先拆成自然天 H5；每完成一个自然天就可立刻进入特征、状态和 summary 流程，其余自然天继续在后台进程构建。推荐层级是：

1. Natural-day slice H5：保存该自然天片段内必要的连续 LFP/IMU、mask、behavior trial。
2. Feature layer：`/features` 中保存窗口级 band power、activity、valid fraction。
3. Hourly layer：`/rhythm` 和 `/behavior` 中保存每小时 LFP、state occupancy、rule/perf/bias。
4. Longitudinal layer：每个自然天 H5 的 `/longitudinal` 中保存 day-level research table 和模型结果。
5. Slice statistics layer：`slice_statistics.py` 合并同一 slice 的所有 `[LONG]` H5，输出 slice-level sidecar 统计结果。
6. Direct behavior statistics layer：`direct_behavior_statistics.py` 合并同一 slice 的 hourly table，用非回归的分组统计直接比较行为状态之间的 LFP 分布。
7. Paired trial context layer：`paired_trial_context.py` 不改写 H5，围绕每个 trial 的 sample/choice 时间建立 pre/post context table，分析 trial 前后的自然状态、LFP、PLV/PAC 与行为表现/RT 的关系。

`longitudinal_dynamics.py` 只读取单个 H5 的 hourly 数据；`slice_statistics.py` 会在所有自然天都完成 longitudinal dynamics 后，读取同一 slice 的全部 H5 并合并为连续 10-20 天 table。

## 变量定义

每小时一行的 research table 包括：

- 时间变量：`elapsed_day`、`day_index`、`zt_hour`、`circadian_sin`、`circadian_cos`、`is_dark`。
- 行为变量：`rule_code`、`perf_pct`、`perf_state_code`、`bias_score`、`bias_state_code`、trial counts。
- 状态变量：`occ_Wake`、`occ_NREM`、`occ_REM`、`occ_Working` 等 state occupancy。
- LFP 变量：`lfp_<channel_group>_<band>_norm`，默认使用 `/rhythm/hourly_feature_table_normalized`。
- 状态分层 LFP：`state_<state>_<channel_group>_<band>_norm`。

行为状态沿用 GUI 的 `/behavior` 约定：

- rule：`0=location_rule`，`1=frequency_rule`，`2=reversal_frequency_rule`。
- performance：`<60% unlearned`，`60-70% intermediate`，`>=70% learned`。
- bias：`<-0.1 left_bias`，`-0.1~0.1 neutral`，`>0.1 right_bias`。

RT 反应时间由 `Tevent.txt` 定义：

- sample start：`event 0 = S`。
- choice：sample start 后第一个 `event 8 = LickL` 或 `event 10 = LickR`。
- RT：`choice timestamp - sample start timestamp`，无选择或异常范围记为 `NaN`。
- RT class：每个 slice 内对有效 RT 的 `log(RT)` 拟合 3-component Gaussian mixture，按两个交点阈值转回 ms 后分为 `low/middle/high`。

## 模型

Day-level longitudinal dynamics 对每个 LFP target 使用嵌套线性模型：

```text
base model:
LFP ~ elapsed_day + circadian_sin + circadian_cos + state_occupancy

full model:
LFP ~ base model + rule + perf_state + bias_state
```

主要解释量是：

```text
delta_r2_behavior = R2(full model) - R2(base model)
```

这表示在已经控制长时间漂移、昼夜相位和睡眠/认知状态占比之后，rule / learning stage / bias 还能额外解释多少 LFP 变化。

Slice-level statistics 在此基础上增加探索性显著性筛选：

- LFP behavior modulation：对 `lfp_<group>_<band>` 和 `state_<state>_<group>_<band>` 都分别做 `rule_block`、`learning_stage_block`、`bias_block`、`behavior_all`，回答哪些频段能量被行为状态调制。
- State-specific LFP summary：把同一 band/group 在 Wake、NREM、REM、Working 中的结果放在一起比较，标记 top state、significant states 和 state selectivity，回答调制是跨状态普遍存在还是只在某个自然状态出现。
- Natural-state occupancy modulation：对 `occ_<state>` 使用同样的 rule/perf/bias block tests，回答 Wake/NREM/REM/Working 占比是否也随行为状态变化。
- Continuous tests：`perf_pct` 和 `bias_score` 的 partial association。
- Interaction screen：top targets 的 `behavior x circadian phase`。
- Secondary checks：state occupancy tests 也作为 LFP 结果解释的检查项；如果某个 LFP band 显著，同时对应状态占比也显著，需要优先检查该 LFP 结果是否主要由状态组成改变驱动。

默认显著性使用 day-block permutation，保留每一天内部小时顺序；p 值经过 Benjamini-Hochberg FDR 校正。若有效 day block 少于 3 天，仍输出 effect size，但显著性会被标记为 low-confidence。

## 直接分组统计

因为模型控制项会影响结果解释，GUI 还提供 `Direct Behavior Stats` panel。这个 panel 不拟合 regression model，也不控制 elapsed day、ZT 或 state occupancy，而是直接从合并后的 hourly LFP table 做分组统计：

- 按 `rule_code` 比较 location rule、frequency rule、reversal frequency rule 下的 LFP 分布。
- 按 `perf_state_code` 比较 unlearned、intermediate、learned 下的 LFP 分布。
- 按 `bias_state_code` 比较 left bias、neutral、right bias 下的 LFP 分布。
- 对 global LFP target 和 `state_<state>_<group>_<band>` 状态分层 LFP target 都做同样比较。
- 额外对 `occ_<state>` 做同样分组统计，检查 wake/NREM/REM/working 占比本身是否在不同 behavior state 下变化。

主要输出指标：

- `effect_size`：最大组间中位数差异除以 pooled IQR，越大表示原始分布差异越明显。
- `eta2`：组别解释的原始方差比例，作为直观效应量。
- `permutation_p`：打乱 hourly behavior group label 后的标签置换 p 值。
- `fdr_q`：对同一批直接分组检验做 Benjamini-Hochberg FDR。
- pairwise 表中保存每一对行为状态的 median/mean difference、Cliff's delta 和 Mann-Whitney p 值。

这个 panel 的优点是直观、少假设；限制是它不区分行为状态和昼夜相位/自然状态占比/跨天漂移是否混在一起。因此最稳妥的候选结果应同时满足：direct panel 中原始分布差异明显，且 model-based `Slice Statistics` 中在控制协变量后仍保留相同方向的效应。

Direct panel 的表格可以用三类过滤器快速缩小范围：

- `Region`：通道组/脑区，例如 PL、ILA、PFC。
- `State`：`LFP` 表示全局 hourly LFP；Wake、NREM、REM、Working 表示只在该自然状态内计算的 LFP。
- `Behavior`：rule、performance state、bias state。

任一过滤器选择 `None` 表示不按该维度筛选。
`Hourly LFP value` 分布图会在当前 Region/State/Behavior 条件下自动按 band 分面，直接生成多个频段图，方便横向比较 theta/beta/gamma 等频段。

## Paired Trial Context 分析

`Paired Trial Context` panel 回答的是更局部的 temporal question：一个 trial 之前的自然状态和 LFP/连接特征是否会影响这个 trial 的表现；这个 trial 的表现之后，是否更可能出现特定自然状态或 LFP/连接特征。它不替代 hourly 的 `Slice Statistics` / `Direct Behavior Stats`，而是提供 trial-centered paired evidence。

默认窗口：

- pre window：`sample_start - 1h` 到 `sample_start`。
- post window：`choice` 到 `choice + 1h`。

特征：

- 自然状态：pre/post Wake、NREM、REM、Working occupancy 和 post-pre delta。
- LFP：已有 hourly band power 和 state-stratified LFP target。
- PLV：每小时每个 band 的 16x16 channel-pair phase-locking value，包含 self-self。
- PAC：低频 phase 到高频 amplitude 的 band-pair modulation index，保存所有 phase channel x amplitude channel。

统计：

- pre context -> trial behavior：按 correct/error、RT class、performance state、bias state 比较 trial 前 context。
- trial behavior -> post context：按相同行为类别比较 trial 后 context。
- paired delta：同一 trial 的 post-pre delta 用符号翻转检验。
- p 值经过 Benjamini-Hochberg FDR；结果仍是单动物单 slice 的探索性证据。

## 输出

运行 `Longitudinal Dynamics` 后，会生成：

- `<h5>_longitudinal_hourly_table.csv`：每小时一行的完整 research table。
- `<h5>_longitudinal_effect_summary.csv`：每个 LFP target 的 `R2_base`、`R2_full`、`delta_r2_behavior` 和最大行为项。
- `<h5>_longitudinal_summary.json`：输出路径、top effects、分析摘要。
- `<h5>_longitudinal_behavior_timeline.png`：rule/perf/bias 长期时间线。
- `<h5>_longitudinal_behavior_effects.png`：behavior 额外解释度排名。
- `<h5>_longitudinal_circadian_heatmap.png`：top LFP target 的 day x ZT heatmap。
- H5 内新增 `/longitudinal`：保存轻量 numeric table 和 effect table，GUI 可快速读取。

运行 `Slice Statistics` 或 `Run Full Analysis (All Listed H5)` 完成所有同一 slice H5 后，会生成：

- `<slice_id>_slice_statistics_hourly_table.csv`：合并后的连续 slice hourly table。
- `<slice_id>_slice_statistics_effect_summary.csv`：每个 LFP target 的最佳 behavior/cognition test。
- `<slice_id>_slice_statistics_term_tests.csv`：rule、learning stage、bias、RT class、behavior_all 的 neural target block test。
- `<slice_id>_slice_statistics_lfp_behavior_modulation.csv`：带 target metadata 的行为调制总表，区分 global LFP、state-stratified LFP、PLV 和 PAC。
- `<slice_id>_slice_statistics_state_specific_lfp_summary.csv`：每个 group/band/test family 的 top natural state、state selectivity、global 对照和解释标签。
- `<slice_id>_slice_statistics_state_occupancy_tests.csv`：Wake/NREM/REM/Working 等自然状态占比是否被 rule、learning stage、bias 调制。
- `<slice_id>_slice_statistics_interaction_tests.csv`：top LFP targets 的 behavior x circadian phase 交互筛选。
- `<slice_id>_slice_statistics_partial_correlations.csv`：`perf_pct`、`bias_score`、`rt_median_ms` 的 partial correlation。
- `<slice_id>_slice_statistics_summary.json`：GUI reload 使用的总索引。
- `<slice_id>_slice_statistics_effects.png`、`circadian_heatmap.png`、`behavior_lfp_overlay.png`、`state_specific_lfp.png`、`state_occupancy_effects.png`：快速检查图。

这些文件是 output dir 中的 sidecar，不默认写回 daily H5，避免 HDF5 lock。

运行 `Direct Behavior Stats` 后，会生成：

- `<slice_id>_direct_behavior_hourly_table.csv`：直接统计使用的合并 hourly table。
- `<slice_id>_direct_behavior_lfp_group_tests.csv`：LFP/PLV/PAC target 按 rule/perf/bias/RT class 分组的 omnibus 直接检验。
- `<slice_id>_direct_behavior_lfp_pairwise_tests.csv`：每两个行为状态之间的原始分布差异。
- `<slice_id>_direct_behavior_state_occupancy_tests.csv`：自然状态占比按 behavior state 的直接分组差异。
- `<slice_id>_direct_behavior_continuous_associations.csv`：`perf_pct`、`bias_score`、`rt_median_ms` 与 neural target 的 Spearman 相关。
- `<slice_id>_direct_behavior_summary.json`：GUI reload 使用的总索引。
- `<slice_id>_direct_behavior_lfp_effects.png`、`lfp_distributions.png`、`state_occupancy_effects.png`：直接统计快速图。

运行 `Paired Trial Context` 后，会生成：

- `<slice_id>_rt_behavior_features.csv`：trial-level RT、RT class、sample/choice epoch 和 trial behavior。
- `<slice_id>_rt_behavior_features.json`：log-RT GMM component、ms 阈值、hourly RT summary。
- `<h5_base>_connectivity_features.npz/json`：每个 daily H5 的 hourly PLV/PAC matrix sidecar。
- `<slice_id>_paired_trial_context_table.csv`：trial-level pre/post context 和 post-pre delta 总表。
- `<slice_id>_paired_pre_context_behavior_tests.csv`：pre context 按 trial behavior 分组的置换检验。
- `<slice_id>_paired_post_context_behavior_tests.csv`：post context 按 trial behavior 分组的置换检验。
- `<slice_id>_paired_pre_post_delta_tests.csv`：同一 trial post-pre delta 的 paired 检验。
- `<slice_id>_paired_summary.json` 和 paired 快速图：GUI reload 使用的总索引和 top effects。

为保证长 slice 可运行，paired table 默认只把 PLV/PAC 的 mean 和 channel-group pair target 纳入统计；sidecar 中仍保存完整 16x16 matrix。若需要对所有 channel pair 做 trial-level 检验，可在 config 中设置 `connectivity.include_channel_pair_targets: true`，但这会显著增加运行时间和 FDR 比较数。

## 使用流程

GUI：

1. `Build Natural-Day H5s From Slice`
2. `Extract Features`
3. `Classify States`
4. `Summarize Rhythm`
5. `Longitudinal Dynamics`
6. `Slice Statistics`
7. `Direct Behavior Stats`
8. `Paired Trial Context`

也可以直接点击 `Run Full Analysis (All Listed H5)`：GUI 会只处理当前 H5 所属的同一 slice，逐个弹出 state review；所有 H5 完成 `[LONG]` 后自动运行 slice-level statistics。

CLI：

```bash
python3 -m Daily_LFP_Rhythm_GUI.continuous_lfp_rhythm.cli longitudinal-dynamics \
  --h5 /path/to/slice_lfp_rhythm.h5 \
  --config /path/to/config_default.yaml \
  --output-dir /path/to/output
```

## 解读建议

- 先看 `delta_r2_behavior` 排名前 10 的 target，确认是否集中在 theta/beta/gamma 或特定 PFC 深浅通道组。
- 再看 `top_behavior_term`，区分是 rule、learned stage 还是 left/right bias 在驱动。
- 对同一 band/group 查看 `state_specific_lfp_summary.csv`：如果只有 Wake 或 Working 的 q 值显著，而 global LFP 不显著，说明该长期调制可能只在任务/清醒相关状态中出现；如果 NREM/REM 也显著，说明学习状态可能改变了离线状态下的网络动力学。
- 查看 `state_occupancy_tests.csv`：如果 behavior 同时显著解释 Wake/NREM/REM/Working 占比，LFP 结果需要结合 state occupancy 一起解释，而不是直接归因于某个频段本身。
- 然后打开 `Direct Behavior Stats`：如果某个 band 在 learned vs unlearned 或 left/right bias 间的原始分布差异很大，说明它是值得回到原始信号检查的候选；如果 direct panel 显著但 model panel 不显著，通常意味着差异可能被昼夜相位、跨天漂移或状态占比解释掉了。
- 再打开 `Paired Trial Context`：如果某类 pre context 显著预测 correct/RT class，说明 trial 之前的自然状态或网络连接可能提供“准备状态”；如果 post context 在 error/slow RT 后显著变化，则提示行为表现可能影响后续离线状态或 PFC network dynamics。
- 对 top target 回到 hourly table，画 `day_index x zt_hour` heatmap，检查是否只是某个昼夜相位或缺失数据造成。
- 对候选 target 再回到 state-stratified 特征，确认变化是在 Wake、NREM、REM 还是 Working 中最明显。
- Slice statistics 中 `q < 0.05` 的结果应视为单动物单 slice 的候选长期特征；最终结论仍需要跨 slice / 跨动物复现。

## 后续迭代

- 如果同一动物有多个 slice，下一步应把多个 longitudinal hourly table 合并成 animal-level table。
- 如果有多只动物，最终模型应升级为 mixed-effects model：`animal_id` / `slice_id` 作为 random effect。
- 如果某个 band/rule 结果稳定，再回到更细的 feature window 或 trial-aligned LFP 做验证。
