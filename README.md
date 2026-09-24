# SysMonitor 工程文档

面向 Windows、macOS、Linux PC 的 Android 性能日志分析工具。支持 sysmonitor 日志和 Top 文本导入、系统与进程分析、进程分组、两次采集对比、离线交互报告，以及通过 ADB 控制采集。数据保存在本机，不需要云端服务。

## 1. 环境与快速启动

建议使用 Python 3.12、Node.js 24，与当前持续集成环境一致；这不是最低兼容版本声明。离线日志分析不需要 ADB；连接设备时另行安装 Android Platform Tools。

在工程根目录执行，以下激活命令适用于 macOS/Linux：

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
python main.py --open-browser
```

Windows PowerShell 使用 `python -m venv venv` 创建环境，再执行 `venv\Scripts\Activate.ps1`；其余命令相同。若已有虚拟环境，直接激活，无需重建。

浏览器访问 http://127.0.0.1:8765 。后端直接托管构建后的前端；未构建时首页返回 503。

启动入口为 [`main.py`](main.py)，依赖见 [`requirements.txt`](requirements.txt) 和 [`package.json`](frontend/package.json)。

| 启动参数 | 默认值与约束 |
| --- | --- |
| 端口 | 默认 8765；使用 `--port 8766` 修改，允许 1024–65535 |
| 数据库 | 默认用户主目录下 `.sysmonitor/sessions.sqlite3`；使用 `--database ./data/sessions.sqlite3` 指定 |
| 开发模式 | `--dev` 允许本机 5173 开发代理，不启用 Python 热重载 |
| 打开浏览器 | `--open-browser` 等待服务启动成功后打开后端地址 |
| ADB 路径 | `--adb` 接受已存在的绝对文件路径；实际调用仍需具备执行权限 |

### 前后端分离开发

激活 Python 环境后，在两个终端分别执行：

```sh
python main.py --dev
```

```sh
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

访问 http://127.0.0.1:5173 。[`vite.config.ts`](frontend/vite.config.ts) 的接口代理固定指向 8765；变更后端端口时必须同步调整。不要将本地服务或开发服务器暴露到公网。

## 2. 模块架构

| 模块 | 责任 |
| --- | --- |
| [`main.py`](main.py) | 参数解析、本地 Uvicorn 启动、浏览器启动 |
| [`perf_api.py`](perf_api.py) | HTTP 接口、请求校验、本机安全边界、流式任务编排 |
| [`perf_store.py`](perf_store.py) | SQLite 会话与样本、导入去重、趋势查询和进程排行 |
| [`perf_parser.py`](perf_parser.py) | sysmonitor 行解析、字段类型校验与轮转排序 |
| [`perf_top_parser.py`](perf_top_parser.py) | Top 文本解析、时间处理与标准化 |
| [`perf_metrics.py`](perf_metrics.py) | 单位、统计口径、CPU 平台参照识别 |
| [`perf_groups.py`](perf_groups.py) | 进程组、人工配置和资源分析 |
| [`perf_compare.py`](perf_compare.py) | 基于原始样本的跨会话比较 |
| [`perf_report_data.py`](perf_report_data.py) | 报告原始样本聚合与趋势数据 |
| [`perf_report_ui.py`](perf_report_ui.py) | 离线交互 HTML 报告渲染 |
| [`perf_report.py`](perf_report.py) | 报告生成、缓存版本、并发合并与取消 |
| [`perf_adb.py`](perf_adb.py) | 设备选择、sysmonitor 启停、日志拉取和清理 |
| [`perf_top_capture.py`](perf_top_capture.py) | 无 root Top 采集、本地归档、显式导入 |
| [`App.tsx`](frontend/src/App.tsx)、[`Chart.tsx`](frontend/src/Chart.tsx) | React 页面组织、ECharts 图表 |
| [`report.js`](report_assets/report.js)、[`report.css`](report_assets/report.css) | 离线报告交互与样式 |
| [`build.py`](packaging/build.py)、[`verify_build.py`](packaging/verify_build.py) | 原生平台构建与发布产物验证 |

主数据流：日志文件或设备采集 → 来源识别与解析 → 会话、时段、周期归档 → 原始样本统计与趋势降采样 → Web 页面、比较结果或离线报告。

数据库采用 SQLite WAL，连接启用外键与 30 秒忙等待。会话、原始记录、进程组、报告设置和计算缓存持久化保存。原始记录保留来源文件、行号、时间戳、进程身份、时段与周期等定位信息。

