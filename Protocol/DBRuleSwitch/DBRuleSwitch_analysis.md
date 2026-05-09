# DBRuleSwitch.ino 范式分析与流程图

文件：`Protocol/DBRuleSwitch/DBRuleSwitch.ino`

## 1. 总体结构

这个范式可以拆成 4 层：

1. `loop()` 调度层：负责暂停/恢复、trial 完成后的结果更新、协议切换、下一 trial 构建、block 间隔控制。
2. `applyProtocolPreset()` / `autoChangeProtocol()`：定义 `protocol0 -> protocol10 -> BlockSwitchMode` 的训练推进逻辑。
3. `trialSelection()` + `rule_define()`：决定当前 trial 的刺激组合、正确侧、是否启用 antibias / congruency antibias。
4. `construct_matrix_and_Run()`：把一个 trial 具体展开成 gpSMART 状态机。

关键代码位置：

- `loop()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L540)
- `construct_matrix_and_Run()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L866)
- `UpdateTrialOutcome()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L1477)
- `applyProtocolPreset()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L1675)
- `autoChangeProtocol()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L1763)
- `trialSelection()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L1893)
- `computeTrialType()` / `rule_define()`：[DBRuleSwitch.ino](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/Protocol/DBRuleSwitch/DBRuleSwitch.ino#L2740)

## 2. protocol 语义总表

| Protocol | 主要阶段 | rule | `TrialBlockInitPeriod` | `TimeOut` | 关键特点 |
| --- | --- | --- | --- | --- | --- |
| 0 | Habituation | 0 | 50 ms | 2000 ms | 位置规则；高比例 free drop |
| 1 | Habituation | 0 | 100 ms | 2000 ms | 同上 |
| 2 | Habituation | 0 | 150 ms | 2000 ms | 同上 |
| 3 | Habituation | 0 | 200 ms | 2000 ms | 同上 |
| 4 | Habituation | 0 | 250 ms | 2000 ms | 同上，向正式 task 过渡 |
| 5 | Location | 0 | 250 ms | 3000 ms | 正式 loc；允许 free drop / autoReward |
| 6 | Location retention | 0 | 250 ms | 8000 ms | retention；使用完整刺激组合 |
| 7 | Frequency | 1 | 250 ms | 3000 ms | 频率规则；允许 free drop / autoReward |
| 8 | Frequency retention | 1 | 250 ms | 8000 ms | retention；完整刺激组合 |
| 9 | Reversal frequency | 2 | 250 ms | 3000 ms | 频率反转规则；允许 free drop / autoReward |
| 10 | Reversal retention | 2 | 250 ms | 8000 ms | retention；完整刺激组合；末端进入 block switch |

规则定义：

- `rule = 0`：按 `stimu1` 的左右位置决定正确侧。
- `rule = 1`：按 `stimu2` 的频率高低决定正确侧，`stimu2 < 0 -> left`，`stimu2 > 0 -> right`。
- `rule = 2`：频率反转，`stimu2 < 0 -> right`，`stimu2 > 0 -> left`。
- 当 `stimu2 == 0` 且 `rule = 1/2` 时，trial 正确侧是随机分配的“模糊 trial”。

## 3. 总体 protocol 流程图

```mermaid
flowchart TD
    A0["Protocol 0\nrule0, init=50 ms"] -->|valid trials > 100| A1["Protocol 1\nrule0, init=100 ms"]
    A1 -->|valid trials > 100| A2["Protocol 2\nrule0, init=150 ms"]
    A2 -->|valid trials > 100| A3["Protocol 3\nrule0, init=200 ms"]
    A3 -->|valid trials > 100| A4["Protocol 4\nrule0, init=250 ms"]
    A4 -->|valid trials > 100| P5["Protocol 5\nLocation\nrule0, timeout=3 s"]
    P5 -->|currProtocolTrials >= 250\nand left/right easy perf50 >= 75| P6["Protocol 6\nLocation retention\nrule0, timeout=8 s"]
    P6 -->|currProtocolTrials >= 500\nand left/right easy perf50 >= 75| P7["Protocol 7\nFrequency\nrule1, timeout=3 s"]
    P7 -->|currProtocolTrials >= 250\nand left/right easy perf50 >= 75| P8["Protocol 8\nFrequency retention\nrule1, timeout=8 s"]
    P8 -->|currProtocolTrials >= 500\nand left/right easy perf50 >= 75| P9["Protocol 9\nReversal frequency\nrule2, timeout=3 s"]
    P9 -->|currProtocolTrials >= 250\nand left/right easy perf50 >= 75| P10["Protocol 10\nReversal retention\nrule2, timeout=8 s"]
    P10 -->|currProtocolTrials >= 500\nand left/right easy perf50 >= 75| BS["Enter BlockSwitchMode\nBlockSwitchMode=1\nPendingProtocolIndex=5"]

    BS --> B0["Step 0\ncurrent rule0 block\nprotocol 5"]
    B0 -->|>=250 valid trials\nand side easy perf50 >= 75| B1{"Randomly choose\nsecond rule"}
    B1 -->|choose rule1| B7["protocol 7"]
    B1 -->|choose rule2| B9["protocol 9"]
    B7 -->|>=250 valid trials\nand side easy perf50 >= 75| B9R["protocol 9"]
    B9 -->|>=250 valid trials\nand side easy perf50 >= 75| B7R["protocol 7"]
    B9R -->|>=250 valid trials\nand side easy perf50 >= 75| B5["protocol 5"]
    B7R -->|>=250 valid trials\nand side easy perf50 >= 75| B5
    B5 -->|cycle repeats| B0

    note1["注意：真正切换协议不是立刻生效。\n先写入 PendingProtocolIndex，只有到下一次 TrialBlockOnset==1\n或 manual change 时才执行 switchToProtocol()."]
    BS -.-> note1
```

## 4. BlockSwitchMode 关键逻辑图

```mermaid
flowchart TD
    S["trial 结束后\nloop() -> autoChangeProtocol(false)"] --> Q{"PendingProtocolIndex != 255 ?"}
    Q -->|Yes| Q1{"TrialBlockOnset == 1\nor manual?"}
    Q1 -->|Yes| Q2["switchToProtocol(PendingProtocolIndex)\n重置 currProtocolTrials/perf\n如 rule 改变则记录 PreviousRule\n并 activateRuleSwitchHint()"]
    Q1 -->|No| Q3["保持当前协议直到下一个 block onset"]
    Q -->|No| M{"BlockSwitchMode == 0 ?"}
    M -->|Yes| T["按 training 模式推进\n0->1->2->3->4->5->6->7->8->9->10"]
    M -->|No| B{"currProtocolTrials >= 250\nand left/right easy perf50 >= 75\nor manual?"}
    B -->|No| B0["继续当前 block"]
    B -->|Yes| B1{"BlockSwitchStep"}
    B1 -->|0| B2["随机选择第二规则\nPending=7 或 9\nBlockSwitchStep=1"]
    B1 -->|1| B3["切到剩下那个规则\nPending=9 或 7\nBlockSwitchStep=2"]
    B1 -->|2| B4["回到 protocol 5\nPending=5\nBlockSwitchStep=0"]
```

BlockSwitchMode 还有 4 个非常重要的隐含控制量：

- `TrialBlockOnset`：标记“当前是不是一个 block 的第一 trial”。
- `InterBlockIntervalMs`：只有 block onset 且间隔到时，才允许真正开始下一个 block。
- `RuleSwitchHintActive`：rule 发生改变后的前 5 个正确 trial 会打开提示。
- `PreviousRule`：block switch 时用于判断 congruent / incongruent trial。

## 5. trial 选择逻辑图

```mermaid
flowchart TD
    A["trialSelection()"] --> B["判断是否 retention protocol\n6/8/10"]
    B --> C["useFullStimuCombo = BlockSwitchMode==1 或 retention"]
    C --> D["如果 training 且非 retention\n计算 side antibias 概率 antiBiasLeftProb"]
    D --> E["如果 BlockSwitchMode==1\n基于 PreviousRule 与 current rule\n估计 congruent / incongruent 表现\n得到 congDesiredProb"]
    E --> F["最多尝试 64 次 rejection sampling"]
    F --> G["随机抽 stimu1 in {-1,1}"]
    G --> H{"useFullStimuCombo ?"}
    H -->|Yes| H1["stimu2 in {-1,-0.5,0,0.5,1}"]
    H -->|No| H2["stimu2 only in {-1,1}"]
    H1 --> I["rule_define(rule, stimu1, stimu2)\n决定 TrialType 和 SampleType"]
    H2 --> I
    I --> J{"training 且非 retention\n需要 side antibias ?"}
    J -->|Yes| K["按 antiBiasLeftProb\n决定是否接受该抽样"]
    J -->|No| L
    K --> L{"BlockSwitchMode==1 且\n启用 congruency antibias ?"}
    L -->|Yes| M["按 congDesiredProb\n决定是否接受 congruent / incongruent / ambiguous"]
    L -->|No| N["接受"]
    M --> N
    N --> O["输出 currStimu / TrialType / SampleType"]
    O --> P["连续 >=10 次 early lick -> EL_Favor=0"]
```

这里最关键的行为差异：

- training 非 retention：只抽最容易的 `stimu2 = -1 / +1`，不抽中间频率。
- retention 或 BlockSwitchMode：开启完整 `5` 级频率组合，包含 `stimu2 = 0` 的模糊 trial。
- BlockSwitchMode：不是做 side antibias，而是做 congruent / incongruent antibias。

## 6. trial-level 状态流程图

### 6.1 protocol 0-4 的 trial 状态图

```mermaid
stateDiagram-v2
    [*] --> TrialBlockDelay
    TrialBlockDelay --> NeuralReader: Tup and block onset
    TrialBlockDelay --> CapDisableBeforeTrialBlockEnd: DI1Rising on block-onset self-init window
    TrialBlockDelay --> TrialStart: Tup and not block onset

    NeuralReader --> CapReinit: SoftEvent1 or Tup
    CapReinit --> TrialStart
    TrialStart --> PreCueDelayPeriod
    PreCueDelayPeriod --> GiveFreeDrop: free reward path
    PreCueDelayPeriod --> SampleDelay: no free reward
    GiveFreeDrop --> SampleDelay
    SampleDelay --> SampleCue

    SampleCue --> Reward: correct lick during sample
    SampleCue --> SampleCue: wrong lick loops in shaping
    SampleCue --> AnswerPeriod: Tup

    AnswerPeriod --> Reward: correct lick
    AnswerPeriod --> AnswerPeriod: wrong lick ignored
    AnswerPeriod --> NoResponse: Tup

    Reward --> RewardConsumption
    RewardConsumption --> Fixed_ITI
    Fixed_ITI --> Fixed_ITI_Return: any lick
    Fixed_ITI_Return --> Fixed_ITI
    Fixed_ITI --> TrialEnd: Tup
    NoResponse --> TrialEnd
    TrialEnd --> CapDisableBeforeTrialBlockEnd: Tup
    TrialEnd --> [*]: DI1Falling
    CapDisableBeforeTrialBlockEnd --> TrialBlockEnd
    TrialBlockEnd --> [*]
```

### 6.2 protocol 5-10 / BlockSwitchMode 的 trial 状态图

```mermaid
stateDiagram-v2
    [*] --> TrialBlockDelay
    TrialBlockDelay --> NeuralReader: Tup and block onset
    TrialBlockDelay --> CapDisableBeforeTrialBlockEnd: DI1Rising on block-onset self-init window
    TrialBlockDelay --> TrialStart: Tup and not block onset

    NeuralReader --> CapReinit: SoftEvent1 or Tup
    CapReinit --> TrialStart
    TrialStart --> PreCueDelayPeriod
    PreCueDelayPeriod --> GiveFreeDrop: protocol 5/7/9 free drop path
    PreCueDelayPeriod --> SampleDelay: default
    GiveFreeDrop --> SampleDelay
    SampleDelay --> SampleCue

    SampleCue --> Reward: correct lick
    SampleCue --> ErrorTrial: wrong lick
    SampleCue --> AnswerPeriod: Tup

    AnswerPeriod --> Reward: correct lick
    AnswerPeriod --> ErrorTrial: wrong lick
    AnswerPeriod --> NoResponse: Tup

    ErrorTrial --> TimeOut
    Reward --> RewardConsumption
    RewardConsumption --> StopLicking
    StopLicking --> StopLickingReturn: any lick
    StopLickingReturn --> StopLicking
    StopLicking --> Fixed_ITI: Tup
    TimeOut --> Fixed_ITI
    Fixed_ITI --> Fixed_ITI_Return: any lick
    Fixed_ITI_Return --> Fixed_ITI
    Fixed_ITI --> TrialEnd: Tup
    NoResponse --> TrialEnd
    TrialEnd --> CapDisableBeforeTrialBlockEnd: Tup
    TrialEnd --> [*]: DI1Falling
    CapDisableBeforeTrialBlockEnd --> TrialBlockEnd
    TrialBlockEnd --> [*]
```

## 7. trial level 时序图

```mermaid
sequenceDiagram
    participant Mouse
    participant Loop as loop()
    participant Select as trialSelection()
    participant Matrix as construct_matrix_and_Run()
    participant Smart as gpSMART state machine
    participant Update as UpdateTrialOutcome()
    participant Change as autoChangeProtocol()

    Loop->>Select: 选择 currStimu / TrialType / SampleType
    Loop->>Matrix: 根据当前 protocol 构建 trial matrix
    Matrix->>Smart: AddState + Run
    Smart-->>Mouse: TrialBlockDelay / TrialStart / cue / sample / reward / timeout
    Mouse-->>Smart: self-init / lick / no response
    Smart-->>Loop: smartFlag[3]=1 (trial 完成)
    Loop->>Update: 解析 stateVisited / events
    Update-->>Loop: TrialOutcome, is_earlylick, currProtocolTrials, perf
    Loop->>Change: 检查是否要切 protocol 或写 PendingProtocolIndex
    Loop->>Loop: autoReward()
    Loop->>Select: 为下一 trial 再次抽样
```

trial 完成后真正发生的顺序，对应 `loop()` 中的固定后处理链：

1. `UpdateTrialOutcome()`
2. `write_SD_trial_info()`
3. `SendTrialInfo2PC()`
4. `autoChangeProtocol(false)`
5. `autoReward()`
6. `trialSelection()`
7. `write_SD_para_S()`

## 8. protocol level 时序图

```mermaid
sequenceDiagram
    participant Trial as Trial n finished
    participant Update as UpdateTrialOutcome()
    participant Perf as easy perf / currProtocolTrials
    participant Change as autoChangeProtocol()
    participant Pending as PendingProtocolIndex
    participant NextBlock as TrialBlockOnset
    participant Switch as switchToProtocol()

    Trial->>Update: 根据 visited states 标记 correct / error / no-response / earlylick
    Update->>Perf: 仅 correct / error 计入 currProtocolTrials
    Perf->>Change: 提供 easy perf50 与 currProtocolTrials
    Change->>Pending: 若满足门槛，则写入下一 protocol
    Note over Pending: 不一定立刻切换
    NextBlock->>Change: 到下一个 block onset 时再次检查
    Change->>Switch: switchToProtocol(PendingProtocolIndex)
    Switch->>Switch: applyProtocolPreset()
    Switch->>Switch: 若 rule 改变，记录 PreviousRule 并激活 RuleSwitchHint
    Switch-->>Trial: 后续 trial 按新 protocol 运行
```

## 9. protocol 转移条件汇总

### 9.1 进入下一 protocol 的主条件

- `protocol0 -> 1`：`currProtocolTrials > 100`
- `protocol1 -> 2`：`currProtocolTrials > 100`
- `protocol2 -> 3`：`currProtocolTrials > 100`
- `protocol3 -> 4`：`currProtocolTrials > 100`
- `protocol4 -> 5`：`currProtocolTrials > 100`
- `protocol5 -> 6`：`currProtocolTrials >= 250` 且左右 easy trial 最近 50 次表现都 `>= 75`
- `protocol6 -> 7`：`currProtocolTrials >= 500` 且左右 easy trial 最近 50 次表现都 `>= 75`
- `protocol7 -> 8`：`currProtocolTrials >= 250` 且左右 easy trial 最近 50 次表现都 `>= 75`
- `protocol8 -> 9`：`currProtocolTrials >= 500` 且左右 easy trial 最近 50 次表现都 `>= 75`
- `protocol9 -> 10`：`currProtocolTrials >= 250` 且左右 easy trial 最近 50 次表现都 `>= 75`
- `protocol10 -> BlockSwitchMode`：`currProtocolTrials >= 500` 且左右 easy trial 最近 50 次表现都 `>= 75`

### 9.2 easy performance 的定义

- 只统计当前 protocol。
- 只统计 `SampleType == 2` 的 easy trial。
- 只把 `Outcome == correct/error` 算进分母。
- 左右两侧分别看最近 `50` 个 easy valid trials。

### 9.3 valid trial 的定义

`currProtocolTrials` 只在以下结果下递增：

- `correct`
- `error`

不会计入：

- `no response`
- `early lick`

### 9.4 BlockSwitchMode 的 block 内转移条件

- 当前 block 完成后，如果 `currProtocolTrials >= 250` 且左右 easy perf50 都 `>= 75`，就准备切到下一 block。
- 真正切换动作要等到 `TrialBlockOnset == 1`。
- 切换序列是三段循环：
  - `5 -> (7 或 9，随机)`
  - `(7 或 9) -> 另一个`
  - `另一个 -> 5`
  - 然后重复

## 10. 这个文件里最重要的隐含设计点

### 10.1 训练不是“trial 结束立刻换 protocol”，而是“先挂起，等 block onset 再切”

这由 `PendingProtocolIndex` 和 `TrialBlockOnset` 共同实现。这样 protocol 切换总在 block 边界发生，不会把一个 block 中途切断。

### 10.2 BlockSwitchMode 有真正的 inter-block gate

当 `BlockSwitchMode == 1` 且 `TrialBlockOnset == 1` 时，`loop()` 会先检查：

- `InterBlockIntervalMs` 是否到时
- 是否已经播过 ready cue
- 自启动输入是否满足

只有这些条件都满足才会真正构建下一个 trial matrix。

### 10.3 rule switch 后前 5 个正确 trial 会给额外提示

如果 `switchToProtocol()` 发现 `rule` 变化：

- 保存 `PreviousRule`
- `RuleSwitchHintActive = 1`
- 前 5 个正确 trial：
  - `TrialBlockDelay` 增加 cue 提示
  - 奖励时长翻倍，但上限 `200 ms`

### 10.4 BlockSwitchMode 下 stimulus sampling 逻辑也变了

- 普通 training：做 side antibias
- BlockSwitchMode：做 congruent / incongruent antibias

因此 BlockSwitchMode 不是“只换 rule”，而是连 trial 分布都一起换了。

### 10.5 early lick 惩罚逻辑在文件里是“部分显式，部分未连通”

代码里确实定义了：

- `CurrentELPunishEnabled`
- `EarlyLickAborted`
- `TimeOutEL`

但在 `DBRuleSwitch.ino` 这一层，`PreCueDelayPeriod` / `SampleDelay` 没有显式写出把 lick 事件跳到 `EarlyLickAborted` 的转移边。因此更稳妥的理解是：

- 设计意图上：retention 和 BlockSwitchMode 希望启用 early lick punishment
- 但是否真的生效，还取决于 `gpSMART` 底层是否在 `CreateState()` 或别处隐式处理 early lick

如果你愿意，我下一步可以继续帮你把这块做成“代码级核查版”，专门去验证 early lick punishment 在实际运行中到底有没有被真正接上。
