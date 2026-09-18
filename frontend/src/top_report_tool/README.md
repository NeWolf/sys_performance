# top 性能数据分析报告生成器

把智能座舱 Android `top -b` 采集文件，一键生成交互式 HTML 性能分析报告。
**不依赖任何 Claude / 在线服务，纯本地 Python 即可运行。**

## 环境要求
- Python 3.7+
- numpy  (`pip install numpy`)

## 使用方法
```bash
# 单个文件
python3 top_report.py top_raw_20260916_152602.txt -o report_out

# 多个文件 (自动生成 index.html 索引)
python3 top_report.py top_raw_*.txt -o report_out
```
生成的 HTML 直接用浏览器打开即可，无需联网（图表库走 CDN，若需完全离线见下）。

## 输出内容
每个文件一个 HTML，含：
1. 整机 CPU / 内存随时间折线（带 P95 / P99 参考线）
2. 全部非内核进程 vs 整机的组合对比图（图例可搜索/切换，定位增高来源）
3. 全进程统计表（均值 / P95 / P99 / 峰值），点击任意行查看该进程详细折线

## 平台自动识别
- **8255**：`800%cpu` 满载、进程行含 VSWAP 列、内存 14725M
- **8295**：`700%cpu` 满载、无 VSWAP 列、内存 20617M
工具按此自动切换解析规则和 CPU 口径，无需手工指定。

## 口径说明
- 进程 %CPU 为单核=100% 口径（跨平台可直接对比）
- 整机 CPU used = 满载% − idle%（8255 满载 800%，8295 满载 700%，不可直接跨平台比整机整数值）
- 进程内存 MB = %MEM × 整机内存 / 100

## 集成到你自己的程序
- `parse_file(path)` 返回逐采样的结构化数据，可直接喂给你的后端/数据库
- `build_payload(S)` 产出统计字典（均值/P95/P99/峰值 + 时序），可用于生成任意格式报表
- `render(...)` 负责套用 HTML 模板；换模板只需改 `_template_src.py`
可作为库导入：`from top_report import parse_file, build_payload`

## 完全离线部署（可选）
HTML 里的 Chart.js 走 CDN。若目标环境无外网，把
`https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js`
下载下来，改 `_template_src.py` 里的 `<script src=...>` 指向本地文件即可。
