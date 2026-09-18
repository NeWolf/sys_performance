#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能座舱 top 性能数据分析工具
用法:
    python3 top_report.py 文件1.txt [文件2.txt ...] [-o 输出目录]
    python3 top_report.py *.txt -o report_out

功能:
  - 自动识别平台 (8255=800%/有VSWAP列, 8295=700%/无VSWAP列)
  - 解析每个 top 采集文件, 计算整机及各进程 CPU/内存 的 均值/P95/P99/峰值
  - 生成交互式 HTML 报告 (整机总览图 + 全进程组合图 + 可点击的进程详情折线图)
  - 多文件时自动生成 index.html 索引页

依赖: Python3, numpy   (pip install numpy)
"""
import json, os, re, html, sys, argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

def load_template():
    src = open(os.path.join(HERE, "_template_src.py"), encoding="utf-8").read()
    return src.split('HTML_TEMPLATE = """', 1)[1].split('"""', 1)[0]

TS   = re.compile(r'时间: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
MEM  = re.compile(r'Mem:\s+(\d+)M total,\s+(\d+)M used,\s+(\d+)M free')
CPU  = re.compile(r'(\d+)%cpu\s+(\d+)%user\s+(\d+)%nice\s+(\d+)%sys\s+(\d+)%idle')
# 无VSWAP (8295): PID USER PR NI VIRT RES SHR S %CPU %SCPU %MEM TIME+ ARGS
P_NOVS = re.compile(r'^\s*(\d+)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+([A-Z])\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\S+)\s+(.*)$')
# 有VSWAP (8255): 多一列 VSWAP
P_VSWAP = re.compile(r'^\s*(\d+)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+([A-Z])\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\S+)\s+(.*)$')

def detect(txt):
    vs = 'VSWAP' in txt
    m = re.search(r'(\d+)%cpu', txt)
    full = int(m.group(1)) if m else (800 if vs else 700)
    return vs, full

def parse_file(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    vs, full = detect(txt)
    prx = P_VSWAP if vs else P_NOVS
    ci_cpu, ci_mem, ci_name = (10, 12, 14) if vs else (9, 11, 13)
    S = []; cur = None
    for line in txt.splitlines():
        m = TS.search(line)
        if m:
            if cur: S.append(cur)
            cur = {'time': m.group(1), 'cpu': None, 'memt': None, 'memu': None, 'procs': {}}; continue
        if cur is None: continue
        mm = MEM.search(line)
        if mm: cur['memt'] = int(mm.group(1)); cur['memu'] = int(mm.group(2)); continue
        mc = CPU.search(line)
        if mc: cur['cpu'] = int(mc.group(1)) - int(mc.group(5)); continue
        mp = prx.match(line)
        if mp:
            cpu = float(mp.group(ci_cpu)); memp = float(mp.group(ci_mem)); nm = mp.group(ci_name).strip()
            mb = memp * cur['memt'] / 100.0 if cur['memt'] else 0.0
            if nm in cur['procs']:
                cur['procs'][nm]['cpu'] += cpu; cur['procs'][nm]['mem_mb'] += mb
            else:
                cur['procs'][nm] = {'cpu': cpu, 'mem_mb': mb}
    if cur: S.append(cur)
    return S, vs, full

def pct(a, p): return float(np.percentile(a, p)) if a else 0.0

def build_payload(S):
    n = len(S)
    times = [s['time'] for s in S]
    ct = [s['cpu'] for s in S if s['cpu'] is not None]
    mt = [s['memu'] for s in S if s['memu'] is not None]
    names = set()
    for s in S: names.update(s['procs'])
    pc = {nm: [0.0]*n for nm in names}; pm = {nm: [0.0]*n for nm in names}
    for i, s in enumerate(S):
        for nm, inf in s['procs'].items():
            pc[nm][i] = inf['cpu']; pm[nm][i] = inf['mem_mb']
    stats = []
    for nm in names:
        c = pc[nm]; m = pm[nm]
        stats.append({'name': nm, 'cpu_mean': float(np.mean(c)), 'cpu_p95': pct(c,95), 'cpu_p99': pct(c,99),
            'cpu_max': float(np.max(c)), 'mem_mean': float(np.mean(m)), 'mem_p95': pct(m,95), 'mem_p99': pct(m,99),
            'mem_max': float(np.max(m)), 'n': int(sum(1 for v in c if v>0))})
    stats.sort(key=lambda r: -r['cpu_mean'])
    proc_charts = [{'name': r['name'], 'cpu': pc[r['name']], 'mem': pm[r['name']],
        'cpu_p95': r['cpu_p95'], 'cpu_p99': r['cpu_p99'], 'mem_p95': r['mem_p95'], 'mem_p99': r['mem_p99'],
        'cpu_mean': r['cpu_mean'], 'mem_mean': r['mem_mean']} for r in stats[:25]]
    allser = {nm: {'cpu': pc[nm], 'mem': pm[nm]} for nm in names}
    return {'n_samples': n, 'time_start': times[0] if times else '', 'time_end': times[-1] if times else '',
        'x': list(range(n)), 'cpu_total': ct, 'mem_total': mt, 'cpu_p95': pct(ct,95), 'cpu_p99': pct(ct,99),
        'mem_p95': pct(mt,95), 'mem_p99': pct(mt,99), 'cpu_mean': float(np.mean(ct)) if ct else 0,
        'mem_mean': float(np.mean(mt)) if mt else 0, 'cpu_max': max(ct) if ct else 0, 'mem_max': max(mt) if mt else 0,
        'mem_total_val': mt[0] if mt else 0, 'proc_charts': proc_charts, 'all_proc_stats': stats, 'all_series': allser}

def render(tpl, p, fname, full, memtot, nav=''):
    plat = '8255' if full == 800 else '8295'
    t = tpl.replace('700%', f'{full}%')
    title = f"性能数据 · {plat} · {fname}"
    meta = f"{p['n_samples']} 个采样点 · {p['time_start']} ~ {p['time_end']} · {len(p['all_proc_stats'])} 个进程 · {plat}平台({full}%=8核,{memtot}M)"
    nk = len([r for r in p['all_proc_stats'] if not r['name'].startswith('[')])
    return t.format(title=html.escape(title), meta=html.escape(meta), nav_links=nav,
        n_samples=p['n_samples'], time_start=p['time_start'], time_end=p['time_end'],
        cpu_mean=p['cpu_mean'], cpu_p95=p['cpu_p95'], cpu_p99=p['cpu_p99'], cpu_max=p['cpu_max'],
        cpu_pct_of_8=p['cpu_max']/8, mem_mean=p['mem_mean'], mem_p95=p['mem_p95'], mem_p99=p['mem_p99'],
        mem_max=p['mem_max'], mem_total_val=p['mem_total_val'], n_nonkernel=nk, data_json=json.dumps(p))

def make_index(items, outdir):
    h = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
         '<title>top 性能数据分析</title><style>:root{color-scheme:light}'
         'body{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f5f6f8;color:#222;margin:0}'
         'header{background:#1a2233;color:#fff;padding:24px 28px}h1{margin:0 0 6px;font-size:22px}'
         '.container{max-width:920px;margin:0 auto;padding:24px}.card{background:#fff;border-radius:10px;'
         'padding:18px 22px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}.card a{color:#2d6cdf;'
         'text-decoration:none;font-size:17px;font-weight:600}.tag{display:inline-block;background:#eef;'
         'border-radius:4px;padding:1px 8px;font-size:12px;margin-left:8px;color:#3355aa}'
         '.info{color:#666;font-size:13px;margin-top:6px}</style></head><body>'
         '<header><h1>top 性能数据分析</h1><div style="opacity:.8;font-size:13px">'
         f'{len(items)} 份采集文件 · 单核=100% 口径 · 交互式进程详情</div></header><div class="container">')
    for fn, href, plat, p in items:
        info = (f"{p['n_samples']}采样 · {p['time_start']}~{p['time_end']} · "
                f"CPU均{p['cpu_mean']:.0f}/P95 {p['cpu_p95']:.0f}/峰{p['cpu_max']:.0f} · "
                f"内存峰{p['mem_max']:.0f}MB · {len(p['all_proc_stats'])}进程")
        h += (f'<div class="card"><a href="{href}">{html.escape(fn)}</a>'
              f'<span class="tag">{plat}</span><div class="info">{info}</div></div>')
    h += '</div></body></html>'
    open(os.path.join(outdir, 'index.html'), 'w', encoding='utf-8').write(h)

def main():
    ap = argparse.ArgumentParser(description="智能座舱 top 性能数据分析报告生成器")
    ap.add_argument('files', nargs='+', help='一个或多个 top_raw_*.txt 文件')
    ap.add_argument('-o', '--out', default='report_out', help='输出目录 (默认 report_out)')
    args = ap.parse_args()
    tpl = load_template()
    os.makedirs(args.out, exist_ok=True)
    items = []
    for fp in args.files:
        if not os.path.isfile(fp):
            print(f"[跳过] 找不到文件: {fp}"); continue
        S, vs, full = parse_file(fp)
        if not S:
            print(f"[跳过] 未解析到采样: {fp}"); continue
        memtot = next((s['memt'] for s in S if s['memt']), 0)
        p = build_payload(S)
        fname = os.path.basename(fp)
        plat = '8255' if full == 800 else '8295'
        href = os.path.splitext(fname)[0] + f'_{plat}.html'
        htmlc = render(tpl, p, fname, full, memtot)
        open(os.path.join(args.out, href), 'w', encoding='utf-8').write(htmlc)
        items.append((fname, href, plat, p))
        print(f"[完成] {fname} -> {href}  ({plat}, {p['n_samples']}采样, CPU均{p['cpu_mean']:.0f}%/峰{p['cpu_max']:.0f}%, 内存峰{p['mem_max']:.0f}MB)")
    if len(items) > 1:
        make_index(items, args.out)
        print(f"[完成] 索引页 index.html ({len(items)} 份)")
    print(f"\n输出目录: {os.path.abspath(args.out)}")

if __name__ == '__main__':
    main()