## 3. 日志导入与分析流程

1. 选择 1–5 个日志文件，可填写不超过 120 字符的会话名。单文件不超过 1 GiB，总请求上限为 5 GiB 加 1 MiB 表单开销。
2. 同一次导入只接受一种来源，不能混合 sysmonitor 与 Top。不同来源可分别导入后比较。
3. sysmonitor 轮转日志按旧到新排序，即编号 4、3、2、1、无编号；其他名称按文件名排序。
4. 按内容 SHA-256 去除同批重复文件；已存在相同会话指纹时返回原会话，不重复建库。
5. 解析、分段和批量入库在事务中完成。重启标识变化或时间倒退形成新时段；采样时间变化或新的 Top 样本形成新周期。
6. 查看系统概览、进程排行和明细，按时段定位；需要业务维度时配置进程组，再查看资源分析。
7. 比较两次不同采集时，先确认场景、时长、采样间隔与选定时段是否一致。
8. 导出离线 HTML 可独立打开；报告按日志原始数据生成，不包含人工配置和进程组。

导入会记录错误行、未知行、缺少系统样本的周期及事件；问题与事件各最多保留 100 项明细，不应把明细条数当作全部异常数。sysmonitor 单行上限 1 MiB；末尾无换行的残行跳过，非法数值和非有限数值不作为有效样本。

### 日志记录类型

字段定义以 [`perf_parser.py`](perf_parser.py) 为准；设备原始格式说明见 [`perf_log_format_README.txt`](frontend/src/sysmonitor_test/perf_log_format_README.txt)。

| 类型 | 含义 |
| --- | --- |
| S | 系统 CPU、可用/总计/已用内存、重启标识 |
| P | 进程 CPU、调度、RSS、缺页、物理/逻辑 I/O、进程名 |
| D | 系统 dmabuf 总量和对象数 |
| DE | 按 exporter 聚合的 dmabuf |
| DP | 按进程统计的 dmabuf |

Top 有 UTC 标记时按 UTC 解析；没有时区的旧文本按导入机器本地时区解析，包含夏令时影响。跨机器导入无时区日志时，应核对时间显示。

## 4. 数据口径

### 4.1 统计与趋势

- 统计使用当前范围内全部有效原始样本，缺失不补零；均值按样本计算，不按时间加权。
- P95/P99 使用最近秩：排序后取第 ceil(p × N) 个样本，从 1 开始，不插值。不可平均各段百分位代替全范围统计。
- 图表缩放、降采样不改变统计值。P95/P99 参考线可切换，是样本分布参考而非性能准入阈值，也不作用于叠加参照曲线。
- 同名进程聚合按同段、同周期先合计，再统计；不能把多个 PID 的百分位直接相加。
- 不同曲线使用各自时间与周期，不按独立采样数组下标强行对齐。缺样、PID/时段变化、周期跳跃及时间不递增按连续性规则断线，不补零连接。
- 累计仅用于周期增量；缺失覆盖和浮点舍入会影响累计值。全会话时长为各时段跨度之和，不包含段间间隔。

### 4.2 CPU 与 KDMIPS

**系统概览、系统统计和会话比较的整机 CPU 满载始终为 100%。进程 CPU 使用单核算力口径，可超过 100%。** 两者不能直接相加或混用。

只有进程趋势叠加的整机参照按单核口径展示，识别逻辑见 [`perf_metrics.py`](perf_metrics.py)：

1. 优先采用有效正数的明确 CPU 满载容量；800% 标记为 8255，700% 标记为 8295，其他容量保留但不强行命名平台。
2. 没有明确容量时，逐系统样本按总内存推断：大于 0 且小于 16384 MB 对应 800%；18432–22528 MB（含边界）对应 700%；其他范围未知。这是特定设备启发式规则，不是通用的内存与核数映射。
3. Top 参照直接使用原始单核占用，缺失时不回退。sysmonitor 使用整机百分比乘容量再除以 100；无法识别容量时留空，不默认乘 8。
4. 不额外累加 com.desa 等进程，避免重复计算整机负载。参照说明根据全范围样本生成，不仅依据降采样点。

离线报告固定按单核 CPU 百分比 ÷ 100 × 28.75 估算 KDMIPS；这是固定系数派生值，并非实测。进程组资源页则要求显式填写平台和正有限单核系数，未配置不能套用默认值。原始日志的 DMIPS 字段与上述派生 KDMIPS 不等同。

