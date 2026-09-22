"""Shared metric semantics; identifiers and categories are not numeric measures."""
import math

from perf_parser import SCHEMAS


def resource_settings(config):
    """Validate explicit calibration without changing legacy settings payloads."""
    if not isinstance(config, dict):
        raise ValueError("报告配置必须是对象")
    platform = config.get("cpu_platform", "")
    factor = config.get("kdmips_per_core")
    if not isinstance(platform, str) or len(platform) > 120:
        raise ValueError("cpu_platform 必须是最多 120 字的文本")
    if factor is not None:
        try:
            valid = type(factor) in (int, float) and math.isfinite(factor) and factor > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("kdmips_per_core 必须是正有限数或 null")
        if not platform.strip():
            raise ValueError("填写换算系数时必须填写 CPU 平台")
    return dict(cpu_platform=platform, kdmips_per_core=factor)


def to_kdmips(cpu1c, factor):
    if cpu1c is None or factor is None:
        return None
    value = cpu1c / 100 * factor
    return value if math.isfinite(value) else None


CATEGORIES = {"S": ("boot",), "P": ("pol", "state", "cpu_core")}
IDENTIFIERS = {"ts", "pid", "name", "exporter", "boot", "pol", "state", "cpu_core"}
NUMERIC = {kind: tuple(field for field in schema.split() if field not in IDENTIFIERS)
           for kind, schema in SCHEMAS.items()}
INCREMENTS = {"rss_delta_kb", "majflt", "rd_kb", "wr_kb", "rchar_kb", "wchar_kb", "syscr", "syscw"}
LABELS = {
    "cpu_total": "系统 CPU 总占用", "cpu_user": "用户态 CPU", "cpu_sys": "内核态 CPU",
    "cpu_iow": "I/O 等待 CPU", "cpu_irq": "中断 CPU", "mem_avail_mb": "可用内存",
    "mem_free_mb": "空闲内存",
    "mem_total_mb": "总内存", "mem_used_mb": "已用内存", "cpu": "进程 CPU（整机口径）",
    "cpu1c": "进程 CPU（单核口径）", "dmips": "DMIPS 加权占用", "wait": "调度等待",
    "nthr": "线程数", "rss_kb": "驻留内存 RSS", "rss_delta_kb": "周期 RSS 变化",
    "majflt": "周期主缺页", "rd_kb": "周期物理读取", "wr_kb": "周期物理写入",
    "rchar_kb": "周期逻辑读取", "wchar_kb": "周期逻辑写入", "syscr": "周期读系统调用",
    "syscw": "周期写系统调用", "total_mb": "系统 dmabuf 大小", "total_objs": "系统 dmabuf 对象数",
    "size_mb": "Exporter dmabuf 大小", "size_kb": "进程 dmabuf 大小", "objs": "dmabuf 对象数",
    "boot": "开机计数", "pol": "调度策略", "state": "进程状态", "cpu_core": "最后运行核",
}
NOTES = {
    "cpu1c": "单核口径，可超过 100%",
    "dmips": "按大小核算力折算，同构 SoC 约等于 cpu",
    "wait": "运行队列等待占比，需 sched_detail",
    "rss_kb": "RSS 前 20 进程可能含 GPU 回填，不与 dmabuf 相加",
    "rss_delta_kb": "有符号周期变化；累计为已采集变化之和，不保证等于首尾差",
    "majflt": "周期增量，可作为 I/O 代理信号",
    "rd_kb": "物理 I/O 周期增量，非 KB/s；零值可能受权限限制",
    "wr_kb": "物理 I/O 周期增量，非 KB/s；零值可能受权限限制",
    "rchar_kb": "逻辑 I/O 周期增量，可能命中缓存",
    "wchar_kb": "逻辑 I/O 周期增量，可能命中缓存",
    "syscr": "周期增量，零值可能受权限限制",
    "syscw": "周期增量，零值可能受权限限制",
    "cpu_core": "-1 为未知；样本频数不是各核 CPU 时间占比",
    "total_mb": "低频实际样本，缺失不补零",
    "size_kb": "同进程内去重，跨进程可能共享；DP 合计不必等于 D",
}
METHOD = ("基于当前统计范围的全部有效原始样本，忽略缺失值、不补零；"
          "P95/P99 使用最近秩法：升序第 ceil(p×N) 个值（从 1 开始），不插值；"
          "均值和分类占比均按样本计算，非时间加权。图表缩放、降采样不改变统计。"
          "累计仅适用于周期增量；大整数和大额浮点累计可能舍入。")


def unit(field):
    if field.endswith("_kb"):
        value = "KB"
    elif field.endswith("_mb"):
        value = "MB"
    elif field.startswith("cpu") or field in {"dmips", "wait"}:
        value = "%"
    elif field == "nthr":
        value = "个"
    elif field in {"objs", "total_objs"}:
        value = "个"
    else:
        value = "次"
    return value + (" / 周期" if field in INCREMENTS else "")


def metadata(field):
    return {"label": LABELS[field], "unit": unit(field), "note": NOTES.get(field, "")}