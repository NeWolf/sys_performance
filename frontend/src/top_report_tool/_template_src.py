import json, os, html

with open('chart_data_full.json') as f:
    payloads = json.load(f)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif; margin: 0; padding: 0; background: #f5f6f8; color: #222; }}
header {{ background: #1a2233; color: #fff; padding: 18px 28px; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
header h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 600; }}
header .meta {{ font-size: 13px; opacity: .8; }}
header a {{ color: #8ab4ff; text-decoration: none; margin-right: 16px; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 24px; }}
.section {{ background: #fff; border-radius: 10px; padding: 20px 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.section h2 {{ margin: 0 0 6px; font-size: 18px; color: #1a2233; border-left: 4px solid #2d6cdf; padding-left: 10px; }}
.section .desc {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 12px; margin-bottom: 16px; }}
.stat-card {{ background: #f0f4fa; border-radius: 8px; padding: 12px 14px; }}
.stat-card .label {{ font-size: 12px; color: #666; margin-bottom: 4px; }}
.stat-card .value {{ font-size: 20px; font-weight: 600; color: #1a2233; }}
.stat-card .sub {{ font-size: 11px; color: #888; }}
.chart-box {{ position: relative; height: 420px; margin: 12px 0; }}
.chart-box-tall {{ position: relative; height: 560px; margin: 12px 0; }}
.chart-grid {{ display: grid; grid-grid-template-columns: 1fr 1fr; gap: 20px; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
@media (max-width: 900px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}
.legend-hint {{ font-size: 12px; color: #888; margin: 8px 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; }}
th {{ background: #f7f8fa; color: #555; font-weight: 600; position: sticky; top: 0; cursor: pointer; }}
th:hover {{ background: #eef1f5; }}
th:first-child, td:first-child {{ text-align: left; }}
tr {{ cursor: pointer; }}
tr:hover td {{ background: #f0f4fa; }}
tr.selected td {{ background: #e3ecf7; }}
.filter-bar {{ margin-bottom: 12px; }}
.filter-bar input {{ padding: 8px 12px; width: 300px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }}
.note {{ font-size: 12px; color: #888; margin-top: 8px; }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.pill-p95 {{ background: #fff4e5; color: #cc7a00; }}
.pill-p99 {{ background: #fede8e8; color: #c0392b; }}
#detailArea {{ min-height: 200px; }}
.detail-empty {{ text-align: center; padding: 40px; color: #999; font-size: 14px; }}
.detail-header {{ font-size: 16px; font-weight: 600; color: #1a2233; margin-bottom: 4px; }}
.detail-stats {{ font-size: 13px; color: #666; margin-bottom: 12px; }}
.color-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }}
/* Custom legend for combo charts */
.clegend {{ max-height: 200px; overflow-y: auto; border: 1px solid #e0e0e0; border-radius: 6px; padding: 8px 10px; margin: 8px 0; background: #fafbfc; }}
.clegend-bar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }}
.clegend-bar input {{ padding: 6px 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 12px; width: 200px; }}
.clegend-bar button {{ padding: 5px 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 12px; cursor: pointer; background: #fff; }}
.clegend-bar button:hover {{ background: #f0f0f0; }}
.clegend-list {{ display: flex; flex-wrap: wrap; gap: 4px 12px; }}
.clegend-item {{ display: inline-flex; align-items: center; gap: 4px; cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: 4px; user-select: none; }}
.clegend-item:hover {{ background: #e8eef5; }}
.clegend-item.off {{ opacity: 0.4; text-decoration: line-through; }}
.clegend-dot {{ width: 12px; height: 3px; border-radius: 2px; flex-shrink: 0; }}
.clegend-count {{ font-size: 11px; color: #888; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  <div style="margin-top:8px;">
    <a href="index.html">← 返回索引</a>
    {nav_links}
  </div>
</header>
<div class="container">

<div class="section">
  <h2>总览 · 整机 CPU 与内存</h2>
  <div class="desc">整机 CPU 占用（%）与内存使用（MB）随时间变化，含 P95 / P99 参考线。CPU% 以 8 核满载 = 700% 为口径。</div>
  <div class="stat-grid">
    <div class="stat-card"><div class="label">采样数</div><div class="value">{n_samples}</div><div class="sub">{time_start} ~ {time_end}</div></div>
    <div class="stat-card"><div class="label">CPU 均值</div><div class="value">{cpu_mean:.0f}%</div><div class="sub">P95 {cpu_p95:.0f}% · P99 {cpu_p99:.0f}%</div></div>
    <div class="stat-card"><div class="label">CPU 峰值</div><div class="value">{cpu_max:.0f}%</div><div class="sub">占 8 核 {cpu_pct_of_8:.0f}%</div></div>
    <div class="stat-card"><div class="label">内存均值</div><div class="value">{mem_mean:.0f} MB</div><div class="sub">P95 {mem_p95:.0f} · P99 {mem_p99:.0f}</div></div>
    <div class="stat-card"><div class="label">内存峰值</div><div class="value">{mem_max:.0f} MB</div><div class="sub">总 {mem_total_val} MB</div></div>
  </div>
  <div class="chart-grid">
    <div><div class="chart-box"><canvas id="cpuTotal"></canvas></div></div>
    <div><div class="chart-box"><canvas id="memTotal"></canvas></div></div>
  </div>
</div>

<div class="section">
  <h2>组合图 · 整机 vs 各进程 CPU（已过滤内核进程）</h2>
  <div class="desc">整机 CPU 与全部 {n_nonkernel} 个非内核进程同图对比，便于定位"是谁引起的增高"。橙色虚线 = P95，红色虚线 = P99（均针对整机）。下方图例可搜索、点击切换显示/隐藏。</div>
  <div class="chart-box-tall"><canvas id="cpuCombo"></canvas></div>
  <div class="clegend">
    <div class="clegend-bar">
      <input id="cpuLegendFilter" type="text" placeholder="过滤进程名...">
      <button onclick="toggleAll('cpuCombo', true)">全显</button>
      <button onclick="toggleAll('cpuCombo', false)">全隐</button>
      <button onclick="showTopN('cpuCombo', 15)">只显前15</button>
      <span class="clegend-count" id="cpuLegendCount"></span>
    </div>
    <div class="clegend-list" id="cpuLegendList"></div>
  </div>
</div>

<div class="section">
  <h2>组合图 · 整机 vs 各进程 内存（已过滤内核进程）</h2>
  <div class="desc">整机内存与全部 {n_nonkernel} 个非内核进程同图对比。橙色虚线 = P95，红色虚线 = P99（均针对整机）。</div>
  <div class="chart-box-tall"><canvas id="memCombo"></canvas></div>
  <div class="clegend">
    <div class="clegend-bar">
      <input id="memLegendFilter" type="text" placeholder="过滤进程名...">
      <button onclick="toggleAll('memCombo', true)">全显</button>
      <button onclick="toggleAll('memCombo', false)">全隐</button>
      <button onclick="showTopN('memCombo', 15)">只显前15</button>
      <span class="clegend-count" id="memLegendCount"></span>
    </div>
    <div class="clegend-list" id="memLegendList"></div>
  </div>
</div>

<div class="section">
  <h2>进程详情 · 点击查看折线图</h2>
  <div class="desc">在下方表格中点击任意一行，即可在上方展开该进程的 CPU / 内存详细折线图（含 P95 / P99 参考线）。表格可排序、可搜索。</div>
  <div id="detailArea">
    <div class="detail-empty">↑ 点击下方表格中任意进程行，查看其详细 CPU / 内存折线图</div>
  </div>
  <div class="filter-bar" style="margin-top:20px;"><input id="procFilter" type="text" placeholder="过滤进程名..."></div>
  <div style="max-height:560px; overflow:auto;">
  <table id="procTable">
    <thead><tr>
      <th data-sort="name">进程名</th>
      <th data-sort="cpu_mean">CPU均值%</th>
      <th data-sort="cpu_p95">CPU P95%</th>
      <th data-sort="cpu_p99">CPU P99%</th>
      <th data-sort="cpu_max">CPU峰值%</th>
      <th data-sort="mem_mean">内存均值MB</th>
      <th data-sort="mem_p95">内存P95MB</th>
      <th data-sort="mem_p99">内存P99MB</th>
      <th data-sort="mem_max">内存峰值MB</th>
      <th data-sort="n">采样数</th>
    </tr></thead>
    <tbody id="procTableBody"></tbody>
  </table>
  </div>
</div>

</div>
<script>
const DATA = {data_json};
const x = DATA.x;
const cpuTotal = DATA.cpu_total;
const memTotal = DATA.mem_total;

// ---- Non-kernel process list (sorted by mean CPU desc) ----
const nonKernel = DATA.all_proc_stats.filter(r => !r.name.startsWith('['));
const nNonKernel = nonKernel.length;

function linePlugin(p95, p99, color95, color99) {{
  return {{
    id: 'pLines'+Math.random().toString(36).slice(2),
    afterDraw(chart) {{
      const {{ctx, chartArea: {{left, right}}, scales: {{y}}}} = chart;
      function drawLine(val, color, label) {{
        if (val == null) return;
        const py = y.getPixelForValue(val);
        ctx.save();
        ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([6,4]);
        ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(right, py); ctx.stroke();
        ctx.fillStyle = color; ctx.font = '11px sans-serif'; ctx.setLineDash([]);
        ctx.fillText(label + ' ' + (val).toFixed(1), left+6, py-4);
        ctx.restore();
      }}
      drawLine(p95, color95, 'P95'); drawLine(p99, color99, 'P99');
    }}
  }};
}}

function makeChart(canvasId, label, data, p95, p99, color, yLabel) {{
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: x,
      datasets: [{{ label: label, data: data, borderColor: color, backgroundColor: color+'22', borderWidth: 1, pointRadius: 0, tension: 0.2, fill: true }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {{
        legend: {{ display: true, position: 'top' }},
        tooltip: {{ callbacks: {{ title: (i)=>'采样 #'+i[0].label, label: (c)=>c.dataset.label+': '+Number(c.raw).toFixed(1) }} }}
      }},
      scales: {{
        x: {{ ticks: {{ maxTicksLimit: 10, autoSkip: true }}, title: {{ display: true, text: '采样序号' }} }},
        y: {{ title: {{ display: true, text: yLabel }}, beginAtZero: true }}
      }}
    }},
    plugins: [linePlugin(p95, p99, '#e8a33d', '#d64545')]
  }});
}}

makeChart('cpuTotal', '整机 CPU 占用 (%)', cpuTotal, DATA.cpu_p95, DATA.cpu_p99, '#2d6cdf', 'CPU % (700%=8核)');
makeChart('memTotal', '整机内存使用 (MB)', memTotal, DATA.mem_p95, DATA.mem_p99, '#2ca06b', '内存 MB');

// ---- Color palette ----
const PROC_COLORS = [
  '#e74c3c','#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71',
  '#e84393','#0984e3','#00b894','#fd79a8','#6c5ce7','#fdcb6e','#00cec9',
  '#ff7675','#74b9ff','#a29bfe','#55efc4','#ffeaa7','#fab1a0','#74b9ff',
  '#dfe6e9','#b2bec3','#636e72','#2d3436','#e17055','#00cec9','#fd79a8',
  '#6c5ce7','#a29bfe','#fdcb6e','#e84393','#00b894','#0984e3','#e74c3c',
  '#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71','#e84393',
  '#0984e3','#00b894','#fd79a8','#6c5ce7','#fdcb6e','#00cec9','#ff7675',
  '#74b9ff','#a29bfe','#55efc4','#ffeaa7','#fab1a0','#e17055','#00cec9',
  '#fd79a8','#6c5ce7','#a29bfe','#fdcb6e','#e84393','#00b894','#0984e3',
  '#e74c3c','#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71',
  '#e84393','#0984e3','#00b894','#fd79a8','#6c5ce7','#fdcb6e','#00cec9',
  '#ff7675','#74b9ff','#a29bfe','#55efc4','#ffeaa7','#fab1a0','#e17055',
  '#00cec9','#fd79a8','#6c5ce7','#a29bfe','#fdcb6e','#e84393','#00b894',
  '#0984e3','#e74c3c','#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22',
  '#2ecc71','#e84393','#0984e3','#00b894','#fd79a8','#6c5ce7','#fdcb6e',
  '#00cec9','#ff7675','#74b9ff','#a29bfe','#55efc4','#ffeaa7','#fab1a0',
  '#e17055','#00cec9','#fd79a8','#6c5ce7','#a29bfe','#fdcb6e','#e84393',
  '#00b894','#0984e3','#e74c3c','#9b59b6','#3498db','#1abc9c','#f39c12',
  '#e67e22','#2ecc71','#e84393','#0984e3','#00b894','#fd79a8','#6c5ce7',
  '#fdcb6e','#00cec9','#ff7675','#74b9ff','#a29bfe','#55efc4','#ffeaa7',
  '#fab1a0','#e17055','#00cec9','#fd79a8','#6c5ce7','#a29bfe','#fdcb6e',
  '#e84393','#00b894','#0984e3','#e74c3c','#9b59b6','#3498db','#1abc9c',
  '#f39c12','#e67e22','#2ecc71','#e84393','#0984e3','#00b894','#fd79a8',
  '#6c5ce7','#fdcb6e','#00cec9','#ff7675','#74b9ff','#a29bfe','#55efc4',
  '#ffeaa7','#fab1a0','#e17055','#00cec9','#fd79a8','#6c5ce7','#a29bfe',
  '#fdcb6e','#e84393','#00b894','#0984e3','#e74c3c','#9b59b6','#3498db',
  '#1abc9c','#f39c12','#e67e22','#2ecc71','#e84393','#0984e3','#00b894',
  '#fd79a8','#6c5ce7','#fdcb6e','#00cec9','#ff7675','#74b9ff','#a29bfe',
  '#55efc4','#ffeaa7','#fab1a0','#e17055','#00cec9','#fd79a8','#6c5ce7',
  '#a29bfe','#fdcb6e','#e84393','#00b894','#0984e3','#e74c3c','#9b59b6',
  '#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71','#e84393','#0984e3',
  '#00b894','#fd79a8','#6c5ce7','#fdcb6e','#00cec9','#ff7675','#74b9ff',
  '#a29bfe','#55efc4','#ffeaa7','#fab1a0','#e17055','#00cec9','#fd79a8',
  '#6c5ce7','#a29bfe','#fdcb6e','#e84393','#00b894','#0984e3','#e74c3c',
  '#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71','#e84393',
  '#0984e3','#00b894','#fd79a8','#6c5ce7','#fdcb6e','#00cec9','#ff7675',
  '#74b9ff','#a29bfe','#55efc4','#ffeaa7','#fab1a0','#e17055','#00cec9',
  '#fd79a8','#6c5ce7','#a29bfe','#fdcb6e','#e84393','#00b894','#0984e3',
  '#e74c3c','#9b59b6','#3498db','#1abc9c','#f39c12','#e67e22','#2ecc71'
];

// ---- Build combo chart with ALL non-kernel processes ----
function buildCombo(canvasId, totalLabel, totalData, totalColor, procKey, yLabel, p95, p99, legendListId, legendFilterId, legendCountId) {{
  const datasets = [{{ label: totalLabel, data: totalData, borderColor: totalColor, backgroundColor: totalColor+'10', borderWidth: 2.5, pointRadius: 0, tension: 0.2, fill: false, order: 0 }}];
  nonKernel.forEach((r, i) => {{
    const series = DATA.all_series[r.name];
    datasets.push({{
      label: r.name, data: series[procKey], borderColor: PROC_COLORS[i % PROC_COLORS.length],
      borderWidth: 1, pointRadius: 0, tension: 0.2, fill: false, order: i+1,
      hidden: i >= 15  // default show top 15
    }});
  }});
  const ctx = document.getElementById(canvasId).getContext('2d');
  const chart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: x, datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {{
        legend: {{ display: false }},  // use custom legend
        tooltip: {{ mode: 'index', intersect: false, callbacks: {{ title: (i)=>'采样 #'+i[0].label, label: (c)=>c.dataset.label+': '+Number(c.raw).toFixed(1) }} }}
      }},
      scales: {{
        x: {{ ticks: {{ maxTicksLimit: 10, autoSkip: true }}, title: {{ display: true, text: '采样序号' }} }},
        y: {{ title: {{ display: true, text: yLabel }}, beginAtZero: true }}
      }}
    }},
    plugins: [linePlugin(p95, p99, '#e8a33d', '#d64545')]
  }});
  // Build custom legend
  const legendList = document.getElementById(legendListId);
  const legendCount = document.getElementById(legendCountId);
  function updateCount() {{
    const visible = chart.data.datasets.filter(d => !d.hidden).length;
    legendCount.textContent = `(显示 ${{visible}} / ${{chart.data.datasets.length}} 条)`;
  }}
  function renderLegend(filter) {{
    filter = (filter || '').toLowerCase();
    legendList.innerHTML = '';
    chart.data.datasets.forEach((ds, i) => {{
      if (filter && !ds.label.toLowerCase().includes(filter)) return;
      const item = document.createElement('span');
      item.className = 'clegend-item' + (ds.hidden ? ' off' : '');
      const color = (i === 0) ? totalColor : PROC_COLORS[(i-1) % PROC_COLORS.length];
      item.innerHTML = `<span class="clegend-dot" style="background:${{color}}"></span>${{ds.label}}`;
      item.addEventListener('click', () => {{
        ds.hidden = !ds.hidden;
        item.classList.toggle('off', ds.hidden);
        chart.update('none');
        updateCount();
      }});
      legendList.appendChild(item);
    }});
    updateCount();
  }}
  renderLegend('');
  document.getElementById(legendFilterId).addEventListener('input', e => renderLegend(e.target.value));
  return chart;
}}

const cpuComboChart = buildCombo('cpuCombo', '整机 CPU%', cpuTotal, '#1a2233', 'cpu', 'CPU %', DATA.cpu_p95, DATA.cpu_p99, 'cpuLegendList', 'cpuLegendFilter', 'cpuLegendCount');
const memComboChart = buildCombo('memCombo', '整机内存MB', memTotal, '#1a2233', 'mem', '内存 MB', DATA.mem_p95, DATA.mem_p99, 'memLegendList', 'memLegendFilter', 'memLegendCount');

function toggleAll(canvasId, show) {{
  const chart = Chart.getChart(canvasId);
  chart.data.datasets.forEach(ds => ds.hidden = !show);
  chart.update('none');
  // update legend UI
  const listId = canvasId === 'cpuCombo' ? 'cpuLegendList' : 'memLegendList';
  document.querySelectorAll('#'+listId+' .clegend-item').forEach((item, i) => item.classList.toggle('off', !show));
  const countId = canvasId === 'cpuCombo' ? 'cpuLegendCount' : 'memLegendCount';
  const visible = chart.data.datasets.filter(d => !d.hidden).length;
  document.getElementById(countId).textContent = `(显示 ${{visible}} / ${{chart.data.datasets.length}} 条)`;
}}

function showTopN(canvasId, n) {{
  const chart = Chart.getChart(canvasId);
  // dataset 0 = total, always show; 1..n show, rest hide
  chart.data.datasets.forEach((ds, i) => {{ ds.hidden = (i > n); }});
  chart.update('none');
  const listId = canvasId === 'cpuCombo' ? 'cpuLegendList' : 'memLegendList';
  document.querySelectorAll('#'+listId+' .clegend-item').forEach((item, i) => item.classList.toggle('off', i > n));
  const countId = canvasId === 'cpuCombo' ? 'cpuLegendCount' : 'memLegendCount';
  const visible = chart.data.datasets.filter(d => !d.hidden).length;
  document.getElementById(countId).textContent = `(显示 ${{visible}} / ${{chart.data.datasets.length}} 条)`;
}}

// ---- Detail on click ----
let detailChartCpu = null, detailChartMem = null;
const detailArea = document.getElementById('detailArea');

function showDetail(idx) {{
  const p = DATA.all_proc_stats[idx];
  const series = DATA.all_series[p.name] || {{ cpu: [], mem: [] }};
  detailArea.innerHTML = `
    <div class="detail-header"><span class="color-dot" style="background:#6c5ce7"></span>${{p.name}}</div>
    <div class="detail-stats">
      CPU: 均值 ${{p.cpu_mean.toFixed(1)}}% · <span class="pill pill-p95">P95 ${{p.cpu_p95.toFixed(1)}}%</span> · <span class="pill pill-p99">P99 ${{p.cpu_p99.toFixed(1)}}%</span> · 峰值 ${{p.cpu_max.toFixed(1)}}%
      &nbsp;|&nbsp; 内存: 均值 ${{p.mem_mean.toFixed(0)}}MB · <span class="pill pill-p95">P95 ${{p.mem_p95.toFixed(0)}}MB</span> · <span class="pill pill-p99">P99 ${{p.mem_p99.toFixed(0)}}MB</span> · 峰值 ${{p.mem_max.toFixed(0)}}MB
      &nbsp;|&nbsp; 采样 ${{p.n}} 次
    </div>
    <div class="chart-grid">
      <div class="chart-box"><canvas id="dCpu"></canvas></div>
      <div class="chart-box"><canvas id="dMem"></canvas></div>
    </div>
  `;
  detailChartCpu = makeChart('dCpu', p.name+' CPU%', series.cpu, p.cpu_p95, p.cpu_p99, '#6c5ce7', 'CPU %');
  detailChartMem = makeChart('dMem', p.name+' 内存MB', series.mem, p.mem_p95, p.mem_p99, '#00b894', '内存 MB');
}}

// ---- Full table ----
const tbody = document.getElementById('procTableBody');
let procRows = DATA.all_proc_stats.map((r, idx) => ({{
  idx, ...r,
  html: `<tr data-idx="${{idx}}"><td>${{r.name}}</td><td>${{r.cpu_mean.toFixed(1)}}</td><td>${{r.cpu_p95.toFixed(1)}}</td><td>${{r.cpu_p99.toFixed(1)}}</td><td>${{r.cpu_max.toFixed(1)}}</td><td>${{r.mem_mean.toFixed(0)}}</td><td>${{r.mem_p95.toFixed(0)}}</td><td>${{r.mem_p99.toFixed(0)}}</td><td>${{r.mem_max.toFixed(0)}}</td><td>${{r.n}}</td></tr>`
}}));
function renderTable(rows) {{
  tbody.innerHTML = rows.map(r=>r.html).join('');
  tbody.querySelectorAll('tr').forEach(tr => {{
    tr.addEventListener('click', () => {{
      tbody.querySelectorAll('tr').forEach(t => t.classList.remove('selected'));
      tr.classList.add('selected');
      showDetail(parseInt(tr.dataset.idx));
      detailArea.scrollIntoView({{ behavior:'smooth', block:'start' }});
    }});
  }});
}}
renderTable(procRows);

document.getElementById('procFilter').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  renderTable(procRows.filter(r => r.name.toLowerCase().includes(q)));
}});

let sortKey = 'cpu_mean', sortDesc = true;
document.querySelectorAll('#procTable th').forEach(th => {{
  th.addEventListener('click', (e) => {{
    e.stopPropagation();
    const key = th.dataset.sort;
    if (sortKey === key) sortDesc = !sortDesc; else {{ sortKey = key; sortDesc = true; }}
    const sorted = [...procRows].sort((a,b) => {{
      let va = a[key], vb = b[key];
      if (typeof va === 'string') {{ va = va.toLowerCase(); vb = vb.toLowerCase(); return sortDesc ? vb.localeCompare(va) : va.localeCompare(vb); }}
      return sortDesc ? vb-va : va-vb;
    }});
    renderTable(sorted);
  }});
}});
</script>
</body>
</html>
"""

files = list(payloads.keys())
nav_parts = []
for i, f in enumerate(files):
    short = f.replace('top_raw_','').replace('.txt','')
    nav_parts.append(f'<a href="top_{i+1}.html">文件{i+1} ({short})</a>')
nav_html = ' '.join(nav_parts)

for i, (fname, p) in enumerate(payloads.items()):
    title = f"性能数据 · 文件 {i+1} / {len(files)} · {fname}"
    meta = f"{p['n_samples']} 个采样点 · {p['time_start']} ~ {p['time_end']} · {len(p['all_proc_stats'])} 个进程"
    data_json = json.dumps(p)
    n_nonkernel = len([r for r in p['all_proc_stats'] if not r['name'].startswith('[')])
    html_content = HTML_TEMPLATE.format(
        title=html.escape(title), meta=html.escape(meta), nav_links=nav_html,
        n_samples=p['n_samples'], time_start=p['time_start'], time_end=p['time_end'],
        cpu_mean=p['cpu_mean'], cpu_p95=p['cpu_p95'], cpu_p99=p['cpu_p99'],
        cpu_max=p['cpu_max'], cpu_pct_of_8=p['cpu_max']/7,
        mem_mean=p['mem_mean'], mem_p95=p['mem_p95'], mem_p99=p['mem_p99'],
        mem_max=p['mem_max'], mem_total_val=p['mem_total_val'],
        n_nonkernel=n_nonkernel,
        data_json=data_json,
    )
    out = f'top_{i+1}.html'
    with open(out, 'w') as f:
        f.write(html_content)
    print(f"Wrote {out}: {len(html_content)//1024} KB")

index_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>046车 0805 性能数据分析</title>
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif; margin:0; padding:0; background:#f5f6f8; color:#222; }
header { background:#1a2233; color:#fff; padding:24px 28px; }
header h1 { margin:0 0 6px; font-size:22px; font-weight:600; }
header .meta { font-size:13px; opacity:.8; }
.container { max-width:1000px; margin:0 auto; padding:24px; }
.card { background:#fff; border-radius:10px; padding:20px 24px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,.06); transition:box-shadow .2s; }
.card:hover { box-shadow:0 4px 12px rgba(0,0,0,.1); }
.card a { color:#2d6cdf; text-decoration:none; font-size:17px; font-weight:600; }
.card a:hover { text-decoration:underline; }
.card .info { color:#666; font-size:13px; margin-top:8px; }
.stat { display:inline-block; margin-right:18px; font-size:13px; color:#444; }
.stat b { color:#1a2233; }
.note { color:#888; font-size:12px; margin-top:16px; line-height:1.6; }
</style>
</head>
<body>
<header>
  <h1>046车 0805 性能数据分析</h1>
  <div class="meta">5 个 top 采集文件 · 整机与进程级 CPU / 内存折线图 · 含 P95 / P99 分位 · 组合对比图（已过滤内核进程）· 交互式进程详情</div>
</header>
<div class="container">
"""
for i, (fname, p) in enumerate(payloads.items()):
    index_html += f"""
<div class="card">
  <a href="top_{i+1}.html">文件 {i+1}：{fname}</a>
  <div class="info">
    <span class="stat">采样 <b>{p['n_samples']}</b></span>
    <span class="stat">时段 <b>{p['time_start']} ~ {p['time_end']}</b></span>
    <span class="stat">进程数 <b>{len(p['all_proc_stats'])}</b></span>
    <span class="stat">CPU 均值 <b>{p['cpu_mean']:.0f}%</b> P95 <b>{p['cpu_p95']:.0f}%</b> P99 <b>{p['cpu_p99']:.0f}%</b></span>
    <span class="stat">内存均值 <b>{p['mem_mean']:.0f}MB</b> P95 <b>{p['mem_p95']:.0f}MB</b></span>
  </div>
</div>
"""
index_html += """
<div class="note">
说明：CPU% 以 8 核满载 = 700% 为口径（单核 100%）。内存单位为 MB。P95/P99 为分位数参考线。<br>
每个文件页含：①整机 CPU/内存折线总览；②组合图（整机 vs 全部非内核进程同图对比，定位增高来源，图例可搜索/切换）；③全进程统计表，点击任意行查看该进程详细折线图。
</div>
</div>
</body>
</html>
"""
with open('index.html', 'w') as f:
    f.write(index_html)
print("Wrote index.html")