### 4.3 内存与 I/O

- sysmonitor 系统已用内存按总内存减 MemAvailable；Top 使用其原始 used。二者的缓存口径不同。
- 系统剩余内存逐样本按总内存减已用内存计算，再统计均值、分位数和峰值；不能用聚合后的两个统计值相减，也不能统一称为 Linux 空闲内存。
- 跨来源仍展示系统、进程内存数值差异和进程排行，但差异只能参考，不能直接断定退化。sysmonitor RSS 前 20 进程可能包含 GPU 回填，Top RES 不含该回填。
- RSS 不能与 dmabuf 简单相加。DP 在单进程内去重，跨进程可能共享，进程合计不必等于系统 D。
- 物理读写与逻辑读写是 **KB/周期增量，不是 KB/s**；累计为有效增量之和。比较速率需要结合采样间隔，不能直接更换单位标签。
- 系统 I/O 参照仅按周期汇总已采集进程，不能代表全部进程或整机磁盘吞吐。逻辑 I/O 可能命中缓存，物理 I/O 零值也可能受权限限制。

### 4.4 两次采集比较

差值为对比采集减基准采集；相对变化以基准绝对值为分母，基准为零时不计算。CPU 差值单位为百分点。进程按原始完整名称精确匹配，而非 PID 或路径简称；单侧未观察到不代表进程新增或退出。红色增加、绿色减少不自动代表性能变差或变好。

## 5. 设备采集

### sysmonitor / ADB

- ADB 查找顺序：显式指定路径、环境变量、系统 PATH。未捆绑宿主 ADB，必须自行安装并完成设备授权。
- 设备需要 root adbd 或可执行 su 0；工具不会自动执行 adb root。随工程提供的采集程序仅适用于 Android ARM64。
- 在设备页选择明确序列号，检查状态后确认启动；已有采集运行时拒绝重复启动。设备采集目录为 `/data/local/tmp/sysmonitor_test`，轮转日志位于 `/log/sys/perf/`。
- 停止后确认设备状态，再拉取并导入日志。删除日志是独立且不可轻率执行的操作；应先保留需要的归档。
- 采集属性是设备级共享状态，会影响其他使用同一 sysmonitor 的工具。ADB 超时不保证操作未生效，应先刷新状态，避免盲目重试。
- 关闭本机服务不等于停止 Android 上的 sysmonitor 采集。

### Top

无需 root，但依赖设备提供兼容 Top 输出。选择设备后明确确认启动；采样次数默认无限，或 1–100000 次，间隔 1–3600 秒。采集完成或停止后，再显式导入，不会每次采样自动创建会话。

单样本上限 16 MiB，归档上限 1 GiB。采集异常保留已完整写入的样本；正在停止或导入时拒绝冲突操作。导入可携带采集 ID，避免旧页面误导入新任务。服务正常关闭时会等待 Top 线程退出及导入收尾。

设备拉取和 Top 原始归档分别保存在数据库同级的 `adb_logs/`、`top_logs/`。归档含设备与进程信息，应按内部数据规范保存和分享。

## 6. HTTP 接口

接口实现以 [`perf_api.py`](perf_api.py) 为准。除获取令牌外，所有 API 请求都需携带 X-Session-Token 请求头；令牌由 GET /api/token 返回，每次服务启动重新生成。以下 SID 表示会话 ID，GID 表示进程组 ID，调用时替换为实际值。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/token | 获取本次服务令牌 |
| GET | /api/sessions | 会话列表 |
| POST | /api/import | multipart 导入，仅接受 files 与 title |
| GET / DELETE | /api/sessions/SID | 概览 / 删除会话 |
| GET | /api/sessions/SID/series | 趋势；类型、PID、时段、名称过滤；默认 1500 点，64–5000 |
| GET | /api/sessions/SID/statistics | 同范围全部有效原始样本统计 |
| GET | /api/sessions/SID/process-references | 进程图整机参照；必须指定时段 |
| GET | /api/sessions/SID/processes | 排行、搜索、分页、同名合并；默认 100 条，最多 500 |
| GET / POST | /api/sessions/SID/groups | 列表 / 创建进程组 |
| GET / PUT / DELETE | /api/sessions/SID/groups/GID | 查询 / 更新 / 删除进程组 |
| GET | /api/sessions/SID/groups/GID/analysis | 组趋势；P 或 DP，默认 600 点 |
| GET | /api/sessions/SID/groups/GID/resources | 资源统计 |
| GET / PUT | /api/sessions/SID/report-settings | 读取 / 更新人工设置 |
| GET | /api/sessions/SID/report | 下载离线 HTML |
| GET | /api/sessions/SID/report/stream | 带进度生成离线报告 |
| GET | /api/compare、/api/compare/stream | 同步比较 / 带进度比较 |
| GET | /api/adb/devices、/api/adb/status | 设备列表 / 指定设备状态 |
| POST | /api/adb/start、/api/adb/stop | sysmonitor 启停 |
| POST | /api/adb/delete-logs、/api/adb/pull | 删除设备日志 / 拉取并导入 |
| GET | /api/top/status、/api/top/archive | Top 状态 / 原始归档 |
| POST | /api/top/start、/api/top/stop、/api/top/import | Top 启停 / 显式导入 |

