# Neural Recorder GUI 系统检查清单与下一轮确认点

## 本轮已完成的优化

- 5 个 subagent 分别完成了 GUI 功能接线、三 cage 独立性、数据/图表、进程线程划分、综合风险审计，并把问题收口为“低风险立即修复”和“需要硬件/架构验证”的两类。
- Chart 刷新配置已统一为 runtime 可配置的目标 fps，默认 `detail_target_fps=12`，对应 detail publish、payload poll 和 render interval 约 `83ms`；runtime config 里更高目标会被限制在 30fps。
- Camera preview 现在不会在 camera open 失败时假显示为 enabled；失败会进入 `last_error` 和 System Log，前端状态回落到关闭。
- Camera health check 改为先调用 `check_health(max_stale_seconds=4.0)`，只有明确 unhealthy 才带原因执行 recovery，避免无原因反复恢复。
- Impedance test 下发失败现在会进入 `last_error` 和 System Log，不再显示成“requested”假成功。
- Local chart 的 top-level import fallback 已修复，避免直接从 GUI 文件夹启动/导入时出现 `attempted relative import beyond top-level package`。
- Local chart 打开失败会立即把 `detail_attached` 和 neural detail stream 回落为 `False`，并刷新 runtime cache，避免主控台按钮停留在 `Close Charts`。
- Packet counter 的上位机处理已区分真实 `u16` overflow、counter reset 和 out-of-order/stale packet；`curr < prev` 不再一律按 overflow 计算，避免旧包导致 `missing packet count exceeded cap`。
- Firmware 语义已确认：`tx_payload_wraped_num` 是全局 `u16_t` packet counter，mode0/mode3 LFP&ESA 期望 step=1，mode3 raw 因为 4 个 raw chunk 合并发送，期望 step=4。
- Habits start date 的程序化状态同步会 block signal，避免后台刷新触发一次额外的 set date 命令。
- Battery history 写盘改用统一 `atomic_write_json(..., durable=False)`，减少半写文件风险，同时避免高频 fsync。
- Neural serial 空读时加入 `1ms` 让出 CPU，减少无数据时 busy loop 抢占同进程 chart/Qt event loop。
- 旧单窗口 `ESBMainWindow` 的默认 plot render 判断阈值调整为 12fps，和 master/detail 默认目标一致，同时最高目标限制为 30fps。
- `SlotService` 的低频定时器已按 `slot_id + timer_name` 错峰启动，避免 3 个 cage 同时刷新状态、写 cache、做 health check 或 charging guard 轮询。
- `charging guard` 的 10s 检测已移入 camera capture 进程：SlotService 只读取 camera 进程给出的检测结果，不再为了检测或预览主动拉取/解码大帧。
- camera preview 现在直接复用 camera 进程内编码好的 JPEG；打开 show camera 时不会再在 SlotService 中重复 `get_latest_frame -> annotate -> imencode`。
- `charging guard` 的 ROI 框、小鼠检测结果、白天/夜晚自适应阈值状态由 camera 进程绘制进 preview，并通过低频 detection status 回传。
- neural EDF save chunk size 已支持 runtime config 下发，默认从原来的更小 chunk 提升到 `100` packets，减少保存队列的固定小包开销。
- chart 关闭时不再维护高频 GUI buffer；chart 打开后也只消费已经 decode 完成的数据，不改变 packet counter、EDF enqueue、threshold sweep 的优先级。
- runtime cache 的 transient 文件仍是低成本写入；history、threshold、profile 等关键长期数据保持可靠写盘。

## 当前进程与线程模型

