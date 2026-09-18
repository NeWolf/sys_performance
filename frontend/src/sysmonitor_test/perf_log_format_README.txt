========================================================================
sysmonitor perf_test 落盘数据格式说明 (v1)
面向: 解析工具开发者
更新: 2026-09-16
========================================================================

一、概述
------------------------------------------------------------------------
sysmonitor 在 perf_test 模式下, 每个采样周期把系统与全部进程的性能数据
以 CSV 行写入独立文件, 供离线解析。

  文件路径 : /log/sys/perf/perf.log        (当前文件)
             /log/sys/perf/perf.1.log ~ perf.4.log   (轮转历史, 数字越大越旧)
  轮转策略 : 单文件 50MB, 最多保留 5 个 (共 250MB), 满了覆盖最旧
  编码     : ASCII / UTF-8, 每行以 '\n' 结尾
  格式     : CSV, 逗号分隔; 每行第 1 个字段是"行类型"前缀, 决定后续字段含义

导出到 PC:
  adb pull /log/sys/perf ./perf

------------------------------------------------------------------------
二、如何开启 / 关闭 perf_test
------------------------------------------------------------------------
所有开关是 Android 属性, 运行时动态生效 (不需重启 sysmonitor, 下个采样周期即生效):

  # 开启性能采集
  adb shell setprop persist.sm.perf.test 1

  # 关闭
  adb shell setprop persist.sm.perf.test 0

相关可选开关:
  persist.sm.perf.interval  采样间隔(秒), 默认 1        (仅 perf_test=1 时用)
  persist.sm.perf.tofile    是否写文件, 默认 1 (0=不写文件)
  persist.sm.perf.tologcat  是否同时打 logcat, 默认 1 (0=只写文件, 不刷屏)
  persist.sm.perf.async     spdlog 异步写, 默认 0(同步)   *启动/首次开启时定死,
                            运行中改不生效

注: tofile/tologcat 可各自独立开关。若只要文件、不要 logcat, 设 tologcat=0。

------------------------------------------------------------------------
三、行类型总览
------------------------------------------------------------------------
每个采样周期(interval 秒)产生一组行, 顺序大致如下:

  S   行  x1       整机 CPU + 内存 汇总
  P   行  xN       每个进程一行 (N ≈ 当前进程数, 全量不截断)
  D   行  x1       系统 dmabuf 总量           (每 10 秒一次, 非每周期)
  DE  行  x(exporter数)  各 dmabuf exporter 分组   (随 D 行一起)
  DP  行  x(进程数)      各进程持有的 dmabuf      (随 D 行一起)

关联方式: 同一周期的所有行, 第 2 个字段都是相同的 ts(本周期统一时间戳,
UTC 毫秒)。按 ts 分组即可还原"某一时刻的完整快照"。
注意: D/DE/DP 每 10 秒才出一次, 其 ts 会与当刻的 S/P 行 ts 对齐。

======================================================================
四、各行字段定义
======================================================================

------------------------------------------------------------------------
[S] 整机汇总行   共 10 字段
------------------------------------------------------------------------
S,ts,boot,cpu_total,cpu_user,cpu_sys,cpu_iow,cpu_irq,mem_avail_mb,mem_total_mb,mem_used_mb

 #  字段          类型     含义
 1  "S"           str      行类型
 2  ts            int64    本周期时间戳, UTC 毫秒
 3  boot          int      开机计数(第几次开机, 跨重启累加)
 4  cpu_total     float    整机 CPU 总占用%  (满8核=100%, 已÷核数)
 5  cpu_user      float    用户态占用%
 6  cpu_sys       float    内核态占用%
 7  cpu_iow       float    iowait 占用%
 8  cpu_irq       float    irq+softirq 占用%
 9  mem_avail_mb  int      MemAvailable, MB  (真实可用, 判内存够不够看这个)
10  mem_total_mb  int      MemTotal, MB
11  mem_used_mb   int      = total - available, MB (含不可回收部分; cache 不算已用)

示例:
S,946684800123,7,42.04,25.09,14.17,0.00,3.25,9326,14725,5399

------------------------------------------------------------------------
[P] 进程行   共 21 字段   (每进程一行, 全量)
------------------------------------------------------------------------
P,ts,pid,cpu,cpu1c,dmips,wait,pol,cpu_core,state,nthr,rss_kb,rss_delta_kb,majflt,rd_kb,wr_kb,rchar_kb,wchar_kb,syscr,syscw,name

 #  字段          类型     含义
 1  "P"           str      行类型
 2  ts            int64    本周期时间戳(同 S 行, 用于关联)
 3  pid           int      进程 ID
 4  cpu           float    CPU 占用%  整机口径 (满8核=100%)
 5  cpu1c         float    CPU 占用%  单核口径 (= cpu × 核数, 满1核=100%, 同 top 默认)
 6  dmips         float    DMIPS 加权占用% (按大小核算力折算; 同构SoC≈cpu)
 7  wait          float    调度等待% (在运行队列排队等CPU的时间占比; 需 sched_detail)
 8  pol           str      调度策略: CFS/FF(FIFO)/RR/B(BATCH)/IDLE/DL/?
 9  cpu_core      int      最后运行在哪个核 (/proc/pid/stat field39); -1=未知