比较请求必须提供 baseline、target，可选 baseline_segment、target_segment。进程组需要名称、时段和 1–50 个成员；成员以 PID 与原始名称定位，可填写业务属性。时间范围采用原始毫秒时间戳。Top 变更请求必须显式传布尔确认；导入支持 capture_id，停止请求不支持此参数。具体请求结构见 [`perf_api.py`](perf_api.py)、[`perf_api.py`](perf_api.py) 和 [`perf_api.py`](perf_api.py)。

### 流式协议与缓存

报告和比较的进度接口使用 **NDJSON，不是 SSE**，响应类型为 application/x-ndjson。逐行解析 JSON，不能假定每个网络数据块就是完整消息。消息类型为进度、心跳、结果或错误；结果载荷在 data，错误文本在 message。

```json
{"type":"heartbeat"}
```

报告与比较流式任务分别最多占用两个计算槽；槽位满时可在流内返回错误，因此 HTTP 成功不代表计算成功。后台线程执行计算，约每 0.25 秒发送心跳；客户端断开触发取消，等待线程退出，不发布半成品报告。前端流式消费实现见 [`api.ts`](frontend/src/api.ts)。

报告缓存按版本与资源哈希失效，持久化压缩保存；损坏时重建，同范围并发请求通过分片锁合并。修改统计语义时应评估缓存版本升级，不能仅依赖静态资源哈希变化。当前版本定义见 [`perf_report.py`](perf_report.py)。

### 常见状态码

| 状态 | 含义与处理 |
| --- | --- |
| 400 / 422 | 输入内容或参数校验失败，检查日志来源、范围与请求结构 |
| 403 | 本机边界或令牌检查失败，重新获取令牌并确认访问地址 |
| 404 | 会话、组或接口不存在，刷新列表 |
| 409 | 导入、设备或采集状态冲突，查询状态后重试 |
| 413 | 日志、请求或归档超限，缩小输入规模 |
| 503 | 数据库忙、磁盘/设备不可用，或前端未构建；根据响应内容处理 |

## 7. 安全与数据维护

- 后端仅绑定 127.0.0.1，严格检查 Host、Origin 和浏览器来源标记；开发模式只额外允许指定开发端口。不支持直接改成公网多用户服务。
- API 在解析和落盘前进行本机边界与令牌检查。令牌不是用户登录系统，也无法抵御已能控制本机的进程。
- 默认关闭 Swagger、ReDoc 与 OpenAPI；响应设置禁止缓存、类型嗅探保护、禁止嵌入和不发送来源信息。离线报告使用受限内容安全策略。
- 宿主 ADB 通过参数数组执行，不经宿主 shell 拼接，并明确指定设备序列号；设备端操作仍有 root 和共享配置风险。
- SQLite 与原始归档都位于本机磁盘，不等于加密存储。导出报告可能包含进程名、业务信息及设备资源数据，分享前应审查。
- 备份前正常退出服务，再复制数据库及需要的归档。运行中不要只复制主数据库文件而遗漏 WAL；需要在线备份时使用 SQLite 备份机制。
- 删除会话不应视为清理所有本地或设备原始日志；归档与设备日志需分别确认和管理。

## 8. 开发验证与测试

激活 Python 环境后，在工程根目录运行：

```sh
python -m unittest discover
npm --prefix frontend run build
npm --prefix frontend run lint
node --check report_assets/report.js
git diff --check
```

