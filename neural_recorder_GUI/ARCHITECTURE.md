# Neural Recorder GUI Architecture

## Package Layout

- `master_app/`
  - 主控台窗口、detail viewer、主控台契约与样式、刷新频率配置加载。
- `services/`
  - 每 cage 后台进程、headless controllers、健康指标、charging guard、恢复逻辑。
- `recorder_app/`
  - 原始 neural recorder GUI 主窗口、7 panel UI、Habits panel。
- `hardware/`
  - neural 串口读写、camera 采集、impedance 相关底层能力。
- `monitoring/`
  - alarm 配置/检查、battery history 分析与历史恢复。
- `storage/`
  - runtime cache 和大 payload/shared memory 读写。
- `support/`
  - 路径解析、Windows 运行时配置等通用支撑能力。
- `workers/`
  - 视频压缩等后台 worker 脚本。
- `widgets/`
  - 可复用的轻量 UI 组件。
- `Cage_Config/`
  - cage profile JSON。
- `Data/`
  - 项目级共享静态数据，例如 charging guard presets。

## Entrypoints

根目录只保留启动入口：

- `master_console.py`
- `detail_window.py`
- `neural_recorder_GUI.py`

其余 Python 实现文件都已经按功能移动到对应子目录。

## Runtime Model

- 主控台：一个 UI 进程。
- 每个 cage：一个独立 `SlotService` 进程。
- 每个 detail chart：一个独立 detail viewer 进程。
- 三个 cage 的命令队列、runtime cache 目录、controller 实例彼此隔离。