10  state         char     进程状态: R运行 S睡眠 D不可中断 Z僵尸 T停止 等
11  nthr          int      线程数
12  rss_kb        long     常驻内存 KB (对RSS前20进程含GPU图形内存回填, 见附录)
13  rss_delta_kb  long     本周期 RSS 变化 KB (带符号, +增 -减)
14  majflt        ulong    主缺页增量(本周期) —— 从磁盘取页次数, IO代理信号
15  rd_kb         uint64   物理读盘 KB (本周期) —— /proc/pid/io read_bytes
16  wr_kb         uint64   物理写盘 KB (本周期) —— write_bytes
17  rchar_kb      uint64   逻辑读 KB (本周期) —— rchar, 含命中缓存的读
18  wchar_kb      uint64   逻辑写 KB (本周期) —— wchar
19  syscr         uint64   读系统调用次数(本周期)
20  syscw         uint64   写系统调用次数(本周期)
21  name          str      进程名: 用户态=cmdline全名; 内核线程=[comm]

  ★ 权限说明: 字段 15-20 (rd/wr/rchar/wchar/syscr/syscw) 读 /proc/pid/io,
    需 CAP_SYS_PTRACE。若 sysmonitor 无此权限, 这些字段对多数进程为 0;
    此时用字段 14 majflt 作为 IO 代理。字段 4-13 无需特殊权限, 始终有效。

示例(有权限):
P,946684800123,5589,14.54,116.30,14.54,0.52,CFS,2,S,45,247120,4292,0,0,156,469,184,246,585,com.desaysv.engmode

------------------------------------------------------------------------
[D] dmabuf 系统总量行   共 4 字段   (每10秒一次)
------------------------------------------------------------------------
D,ts,total_mb,total_objs

 #  字段        类型    含义
 1  "D"         str     行类型
 2  ts          int64   时间戳
 3  total_mb    float   系统 dmabuf 总占用, MB (来自 /sys/kernel/debug/dma_buf/bufinfo)
 4  total_objs  int     系统 dmabuf 对象(块)总数

示例:
D,946684800123,2349.90,2939

------------------------------------------------------------------------
[DE] dmabuf exporter 分组行   共 5 字段   (每个 exporter 一行, 随 D)
------------------------------------------------------------------------
DE,ts,exporter,size_mb,objs

 #  字段       类型    含义
 1  "DE"       str     行类型
 2  ts         int64   时间戳
 3  exporter   str     导出者名 (原名中的逗号已被替换为空格, 如 "qcom system")
                       常见: qcom_hgsl(GPU) / qcom system / system / msm_hab(跨VM)
 4  size_mb    float   该 exporter 的 dmabuf 总大小, MB
 5  objs       int     该 exporter 的对象数

示例:
DE,946684800123,qcom_hgsl,1201.10,2394
DE,946684800123,qcom system,1082.20,412

------------------------------------------------------------------------
[DP] dmabuf 每进程行   共 6 字段   (每持有dmabuf的进程一行, 随 D)
------------------------------------------------------------------------
DP,ts,pid,size_kb,objs,name

 #  字段      类型     含义
 1  "DP"      str      行类型
 2  ts        int64    时间戳
 3  pid       int      进程 ID
 4  size_kb   uint64   该进程持有的 dmabuf 大小 KB (同进程内已按inode去重)
 5  objs      int      该进程持有的 dmabuf 块数(去重后)
 6  name      str      进程名(逗号已替换为空格)

示例:
DP,946684800123,1361,413664,78,/system/bin/surfaceflinger

======================================================================
五、解析注意事项
======================================================================
1. 按第1字段(行类型)分派解析: S/P/D/DE/DP。未知前缀应跳过(向前兼容)。
2. 用第2字段 ts 分组还原同一周期快照。ts 是 UTC 毫秒。
3. 名字字段(P.name / DE.exporter / DP.name)中原有的逗号已被替换为空格,
   所以逗号切分是安全的; 但 name 内部可能含空格, 解析时 name 取"最后一个
   字段到行尾"(即前 N-1 个逗号切分, 剩余全部归 name)。
4. CPU 口径: P.cpu 是整机口径(÷核数), P.cpu1c 是单核口径(同 top 默认)。
   和 top 对比用 cpu1c; 看整机负载用 cpu 或 S.cpu_total。
5. 增量语义: majflt/rd/wr/rchar/wchar/syscr/syscw/rss_delta 都是"本周期增量",
   不是累计值。周期长度 = persist.sm.perf.interval 秒。
6. D/DE/DP 频率(每10秒)与 S/P(每周期)不同, 解析时不要假设每个 ts 都有 dmabuf 行。
7. dmabuf 总量(D/DE, 来自bufinfo全局)与每进程之和(DP)对不上是正常的:
   大量 dmabuf(尤其GPU qcom_hgsl)由驱动内部持有, 无进程fd, DP统计不到。
   判系统总量看 D/DE, 判某进程泄漏看 DP 趋势。
8. 判泄漏看趋势(同一场景下随时间只增不减), 而非单点绝对值。

======================================================================
六、附录: 术语
======================================================================
- RSS       : 进程常驻物理内存。本工具对RSS最大的前20进程回填了GPU图形内存
              (memtrack), 所以图形进程的 rss_kb 会比 /proc 原始值大。
- dmabuf    : 跨设备/进程共享的内存缓冲(图形/多媒体), 不计入进程RSS, 需单独看。
- exporter  : dmabuf 的分配/管理者。qcom_hgsl=GPU图形栈, system/qcom,system=通用堆,
              msm_hab=跨虚拟机通信。
- wait%     : 进程"想运行但在队列里等CPU"的时间占比, 反映被CPU竞争拖累的程度。
- MemAvailable : 内核估算的"应用实际还能拿到多少内存", 已计入可回收的cache;
                 判断内存是否紧张用它, 而非 MemFree。
========================================================================