- Master Console 主进程：只负责三张 cage 卡片 UI、低频 cache 读取、命令投递和 preview 展示。
- 每个 cage 一个独立 `SlotService` 进程：独立 command queue、独立 profile、独立 runtime cache、独立 controller 状态。
- 每个 `SlotService` 内的 neural 串口为 `SerialPort(QThread)`：负责 serial read、packet decode、packet loss 统计、threshold sample collection、EDF enqueue。
- 每个 neural controller 连接后会启动独立 EDF writer process：负责实际 EDF 写入，优先级高于 chart/cache/UI。
- 每个 camera open 后会启动独立 camera capture process：内部还有 capture thread，负责相机读帧、视频写入、preview JPEG、charging guard 检测。
- RF 在每个 cage 内有独立 worker thread，避免 RF connect/query/recovery 阻塞 Qt 主事件循环。
- Habits 串口仍通过独立 worker/QThread 处理，并通过同一个 per-cage command path 与主控台/chart 同步。
- Chart 现在是对应 cage 进程内本地窗口：保留完整 panel 交互，但取消默认高频 detail IPC/pickle/shared-memory/file polling 链路。

## 优先级原则

- 最高优先级：`serial read -> packet decode -> packet counter/loss -> EDF enqueue -> EDF writer`。
- 高优先级但不得阻塞 neural：Habits 需要 GUI 快速回复的命令、mode switch、trial index/alignment、RF on/off/query 的状态确认。
- 中优先级：camera capture、video recording、camera recovery、charging guard 10s 检测。
- 低优先级：chart render、preview JPEG 发布、runtime cache 美化字段、system log 显示刷新。
- 当系统压力升高时，允许丢 chart frame 或降低 preview 频率，不允许丢 neural packet、跳过 EDF enqueue 或破坏 threshold sweep。

## 功能完备性审计结果

- 本轮静态检查发现 master/detail 生成的 `34` 类 `make_slot_command(...)` 均已在 `SlotService` 中有 handler。
- Chart 中保留的关键远程控制已接入同一 per-cage command path：mode1/mode2/mode3 channel、manual/auto threshold、spike filter、impedance compensation、impedance test、Habits read all/SD download/控制命令。
- Chart 中重复的保存路径和保存进度入口保持隐藏，保存控制集中在 Master Console。
- LFP scale、spectrum window、spike spectrum 等 chart 本地显示型功能保留原 chart 逻辑，不作为跨进程空壳按钮处理。
- 需要硬件确认：spike filter 和 impedance compensation 目前有 GUI/profile/status 链路，但是否应进一步下发到 neural 固件/reader 实时处理，需要按旧 GUI 的真实实验语义确认，避免把“显示配置”误改成“设备控制”。
- 需要硬件确认：所有 Habits 需要 GUI 快速回复的上行命令应继续做串口实测，尤其是 read all values、protocol progress/status information、RF power 查询和 trial index/alignment。
- 仍建议在真实硬件上逐个点击 master card 和 chart 可见按钮，确认每个按钮都有设备响应、状态回写和 system log/error 提示。

## 本轮 subagent 风险汇总

- GUI 功能：主要风险是“控件可点但只是更新状态/profile”的半迁移状态，已修复 camera preview 和 impedance test 假成功；spike filter、impedance RC 参数仍建议用硬件或旧 GUI 语义确认。
- 三 cage 独立性：每个 cage 仍是独立 `SlotService` 进程、独立 command queue、独立 runtime cache、独立 serial/camera/EDF 资源；但 Master 的 camera catalog refresh 和 alarm email 仍可能冻结主控 UI，不会冻结 SlotService。
- 数据与 chart：默认刷新链路已统一为 12fps，最高目标限制为 30fps；chart 慢应优先提高显示下采样或刷新 interval，不应影响 serial/EDF/threshold。若仍出现规律丢包，下一步应优先 benchmark serial reader fast path 与 EDF queue put。
- 进程线程：video recording 已在 camera process 中，serial read 在 neural QThread，EDF writer 在独立 process；仍需关注 SlotService 同进程 chart render 和 Python GIL 抢占。
- 综合风险：System Log 需要继续保持聚合限频；packet gap/read gap 不应刷屏，否则日志自身会成为性能压力源。

## 你之前重点关心的检查点