后端测试覆盖解析、统计、存储、报告、API、安全边界、ADB、Top 和部分打包兼容性逻辑。设备相关单元测试不能替代真机验证，打包单元测试也不能替代实际安装包验证。

持续集成定义见 [`package.yml`](.github/workflows/package.yml)。当前使用 Python 3.12、Node.js 24，发布任务可手动触发或由 v 开头标签触发。注意：CI 后端测试命令目前只执行主分析、ADB 和打包三个模块，未覆盖两个 Top 测试模块；本地提交前建议使用上述完整发现命令。

## 9. 构建与发布

必须在目标操作系统原生构建，不是交叉编译。先完成依赖安装和前面的开发验证，再执行以下示例；示例构建父目录应专用于本次构建，发布目录必须尚不存在。

```sh
python -m pip install "pyinstaller>=6.11,<7" "Pillow>=10.4,<12"
python packaging/build.py --output-dir release-doc-example
python packaging/verify_build.py --output-dir release-doc-example --publish-dir publish-doc-example
```

[`build.py`](packaging/build.py) 默认先执行前端依赖安装、构建与 lint；只有确认已有构建产物与源码一致时，才使用 `--skip-frontend`。默认输出父目录为 release，当前产品版本为 1.0.0，子目录名含平台、架构和时间戳，并生成 SHA256SUMS。

| 平台 | 产物与条件 |
| --- | --- |
| macOS | 原生应用和 DMG；最低系统版本 14.0，验证 Mach-O 架构及系统兼容性 |
| Windows | 便携 ZIP；找到 Inno Setup 的 ISCC 时额外生成安装 EXE，否则仅便携包及安装脚本 |
| Linux | tar.gz；找到 dpkg-deb 时额外生成 deb |

PyInstaller 使用目录分发模式，包含前端构建、Android ARM64 采集程序、离线报告资源和图标，但不包含宿主 ADB。发布时保留 ECharts 第三方声明：[`ECHARTS-LICENSE`](report_assets/vendor/ECHARTS-LICENSE)、[`ECHARTS-NOTICE`](report_assets/vendor/ECHARTS-NOTICE)。

[`verify_build.py`](packaging/verify_build.py) 要求输出父目录下恰好存在一份子目录校验清单，并严格要求完整产物组合：Windows ZIP 与安装 EXE、Linux tar.gz 与 deb、macOS DMG。因此只有便携包的构建不能通过完整发布验证。

验证包括校验和、资源完整性、解包启动、首页和静态资源、API 令牌边界；macOS 额外挂载镜像并检查架构和应用元数据。通过后复制产物与校验清单到新的发布目录。不要通过移除校验步骤来绕过缺失的安装器或兼容性问题。

## 10. 常见问题与当前限制

| 现象 | 排查方向 |
| --- | --- |
| 首页返回前端未构建 | 执行前端构建，再重启后端；确认在工程根目录启动 |
| 开发页面接口失败 | 确认后端启用开发模式，前端端口为 5173，代理目标与后端端口一致 |
| 服务重启后请求被拒绝 | 旧令牌已失效，刷新页面重新获取；不要混用不同主机名或端口 |
| 导入后没有新会话 | 检查是否命中内容去重，确认返回的原会话 ID |
| 曲线断线或统计样本数不足 | 检查缺样、重启、时间倒退、PID 变化及解析问题，不能用补零掩盖 |
| 整机参照缺失 | 检查明确 CPU 容量、内存识别范围和 Top 原始 CPU 样本；未知平台不会默认乘 8 |
| I/O 累计不可比 | 检查采样间隔、有效覆盖与时长；不能将周期增量误作每秒速率 |
| ADB 操作超时 | 先检查设备实际状态和授权；超时不保证操作没有生效 |
| Top 完成但列表没有会话 | 采集与导入是独立操作，完成或停止后需要显式导入 |
| 完整发布验证失败 | 检查安装器工具、完整产物组合、唯一构建清单及发布目录是否已存在 |

当前已知限制：平台自动识别只覆盖特定内存区间；跨来源内存差异不能直接判定退化；离线报告不带人工进程组和设置；大日志的计算与缓存依赖本机磁盘和资源。前端主包存在超过 500 KB 的体积警告，属于后续性能优化项，不是构建失败。

本次文档编写验证结果：前端构建通过、lint 零错误和警告、后端完整发现测试 160 项通过；启动与两份打包脚本的帮助命令均可运行。本次未重新生成或安装跨平台发布包，也未执行真实 Android 设备采集验收。

