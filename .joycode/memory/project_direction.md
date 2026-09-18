---
name: sysmonitor 性能工具的项目目标与实施决策
description: 用户确认跨平台 PC 分析 Android sysmonitor 日志，先离线分析再 ADB 和实时，上传与 AI 后置。
type: project
---

目标是统一 Linux、Windows、macOS 上的操作体验，通过 ADB 控制 Android sysmonitor perf_test，支持离线导入、性能分析和离线 HTML 报告。
**Why:** 用户提供了手写需求及 2026-09-16 的 sysmonitor perf_test v1 协议，并于同日要求记住方案并开始开发；监控目标是 Android，不是 PC 本机。
**How to apply:** 采用 Python/FastAPI、React/ECharts、SQLite 的本地工具方案，分平台打包。先验证日志解析、离线分析和报告，再接 ADB 启停导出、实时轮转追踪及三端交付。上传目的地、AI/Skill 边界和报告编辑器未确定，不纳入首版。
协议约束：S 实际 11 列（标题 10 列是笔误）；P/DE/DP 名称可能含空格；I/O 等字段是周期增量；dmabuf 每约 10 秒才有一次，缺测不能补零。
分析需明确：I/O 零值可能由权限造成；等待指标有能力依赖；进程号复用无法由当前协议精确识别；RSS 前 20 名 GPU 回填影响趋势，RSS 与 dmabuf 不能直接相加。
可靠性要求：保留解析异常和数据缺口，处理轮转、半行、重启与时钟跳变；停止后导出优先，实时完整性需要真机验证。PC 定时停止在断连时不保证生效。
开发的参考估算为单人 17–25 个工作日，依赖真实日志、Android 设备和三端验证环境；此估算不是进度承诺。