- 三个 cage 必须像三个终端一样独立：不能共享 command bus、串口、camera、EDF writer 或全局排队。
- 打开/关闭 chart 不能造成规律性丢包；如果出现丢包，应优先查看 `serial_read_gap_ms`、packet gap、save enqueue、writer lag，而不是只看 payload slow。
- EDF 保存中 neural 通道必须保持 firmware 原始 index，不为了 GUI 显示做重排；GUI 控制层可以做 mode1/mode3 物理通道映射。
- mode3 threshold cache 必须按 mouse 保存，且按 `rhd2132_channel_index` 保存；manual threshold 和 auto threshold sweep 成功下发后要更新 JSON 和 EDF annotation。
- packet loss 必须按下位机 packet counter 自增值计算，并处理 overflow 回到 0；mode0/mode3 LFP&ESA step 为 1，mode3 raw step 为 4。
- `curr < prev` 只有在 `prev` 靠近 65535 且 `curr` 靠近 0 时才视为真实 overflow；普通小幅回退视为延迟/乱序旧包并忽略，不计入 missing；明显回到低 counter 视为设备/采样 counter reset。
- Current mode 不应被单个异常包带着跳，应基于连续多个 packet id/window 判断 idle/mode0/mode1/mode2/mode3。
- serial buffer/backlog、decode slow、writer lag、packet gap 要进入 System Log，且不能因为写 log 本身拖慢采集。
- Save Start/Stop 要同步触发 video recording start/stop 和 EDF flush/final save，文件按日期目录保存。
- battery percent 使用下位机上报的 RSOC，不由 GUI 根据电压估算；电压仅显示换算后的 V/mV。
- 电量低于 10% 后的 trial trigger pause 逻辑应按 protocol 11/free-water 方案执行，恢复后回到原 protocol，并保存到 trial/event/params 记录中。
- charging guard 必须 10s 一次自动检查，不依赖 show camera；需要显示 hit required 进度、小鼠是否检测到、charging/fault 状态、day/night 阈值。
- RF recovery 应区分 soft timeout、port_missing、device_unresponsive、reconnecting、failed，并对手动操作保留保护窗。
- Habits 的 read all values、protocol progress、status information、cap/history/cache 更新要同步到 Master Console 和 chart Habits panel。
- Habits 下位机需要上位机快速回复的命令必须优先处理，不能被 chart/cache/camera 刷新阻塞。
- SD download、impedance test/history、refresh cap baseline、Mode3 reref off、IMU mode、sample mode 直接切换都需要保持旧 GUI 等价行为。
- 所有用户可编辑控件要和状态显示分离，后台刷新不能覆盖用户正在选择的 combo/spinbox。
- Master Console 状态颜色规则：ON/正常为绿，OFF/异常为红；loss > 1%、battery < 10%、RSSI 绝对值 > 70 应显示红色。
- System Log 应保留最近 1 天关键事件，普通高频 cap update 不刷 log，并提供 clear 按钮。
- GUI 启动时不能自动 sampling，也不应加载过大的会话级 log/cache 导致启动慢。

## 下一轮真实硬件验证建议

- 单 cage mode0 连续保存 30 分钟，chart off/on 各一次，比较 packet gap、EDF MissingPackets、writer lag 和 System Log。
- 单 cage mode3 LFP&ESA + raw + camera recording + charging guard enabled，观察是否还有规律性丢包。
- 三 cage 同时 connect/sampling/save/camera/chart，分别操作其中一个 cage，确认另外两个 cage 的 status、loss、writer lag 不受影响。
- 执行 full auto threshold sweep，确认 16 个通道逐个采样、计算、下发、写入 per-mouse threshold JSON，并进入 mode3 raw/LFP&ESA EDF annotation。
- 逐个测试 master 与 chart 中所有可见控制：Neural/RF/Habits/Camera/IMU/Sample/Save/Threshold/Impedance/SD download/Charging Guard。
- 人为断开 RF、Habits、camera，确认错误原因进入 System Log，重新连接后 last error 会清除，按钮状态恢复。
- 低电量模拟：RSOC < 10% 时 10s 内进入 protocol 11/free-water pause，RSOC > 20% 后恢复原 protocol。
- Windows 10 长跑：三 cage 全开 6-12 小时，重点记录 GUI 响应、serial_read_gap、writer_lag、camera recovery、runtime cache 写入耗时。

## 本轮测试结果

- `QT_QPA_PLATFORM=offscreen .venv-master-console/bin/python -m pytest tests -q`
- 当前结果：`218 passed`
