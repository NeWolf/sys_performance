/* Offline report application. Log strings enter DOM only through textContent. */
(() => {
  'use strict';
  function readReportData() {
    const read = id => {
      const node = document.getElementById(id);
      if (!node) throw new Error('报告数据块缺失：' + id);
      const value = JSON.parse(node.textContent);
      node.remove?.(); // Release parsed text instead of retaining two full copies.
      return value;
    };
    const header = read('report-data');
    if (header.report_chunks !== 1) return header;
    const count = read('report-chunk-count');
    if (!Number.isSafeInteger(count) || count < 1) throw new Error('报告数据块数量无效');
    const holder = {};
    for (let n = 1; n <= count; n++) {
      const operation = read('report-chunk-' + n);
      const [path, value, items] = operation;
      let parent = holder, key = 'data';
      for (const part of path) {
        if (!Object.hasOwn(parent, key)) throw new Error('报告数据块顺序无效');
        parent = parent[key]; key = part;
      }
      if (operation.length === 2) {
        // Define own properties so log keys cannot alter object prototypes.
        Object.defineProperty(parent, key, {value, writable:true, enumerable:true, configurable:true});
      } else if (value === 'text') {
        parent[key] += items;
      } else {
        const target = parent[key];
        if (!Array.isArray(target) || target.length !== value) throw new Error('报告数组块顺序无效');
        for (const item of items) target.push(item);
      }
    }
    return holder.data;
  }
  const data = readReportData();
  document.documentElement.classList.add('js-enabled');
  const valid = v => typeof v === 'number' && Number.isFinite(v);
  // Decimal fractions avoid binary floating-point ties, including negative HALF_UP.
  function decimalFraction(value) {
    const [coefficient, exponent='0'] = String(value).toLowerCase().split('e');
    const places = (coefficient.split('.')[1] || '').length - Number(exponent);
    const numerator = BigInt(coefficient.replace('.', ''));
    return places > 0 ? [numerator, 10n ** BigInt(places)] : [numerator * 10n ** BigInt(-places), 1n];
  }
  function roundedRatio(n, d) {
    if (d < 0n) { n = -n; d = -d; }
    const sign = n < 0n ? -1n : 1n, magnitude = n < 0n ? -n : n;
    return sign * (magnitude / d + (magnitude % d * 2n >= d ? 1n : 0n));
  }
  const fromCents = cents => {
    const magnitude = cents < 0n ? -cents : cents;
    const value = Number((cents < 0n ? '-' : '') + magnitude / 100n + '.' + String(magnitude % 100n).padStart(2,'0'));
    return valid(value) ? (value === 0 ? 0 : value) : null;
  };
  const centsOf = value => {
    const [n,d] = decimalFraction(value);
    return roundedRatio(n * 100n,d);
  };
  const roundMetric = value => valid(value) ? fromCents(centsOf(value)) : null;
  function scaleMetric(value, multiplier=1, divisor=1) {
    if (!valid(value) || !valid(multiplier) || !valid(divisor)) return null;
    const numerator = centsOf(value) * centsOf(multiplier), denominator = centsOf(divisor);
    return denominator === 0n ? null : fromCents(roundedRatio(numerator, denominator));
  }
  function sum(values) {
    const numbers = values.filter(valid);
    return numbers.length ? fromCents(numbers.reduce((total,value) => total + centsOf(value),0n)) : null;
  }
  const fmt = v => valid(v) ? roundMetric(v).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
  const fmtCount = v => valid(v) ? v.toLocaleString('zh-CN', {maximumFractionDigits:0}) : '—';
  function fmtIO(value, perCycle=false) {
    const input = roundMetric(value);
    if (input == null) return '—';
    const units = ['KB','MB','GB','TB','PB'];
    let index = 0;
    while (Math.abs(input) >= 1024 ** (index + 1) && index < units.length - 1) index++;
    return fmt(scaleMetric(input,1,1024 ** index)) + ' ' + units[index];
  }
  const unitText = (value, unit) => /KB/.test(unit) ? fmtIO(value,unit.includes('周期')) : fmt(value) + ' ' + unit;
  function pidLabel(p) {
    const pids = [...new Set(p.pids || [])];
    if (pids.length <= 3) return pids.join(', ') || '—';
    return 'PID变化' + (Number.isInteger(p.pid_changes) ? p.pid_changes : '未知') + '次';
  }
  const processLabel = p => p.name + ' · ' + (new Set(p.pids || []).size > 3 ? '' : 'PID ') + pidLabel(p);
  function metric(p, field, key, active=false) {
    const source = active ? p.active : p;
    return roundMetric((field === 'rss_kb' || field === 'rss_mb' ? source?.display_metrics?.rss_mb : source?.metrics?.[field])?.[key]);
  }
  const fields = data.process_fields;
  const index = Object.fromEntries(data.full_cycle_schema.map((f, i) => [f, i]));
  const axisIndex = Object.fromEntries(data.cycle_axis_schema.map((f, i) => [f, i]));
  const io = ['rd_kb', 'wr_kb', 'rchar_kb', 'wchar_kb'];
  const labels = {cpu1c:'CPU', cpu_total:'CPU 总占用', cpu_idle:'空闲', cpu_user:'用户态占用', cpu_sys:'内核态占用', cpu_irq:'irq+softirq 占用', cpu_iow:'iowait 占用', rss_kb:'内存', rd_kb:'物理读', wr_kb:'物理写', rchar_kb:'逻辑读', wchar_kb:'逻辑写', mem_used_mb:'已使用内存', mem_free_mb:'空闲内存', mem_avail_mb:'空闲内存', mem_total_mb:'总内存', mem_percent:'内存使用率'};
  const fullTimeLabel = value => {
    const date = new Date(value);
    if (!valid(value) || !Number.isFinite(date.getTime())) return '—';
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };
  const timeLabel = value => {
    const label = fullTimeLabel(value);
    return label === '—' ? label : label.slice(11);
  };
  const palette = ['#a78bfa','#38bdf8','#34d399','#fbbf24','#fb7185','#c084fc','#22d3ee','#fb923c'];
  const instances = new Map();
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  function sample(points) {
    return points;
  }
  function markRuns(points, fs) {
    const runs = Object.fromEntries(fs.map(f => [f, 0]));
    let prev = null;
    for (const p of points) {
      for (const f of fs) {
        if (!prev || p.segment !== prev.segment || p.cycle !== prev.cycle + 1 ||
            !valid(p.ts) || !valid(prev.ts) || p.ts <= prev.ts || !valid(p[f]) || !valid(prev[f])) runs[f]++;
        p['_run_' + f] = runs[f];
      }
      prev = p;
    }
    return points;
  }
  function fullPoints(p) {
    return p.full_cycles.map(row => ({segment:p.segment,
      cycle:row[index.cycle], ts:row[index.ts],
      ...Object.fromEntries(fields.map(f => [f,roundMetric(row[index[f]])])),
      rss_mb:scaleMetric(row[index.rss_kb],1,1024)
    }));
  }
  function cumulative(p, fs) {
    const totals = Object.fromEntries(fs.map(f => [f, 0]));
    const points = fullPoints(p);
    for (const point of points) for (const f of fs) {
      if (valid(point[f])) { totals[f] = sum([totals[f],point[f]]); point[f] = totals[f]; }
      else point[f] = null;
    }
    return sample(markRuns(points, fs));
  }
  function lineSeries(points, field, name, color, divisor=1, yAxisIndex=0, full=false) {
    if (field === 'rss_kb') {
      points = points.map(p => ({...p, rss_mb:Object.hasOwn(p,'rss_mb') ? p.rss_mb : scaleMetric(p.rss_kb,1,1024),
        _run_rss_mb:p._run_rss_mb ?? p._run_rss_kb}));
      field = 'rss_mb'; divisor = 1;
    }
    const groups = [];
    let previous = null, group = null;
    for (const p of (full ? points : sample(points))) {
      const run = p['_run_' + field];
      const connected = previous && p.segment === previous.segment && run != null &&
        run === previous['_run_' + field] && valid(p.ts) && valid(previous.ts) && p.ts > previous.ts;
      if (!valid(p[field]) || !valid(p.ts)) { previous = null; group = null; continue; }
      if (!group || !connected) {
        group = {name, type:'line', data:[], showSymbol:true, symbolSize:3, connectNulls:false,
          yAxisIndex, lineStyle:{width:1, color}, itemStyle:{color}, emphasis:{focus:'series'}, animation:false};
        groups.push(group);
      }
      group.data.push(full ? [p.ts, scaleMetric(p[field],1,divisor), p.cycle] : [p.ts, scaleMetric(p[field],1,divisor)]);
      previous = p;
    }
    return groups;
  }
  function overviewSeries(points, field, metric, color, unit, references=false) {
    const series = lineSeries(points, field, labels[field] || field, color);
    if (field === 'cpu_total' || field === 'mem_used_mb') {
      series.forEach(item => { item.lineStyle.width = 1.5; item.z = 5; });
    }
    if (references && series.length) {
      series[0].markLine = {silent:true, symbol:'none', animation:false,
        lineStyle:{type:'dashed',width:1},
        data:['p95','p99'].filter(key => valid(metric?.[key])).map((key, i) => ({
          name:labels[field] + ' ' + key.toUpperCase(), yAxis:roundMetric(metric[key]),
          lineStyle:{color:field === 'cpu_total' ? ['#ffd93d','#f472b6'][i] : color},
          label:{show:true,position:i ? 'insideEndTop' : 'insideStartTop',
            formatter:labels[field] + ' ' + key.toUpperCase() + ': ' + fmt(metric[key]) + ' ' + unit,
            color:field === 'cpu_total' ? ['#ffd93d','#f472b6'][i] : color}
        }))};
    }
    return series;
  }
  function systemPoints(system) {
    return markRuns((system.full_points || system.series.points).map(point => {
      const p = {...point,segment:system.segment};
      for (const f of data.system_fields) {
        const source = f === 'cpu_total' ? 'cpu_single_core' : f + '_single_core';
        p[f] = roundMetric(f.startsWith('cpu') && Object.hasOwn(point,source) ? point[source] : point[f]);
      }
      p.cpu_single_core = Object.hasOwn(point,'cpu_single_core') ? roundMetric(point.cpu_single_core) : p.cpu_total;
      return p;
    }), [...data.system_fields,'cpu_single_core']);
  }
  function overviewOptions(system) {
    const points = systemPoints(system);
    const cpuFields = data.system_fields.filter(f => f.startsWith('cpu'));
    const legend = (fs, primary) => ({type:'scroll',top:0,
      textStyle:{color:'#cbd5e1'},pageTextStyle:{color:'#cbd5e1'},
      data:fs.map(f => labels[f]),
      selected:Object.fromEntries(fs.map(f => [labels[f], f === primary]))});
    const statistics = f => system.full_points ? exactStats(points,f) : system.metrics?.[f];
    const cpuSeries = cpuFields.flatMap((f,i) => overviewSeries(points,f,statistics(f),
      f === 'cpu_total' ? '#ff6b6b' : palette[i%palette.length], '%', f === 'cpu_total'));
    const memFields = ['mem_used_mb', data.system_fields.includes('mem_free_mb') ? 'mem_free_mb' : 'mem_avail_mb'];
    const memSeries = memFields.flatMap((f,i) => overviewSeries(points,f,statistics(f),palette[i%palette.length],'MB',true));
    return {
      cpu:{series:cpuSeries,legend:legend(cpuFields,'cpu_total'),
        yAxis:{type:'value',name:'单核 %',min:0,splitLine:{lineStyle:{color:'#334155'}}},
        title:cpuSeries.length ? undefined : {text:'暂无有效CPU数据',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}}},
      memory:{series:memSeries,legend:legend(memFields,'mem_used_mb'),
        yAxis:{type:'value',min:0,splitLine:{lineStyle:{color:'#334155'}}},
        title:memSeries.length ? undefined : {text:'暂无有效内存数据',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}}}
    };
  }
  const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      const state = instances.get(entry.target);
      if (state && !state.chart) activate(entry.target, state);
      observer.unobserve(entry.target);
    }
  }, {rootMargin:'300px'}) : null;
  function activate(node, state) {
    if (!node.getClientRects().length || node.clientWidth === 0 || node.clientHeight === 0) return;
    state.chart = echarts.init(node, null, {renderer:'canvas'});
    state.chart.setOption(state.option, true);
    node.dataset.ready = 'true';
  }
  function draw(node, option) {
    if (!node) return;
    option = {backgroundColor:'transparent', color:palette, textStyle:{color:'#cbd5e1'}, animation:false,
      tooltip:{trigger:'axis', renderMode:'richText', confine:true},
      legend:{type:'scroll', top:0, textStyle:{color:'#cbd5e1'}, pageTextStyle:{color:'#cbd5e1'}},
      grid:{left:65,right:45,top:65,bottom:65,containLabel:true},
      xAxis:{type:'time', axisLabel:{formatter:timeLabel,hideOverlap:true},
        axisPointer:{label:{formatter:params => fullTimeLabel(params.value)}}, splitLine:{show:false}},
      yAxis:{type:'value', splitLine:{lineStyle:{color:'rgba(148,163,184,.12)'}}},
      dataZoom:[{type:'inside',filterMode:'none'}, {type:'slider',bottom:8,height:18,borderColor:'#475569',textStyle:{color:'#94a3b8'},labelFormatter:timeLabel}],
      ...option};
    const axes = axis => (Array.isArray(axis) ? axis : [axis]).filter(Boolean);
    for (const axis of [...axes(option.xAxis),...axes(option.yAxis)]) {
      if (axis.type !== 'value') continue;
      const ioUnit = /KB/.test(axis.name || '') ? axis.name : null;
      const formatter = ioUnit ? value => fmtIO(value,ioUnit.includes('周期'))
        : axis.minInterval === 1 && axis.name === '有效样本数' ? fmtCount : fmt;
      if (ioUnit) {
        axis.name = '';
        option.tooltip = {valueFormatter:formatter,...option.tooltip};
      }
      axis.axisLabel = {formatter,...axis.axisLabel};
      axis.axisPointer = {label:{formatter:params => formatter(Number(params.value))},...axis.axisPointer};
    }
    option.tooltip = {valueFormatter:fmt,...option.tooltip};
    const prior = instances.get(node);
    if (prior) {
      prior.option = option;
      if (prior.chart) prior.chart.setOption(option, true);
    } else {
      const state = {option, chart:null};
      instances.set(node, state);
      if (observer) observer.observe(node); else activate(node, state);
    }
  }
  function collectedIO(system, processes, full=false) {
    const points = system.cycle_axis.map(axis => ({segment:system.segment,
      cycle:axis[axisIndex.cycle], ts:axis[axisIndex.ts],
      ...Object.fromEntries(io.map(f => [f, null]))}));
    const byCycle = new Map(points.map(p => [p.cycle, p]));
    for (const process of processes) {
      if (process.segment !== system.segment) continue;
      for (const row of process.full_cycles) {
        const point = byCycle.get(row[index.cycle]);
        if (!point || !valid(point.ts) || row[index.ts] !== point.ts || !(row[index.p_records] > 0)) continue;
        for (const f of io) {
          const value = row[index[f]];
          if (valid(value)) point[f] = sum([point[f],value]);
        }
      }
    }
    for (const point of points) for (const f of io) if (!valid(point[f])) point[f] = null;
    markRuns(points, io);
    return full ? points : sample(points);
  }
  function referenceSeries(system, ioPoints, fs) {
    const series = [];
    for (const f of fs) {
      if (f === 'cpu1c') series.push(...lineSeries(systemPoints(system), 'cpu_single_core',
        '系统 CPU · 单核口径', '#ff6b6b'));
      else if (f === 'rss_kb') series.push(...lineSeries(systemPoints(system), 'mem_used_mb',
        '系统 · 已使用内存', '#ff6b6b'));
      else if (io.includes(f)) series.push(...lineSeries(ioPoints, f,
        '已采集进程 IO 合计 · ' + labels[f], ['#ff6b6b','#fbbf24','#34d399','#38bdf8'][io.indexOf(f)]));
    }
    series.forEach(s => { s.lineStyle.width = 1.5; s.lineStyle.type = 'dashed'; s.z = 5; });
    return series;
  }
  function plot(node, members, fs, {total=false, points=null, unit='', fieldLabels=labels, references=[]}={}) {
    const series = [...references];
    members.forEach((p, n) => {
      const source = points || (total ? cumulative(p, fs) : p.full_cycles
        ? markRuns(fullPoints(p),[...fields,'rss_mb']) : p.series.points);
      fs.forEach((f, j) => series.push(...lineSeries(source, f, (p.pids ? processLabel(p) : p.name) + ' · ' + (fieldLabels[f] || f),
        palette[(n * fs.length + j) % palette.length], f === 'rss_kb' ? 1024 : 1)));
    });
    draw(node, {series, yAxis:{type:'value',name:unit,splitLine:{lineStyle:{color:'#334155'}}},
      title:series.length ? undefined : {text:'暂无有效曲线 / 请先选择进程',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}}});
  }
  // Index exact samples once per selected stream; duplicates remain conflicts.
  function overlayStream(points, fs) {
    const samples = new Map(), cycles = new Map();
    for (const p of points) {
      if (!valid(p.ts)) continue;
      if (!cycles.has(p.ts)) cycles.set(p.ts, new Set());
      cycles.get(p.ts).add(p.cycle);
      const key = JSON.stringify([p.cycle, p.ts]);
      samples.set(key, samples.has(key) ? null : p);
    }
    return {points:markRuns(points, fs), samples, cycles};
  }
  function overlayTooltip(entries, unit, node) {
    return {trigger:'axis', renderMode:'html', confine:false, appendToBody:true, enterable:true,
      backgroundColor:'#ffffff', borderColor:'#dce3ed', borderWidth:1, padding:0,
      textStyle:{color:'#243247',fontSize:12},
      extraCssText:'border-radius:12px;box-shadow:0 12px 36px #24324726;white-space:normal;',
      position(point, params, dom, rect, size) {
        const bounds = node.getBoundingClientRect(), gap = 12;
        const width = document.documentElement.clientWidth, height = document.documentElement.clientHeight;
        let x = bounds.left + point[0] + gap;
        if (x + size.contentSize[0] > width - gap) x = bounds.left + point[0] - size.contentSize[0] - gap;
        const y = Math.max(gap, Math.min(bounds.top + point[1] - 24, height - size.contentSize[1] - gap));
        return [Math.max(gap, Math.min(x, width - size.contentSize[0] - gap)) - bounds.left, y - bounds.top];
      },
      formatter(params) {
        const ts = Number(params[0]?.axisValue), cycles = new Set();
        for (const entry of entries) for (const cycle of entry.stream.cycles.get(ts) || []) cycles.add(cycle);
        const cycle = cycles.size === 1 ? [...cycles][0] : null;
        const box = el('div', null, 'overlay-tooltip');
        const millis = valid(ts) ? '.' + String(new Date(ts).getMilliseconds()).padStart(3,'0') : '';
        box.append(el('strong', fullTimeLabel(ts) + millis + ' · 周期 ' + (cycle ?? '冲突'), 'overlay-tooltip-heading'));
        const list = el('div', null, 'overlay-tooltip-list');
        entries.forEach((entry, n) => {
          const point = cycle == null ? null : entry.stream.samples.get(JSON.stringify([cycle, ts]));
          const value = point?.[entry.field];
          const row = el('div', null, 'overlay-tooltip-row' + (entry.reference ? ' is-reference' : ''));
          const dot = el('i', null, 'overlay-tooltip-dot');
          dot.style.backgroundColor = entry.reference ? '#ff6b6b' : palette[n % palette.length];
          row.append(dot, el('span', entry.name + '：', 'overlay-tooltip-name'),
            el('b', valid(value) ? unitText(scaleMetric(value,1,entry.divisor),unit) : '缺失或冲突',
              'overlay-tooltip-value' + (valid(value) ? '' : ' is-missing')));
          list.append(row);
        });
        box.append(list);
        box.style.color = '#243247';
        box.querySelector('.overlay-tooltip-heading').style.cssText = 'color:#243247;border-color:#dce3ed';
        list.style.scrollbarColor = '#a8b5c8 #ffffff';
        box.querySelectorAll('.overlay-tooltip-row').forEach(row => {
          row.style.borderColor = '#e6eaf0';
          if (row.classList.contains('is-reference')) row.style.backgroundColor = '#f4f7fb';
        });
        box.querySelectorAll('.overlay-tooltip-value').forEach(value => {
          value.style.color = value.classList.contains('is-missing') ? '#65748a' : '#243247';
        });
        return box;
      }};
  }
  function plotOverlay(node, entries, unit) {
    const series = entries.flatMap((entry, n) => {
      const curves = lineSeries(entry.stream.points, entry.field, entry.name,
        entry.reference ? '#ff6b6b' : palette[n % palette.length], entry.divisor, 0, true);
      if (entry.reference) curves.forEach(s => { s.lineStyle.type = 'dashed'; s.lineStyle.width = 1.5; s.z = 5; });
      return curves;
    });
    draw(node, {series, tooltip:overlayTooltip(entries, unit, node),
      yAxis:{type:'value',name:unit,splitLine:{lineStyle:{color:'#334155'}}},
      title:series.length ? undefined : {text:'暂无有效曲线 / 请先选择进程',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}}});
  }
  function rank(ps, field, key, active=false) {
    const value = p => metric(p,field,key,active);
    return ps.filter(p => valid(value(p))).sort((a,b) => value(b)-value(a) || a.name.localeCompare(b.name)).slice(0,30);
  }
  function bars(node, ps, field, key, active=false) {
    const ranked = rank(ps,field,key,active);
    draw(node, {tooltip:{trigger:'axis',renderMode:'richText',confine:true}, legend:{show:false},
      xAxis:{type:'value',name:field === 'rss_kb' ? 'MB' : '%'},
      yAxis:{type:'category',inverse:true,data:ranked.map(processLabel),axisLabel:{width:250,overflow:'truncate'}},
      series:[{type:'bar',barMaxWidth:18,data:ranked.map((p,i) => ({value:metric(p,field,key,active),itemStyle:{color:palette[i%palette.length]}}))}],
      dataZoom:[],grid:{left:20,right:65,top:30,bottom:35,containLabel:true}});
  }
  function exactStats(points, f) {
    const values = points.map(p => roundMetric(p[f])).filter(valid).sort((a,b) => a-b);
    const count = values.length;
    const totalCents = values.reduce((total,value) => total + centsOf(value),0n);
    const percentile = q => count ? values[Math.ceil(q*count)-1] : null;
    return {count, min:count ? values[0] : null,
      avg:count ? fromCents(roundedRatio(totalCents,BigInt(count))) : null,
      p95:percentile(.95), p99:percentile(.99), max:count ? values[count-1] : null,
      total:count ? fromCents(totalCents) : null};
  }
  // Never derive MB statistics by scaling KB aggregates: round every sample first.
  function prepareMetrics(p) {
    const points = p.full_cycles ? fullPoints(p) : (p.chart_samples || []).map(row => ({
      cpu1c:roundMetric(row[2]), rss_mb:scaleMetric(row[3],1,1024)
    }));
    const active = points.filter(point => valid(point.cpu1c) && point.cpu1c > 0);
    for (const [source, samples] of [[p,points],[p.active,active]]) {
      if (!source) continue;
      source.display_metrics = {...source.display_metrics,
        rss_mb:source.display_metrics?.rss_mb || exactStats(samples,'rss_mb')};
      for (const field of (p.full_cycles ? fields : ['cpu1c'])) {
        if (source.metrics?.[field]) source.metrics[field] = exactStats(samples,field);
      }
    }
  }
  function mergeFull(system, members) {
    if (!members.length || members.some(p => p.segment !== system.segment)) throw new Error('只能选择同一采集段的进程');
    const maps = members.map(p => {
      const rows = new Map();
      for (const row of p.full_cycles) {
        const cycle = row[index.cycle];
        rows.set(cycle, rows.has(cycle) ? null : row);
      }
      return rows;
    });
    const points = system.cycle_axis.map(axis => {
      const cycle = axis[axisIndex.cycle], ts = axis[axisIndex.ts];
      const rows = maps.map(map => map.get(cycle));
      const aligned = valid(ts) && rows.every(row => row && valid(row[index.ts]) && row[index.ts] === ts);
      const p = {segment:system.segment, cycle, ts:aligned ? ts : null};
      const mergeFields = [...new Set([...fields,'rss_mb'])];
      for (const f of mergeFields) {
        const values = rows.map(row => !aligned ? null : f === 'rss_mb'
          ? scaleMetric(row[index.rss_kb],1,1024) : roundMetric(row[index[f]]));
        p[f] = values.every(valid) ? sum(values) : null;
      }
      return p;
    });
    const mergeFields = [...new Set([...fields,'rss_mb'])];
    markRuns(points, mergeFields);
    const active = points.filter(p => valid(p.cpu1c) && p.cpu1c > 0);
    return {points, active, metrics:Object.fromEntries(mergeFields.map(f => [f,exactStats(points,f)])),
      activeMetrics:Object.fromEntries(mergeFields.map(f => [f,exactStats(active,f)]))};
  }
  function mergeTable(result, cycles, active=false) {
    const container = el('div');
    for (const group of [['cpu1c'], ['rss_kb'], io]) {
      const wrap = el('div', null, 'scroll'), table = el('table'), head = el('thead'), body = el('tbody');
      const tr = el('tr'), isCPU = group[0] === 'cpu1c', isIO = io.includes(group[0]);
      const headers = ['指标 / 单位','有效周期','时段周期覆盖 %','最小','均值','P95','P99','峰值'];
      if (isCPU) headers.push('P95K KDMIPS','峰值K KDMIPS');
      if (isIO) headers.push('有效累计');
      headers.forEach(t => tr.append(el('th',t))); head.append(tr);
      for (const f of group) {
        const m = (active ? result.activeMetrics : result.metrics)[f === 'rss_kb' ? 'rss_mb' : f];
        const row = el('tr');
        const values = [scaleMetric(m.count,100,cycles),
          ...['min','avg','p95','p99','max'].map(k => roundMetric(m[k]))];
        if (isCPU) values.push(...['p95','max'].map(k => scaleMetric(m[k],28.75,100)));
        if (isIO) values.push(m.total);
        row.append(el('td',labels[f] + (isIO ? '' : ' / ' + (f === 'rss_kb' ? 'MB' : '%'))),
          el('td',fmtCount(m.count)));
        values.forEach((v,i) => row.append(el('td',isIO && i > 0 ? fmtIO(v,i < values.length - 1) : fmt(v)))); body.append(row);
      }
      table.append(head,body); wrap.append(table); container.append(wrap);
    }
    return container;
  }
  function setupFocusTable(wrap, toggle) {
    const table = wrap.querySelector('table');
    const groups = Array.from(table.tBodies);
    const input = wrap.querySelector('.table-search');
    const standards = wrap.querySelector('.show-standards');
    const count = wrap.querySelector('.table-count');
    const filter = () => {
      const query = input.value.trim().toLocaleLowerCase();
      groups.forEach(group => { group.hidden = !group.dataset.search.toLocaleLowerCase().includes(query); });
      count.textContent = groups.filter(group => !group.hidden).length + ' / ' + groups.length + ' 个进程';
      wrap.querySelector('.scroll').scrollTop = 0;
    };
    input.addEventListener('input',filter);
    standards.addEventListener('change',() => {
      groups.forEach(group => {
        const identity = group.querySelector('.focus-identity');
        const measured = group.querySelector('.measured');
        const rows = group.querySelectorAll('.standard');
        identity.rowSpan = standards.checked ? 3 : 1;
        (standards.checked ? rows[0] : measured).prepend(identity);
        rows.forEach(row => { row.hidden = !standards.checked; });
      });
    });
    Array.from(table.tHead.rows[0].cells).forEach((th, column) => {
      if (column === 1) return;
      const button = el('button'); button.type = 'button';
      button.append(...Array.from(th.childNodes)); th.append(button);
      button.setAttribute('aria-label',column === 0 ? '按进程名称排序' : '按当前实测 ' + th.textContent + ' 排序');
      let direction = 1;
      button.addEventListener('click',() => {
        table.tHead.querySelectorAll('th').forEach(h => h.removeAttribute('aria-sort'));
        th.setAttribute('aria-sort',direction === 1 ? 'ascending' : 'descending');
        const value = group => column === 0 ? group.querySelector('.focus-identity strong').textContent
          : group.querySelectorAll('.measured td[data-sort]')[column - 2].dataset.sort;
        groups.sort((a,b) => {
          const x = value(a), y = value(b);
          if (x === '' || y === '') return x === y ? 0 : x === '' ? 1 : -1;
          return direction * (column > 1 ? Number(x) - Number(y) : x.localeCompare(y,'zh-CN',{numeric:true}));
        });
        table.append(...groups); direction *= -1;
      });
    });
    groups.forEach(group => {
      const row = group.querySelector('.measured');
      if (!row.dataset.process) return;
      row.addEventListener('click',() => toggle(row.dataset.process));
      row.addEventListener('keydown',e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(row.dataset.process); }
      });
    });
    filter();
  }
  function normalizeTable(wrap) {
    const headers = Array.from(wrap.querySelectorAll('thead tr:first-child th'),th => th.textContent);
    const lookup = new Map(data.processes.map(p => [p.id,p]));
    wrap.querySelectorAll('tr[data-process]').forEach(row => {
      const p = lookup.get(row.dataset.process);
      if (!p) return;
      row.dataset.search = (row.dataset.search || '') + ' ' + (p.pids || []).join(', ');
      const writeValue = (cell,value,integer=false) => {
        cell.dataset.sort = valid(value) ? String(value) : '';
        cell.textContent = (integer ? fmtCount : fmt)(value);
      };
      if (row.classList.contains('measured')) {
        const values = [metric(p,'cpu1c','p95'),scaleMetric(metric(p,'cpu1c','p95'),28.75,100),
          metric(p,'cpu1c','max'),scaleMetric(metric(p,'cpu1c','max'),28.75,100),
          metric(p,'rss_kb','max'),scaleMetric(metric(p,'rd_kb','max'),1,1024),
          scaleMetric(metric(p,'wr_kb','max'),1,1024)];
        row.querySelectorAll('td[data-sort]').forEach((cell,i) => {
          writeValue(cell,values[i]);
          if (i >= 5) {
            const raw = metric(p,i === 5 ? 'rd_kb' : 'wr_kb','max');
            cell.dataset.ioUnit = 'cycle';
            cell.dataset.ioKb = valid(raw) ? String(raw) : '';
            cell.textContent = fmtIO(raw,true);
          }
        });
        const group = row.closest('tbody');
        group.dataset.search = (group.dataset.search || '') + ' ' + (p.pids || []).join(', ');
        const identity = group.querySelector('.focus-identity');
        if (identity && !identity.querySelector('.process-pid')) identity.append(el('span',pidLabel(p),'process-pid'));
        return;
      }
      Array.from(row.cells).forEach((cell,i) => {
        const header = headers[i] || '';
        if (header === 'PID变化') { writeValue(cell,p.pid_changes,true); return; }
        if (header === '活跃周期') { writeValue(cell,p.active?.metrics?.cpu1c?.count,true); return; }
        const field = /内存/.test(header) ? 'rss_kb' : /活跃|CPU/.test(header) ? 'cpu1c'
          : /累计读|物理读/.test(header) ? 'rd_kb' : /累计写|物理写/.test(header) ? 'wr_kb'
          : /逻辑读/.test(header) ? 'rchar_kb' : /逻辑写/.test(header) ? 'wchar_kb' : null;
        const key = /有效周期/.test(header) ? 'count' : /P99/i.test(header) ? 'p99'
          : /P95/i.test(header) ? 'p95' : /峰值/.test(header) ? 'max'
          : /均值/.test(header) ? 'avg' : /累计/.test(header) ? 'total' : null;
        if (!field || !key) return;
        let value = metric(p,field,key,/活跃/.test(header));
        if (/KDMIPS/.test(header)) value = scaleMetric(value,28.75,100);
        if (io.includes(field) && key !== 'count') cell.dataset.ioUnit = key === 'total' ? 'total' : 'cycle';
        writeValue(cell,value,key === 'count');
      });
      const column = headers.indexOf('PID'), cell = row.cells[column];
      if (column >= 0 && cell) {
        cell.textContent = pidLabel(p); cell.title = pidLabel(p); cell.dataset.sort = pidLabel(p);
      }
    });
    wrap.querySelectorAll('td[data-sort]').forEach(cell => {
      const raw = cell.dataset.sort, value = Number(raw);
      if (!raw || !valid(value)) return;
      const header = headers[cell.cellIndex] || '';
      if (/^(PID|序号|记录ID|周期|行号)$/.test(header)) return;
      const integer = /有效样本|有效周期|活跃周期|PID变化|样本数|记录数/.test(header);
      const rounded = integer ? value : roundMetric(value);
      cell.dataset.sort = String(rounded);
      const ioValue = cell.dataset.ioKb === undefined ? rounded : cell.dataset.ioKb === '' ? null : Number(cell.dataset.ioKb);
      cell.textContent = cell.dataset.ioUnit ? fmtIO(ioValue,cell.dataset.ioUnit === 'cycle') : (integer ? fmtCount : fmt)(rounded);
    });
  }
  function setupTable(wrap, toggle) {
    normalizeTable(wrap);
    if (wrap.dataset.table === 'excel') return setupFocusTable(wrap,toggle);
    const table = wrap.querySelector('table'), rows = Array.from(table.tBodies[0].rows);
    const input = wrap.querySelector('.table-search'), count = wrap.querySelector('.table-count');
    const systemToggle = wrap.dataset.table === 'all' ? wrap.querySelector('.show-system-processes') : null;
    const filter = () => {
      const query = (input?.value || '').toLocaleLowerCase().trim();
      for (const row of rows) {
        const hideSystem = systemToggle && !systemToggle.checked && row.dataset.systemProcess === 'true';
        row.hidden = hideSystem || !(row.textContent + ' ' + (row.dataset.search || '')).toLocaleLowerCase().includes(query);
      }
      if (count) count.textContent = rows.filter(row => !row.hidden).length + ' / ' + rows.length + ' 行';
      const scroll = wrap.querySelector('.scroll');
      if (scroll) scroll.scrollTop = 0;
    };
    if (input) input.addEventListener('input',filter);
    if (systemToggle) systemToggle.addEventListener('change',filter);
    filter();
    if (input) Array.from(table.tHead.rows[0].cells).forEach((th, column) => {
      const button = el('button',th.textContent); button.type = 'button'; th.replaceChildren(button);
      let direction = 1;
      button.addEventListener('click',() => {
        table.querySelectorAll('th').forEach(h => h.removeAttribute('aria-sort'));
        th.setAttribute('aria-sort',direction === 1 ? 'ascending' : 'descending');
        rows.sort((a,b) => {
          const x = a.cells[column].dataset.sort, y = b.cells[column].dataset.sort;
          if (x === '' || y === '') return x === y ? 0 : x === '' ? 1 : -1;
          return direction * (Number.isFinite(Number(x)) && Number.isFinite(Number(y)) ? Number(x)-Number(y) : x.localeCompare(y,'zh-CN',{numeric:true}));
        });
        table.tBodies[0].append(...rows); direction *= -1;
      });
    });
    rows.filter(row => row.dataset.process).forEach(row => {
      row.addEventListener('click',() => toggle(row.dataset.process));
      row.addEventListener('keydown',e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(row.dataset.process); }
      });
    });
  }
  function setupSegment(system) {
    if (document.body?.dataset?.reportStyle === 'compact' && typeof window.setupCompactReport === 'function') {
      window.setupCompactReport(system, {data, valid, fmt, fmtCount, roundMetric, scaleMetric,
        sum, exactStats, metric, pidLabel,processLabel, el, lineSeries, draw, setupTable});
      return;
    }
    const root = document.getElementById(system.id);
    const ps = data.processes.filter(p => p.segment === system.segment);
    const byId = new Map(ps.map(p => [p.id,p]));
    const node = role => document.getElementById(system.id + '-' + role);
    const overlay = new Set(), selected = new Set();
    const ioFull = collectedIO(system, ps, true);
    const ioReference = sample(ioFull);
    const systemStream = overlayStream(systemPoints(system),
      ['cpu_single_core', 'mem_used_mb']);
    const ioStream = overlayStream(ioFull, io), processStreams = new Map();
    function plotCompared(role, members, fs, unit) {
      if (!role.startsWith('overlay-')) {
        plot(node(role), members, fs, {unit, references:referenceSeries(system, ioReference, fs)});
        return;
      }
      const entries = fs.map(f => ({reference:true, divisor:1,
        stream:io.includes(f) ? ioStream : systemStream,
        field:f === 'cpu1c' ? 'cpu_single_core' : f === 'rss_kb' ? 'mem_used_mb' : f,
        name:f === 'cpu1c' ? '系统 CPU · 单核口径' : f === 'rss_kb' ? '系统 · 已使用内存' : '已采集进程 IO 合计 · ' + labels[f]}));
      for (const p of members) {
        if (!processStreams.has(p.id)) processStreams.set(p.id, overlayStream(fullPoints(p), ['cpu1c','rss_kb',...io]));
        for (const f of fs) entries.push({stream:processStreams.get(p.id), field:f,
          divisor:f === 'rss_kb' ? 1024 : 1, name:processLabel(p) + ' · ' + labels[f]});
      }
      plotOverlay(node(role), entries, unit);
    }
    let selectionVersion = 0;
    function refreshOverlay() {
      const members = [...overlay].map(id => byId.get(id));
      root.querySelectorAll('tr[data-process]').forEach(row => {
        const chosen = overlay.has(row.dataset.process);
        row.classList.toggle('selected',chosen); row.setAttribute('aria-selected',String(chosen));
      });
      const tags = root.querySelector('.selection-tags'); tags.replaceChildren();
      for (const p of members) {
        const button = el('button',processLabel(p) + ' ×'); button.type = 'button';
        button.setAttribute('aria-label','移除叠加 ' + p.name);
        button.addEventListener('click',() => toggle(p.id)); tags.append(button);
      }
      plotCompared('overlay-cpu',members,['cpu1c'],'%');
      plotCompared('overlay-rss',members,['rss_kb'],'MB');
      plotCompared('overlay-io',members,io,'KB/周期');
      const ioTotal = p => sum(['rd_kb','wr_kb'].map(f => metric(p,f,'total')));
      const ioMembers = members.length ? members : [...ps].filter(p => io.some(f => valid(metric(p,f,'total')))).sort((a,b) =>
        (ioTotal(b) ?? 0) - (ioTotal(a) ?? 0) || a.name.localeCompare(b.name)).slice(0,5);
      for (const [role,fs] of [['physical',io.slice(0,2)],['logical',io.slice(2)]]) {
        plot(node('io-'+role),ioMembers,fs,{unit:'KB/周期'});
        plot(node('io-'+role+'-total'),ioMembers,fs,{total:true,unit:'有效累计 KB'});
      }
    }
    function toggle(id) {
      if (!byId.has(id)) return;
      overlay.has(id) ? overlay.delete(id) : overlay.add(id); refreshOverlay();
    }
    root.querySelectorAll('.data-table').forEach(table => setupTable(table,toggle));
    const allTable = root.querySelector('[data-table="all"]');
    allTable?.querySelector('.select-top15')?.addEventListener('click', () => {
      const visible = new Set(Array.from(allTable.querySelectorAll('tr[data-process]'))
        .filter(row => !row.hidden).map(row => row.dataset.process));
      rank(ps.filter(p => visible.has(p.id)), 'cpu1c', 'avg', true).slice(0,15)
        .forEach(p => overlay.add(p.id));
      refreshOverlay();
    });
    allTable?.querySelector('.clear-selection')?.addEventListener('click', () => {
      overlay.clear();
      refreshOverlay();
    });
    const overview = overviewOptions(system);
    for (const [role, option, fs, unit] of [
      ['system-cpu', overview.cpu, data.system_fields.filter(f => f.startsWith('cpu')), '%'],
      ['memory', overview.memory, ['mem_used_mb', data.system_fields.includes('mem_free_mb') ? 'mem_free_mb' : 'mem_avail_mb'], 'MB']
    ]) {
      const entries = fs.map(field => ({stream:systemStream,field,name:labels[field],divisor:1}));
      const tooltip = overlayTooltip(entries, unit, node(role));
      draw(node(role), {...option, tooltip:{...tooltip,
        formatter(params) {
          const visible = entries.filter(entry => params.some(p => p.seriesName === entry.name));
          return overlayTooltip(visible, unit, node(role)).formatter(params);
        }}});
    }
    const thresholds = system.cpu_exceedances.thresholds.filter(t => valid(t.threshold));
    draw(node('exceed'), {tooltip:{trigger:'axis',valueFormatter:fmtCount},legend:{show:false},dataZoom:[],xAxis:{type:'category',data:thresholds.map(t => '> '+fmt(t.threshold)+'%')},
      title:thresholds.length ? undefined : {text:'暂无有效系统CPU样本',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}},
      yAxis:{type:'value',name:'有效样本数',minInterval:1},series:[{type:'bar',barMaxWidth:90,data:thresholds.map((t,i) => ({value:t.count,itemStyle:{color:['#fbbf24','#fb923c','#fb7185'][i]}})),label:{show:true,formatter:params => fmtCount(params.value),position:'top',color:'#e2e8f0'}}]});
    bars(node('active-top'),ps,'cpu1c','avg',true);
    bars(node('rss-top'),ps,'rss_kb','avg');
    plot(node('peak-top'),rank(ps,'cpu1c','max'),['cpu1c'],{unit:'%',fieldLabels:{cpu1c:'CPU'}});
    refreshOverlay();
    const search = root.querySelector('.merge-search');
    const mergeSystemToggle = root.querySelector('.merge-show-system-processes');
    const candidates = root.querySelector('.candidate-list'), chosenList = root.querySelector('.selected-list');
    const status = root.querySelector('.merge-status'), resultNode = root.querySelector('.merge-result');
    const apply = root.querySelector('.merge-apply');
    function clearResult() {
      resultNode.replaceChildren();
      for (const [role,fs,unit] of [['cpu',['cpu1c'],'%'],['rss',['rss_kb'],'MB'],['io',io,'KB/周期']]) {
        plotCompared('merge-'+role,[],fs,unit);
      }
    }
    const checks = new Map();
    for (const p of ps) {
      const label = el('label'), check = el('input'); check.type = 'checkbox';
      check.value = p.id;
      label.title = processLabel(p) + (p.dp_only ? ' · DP-only' : '');
      label.dataset.search = p.name + ' ' + (p.pids || []).join(', ');
      label.append(check,el('span',label.title));
      check.addEventListener('change',() => {
        check.checked ? selected.add(p.id) : selected.delete(p.id); refreshSelection();
      });
      const name = p.name.trim();
      const isSystem = !name.startsWith('jkc') && (
        ['android','com.android','.','vendor','/apex/com.android','/system/','/vendor/'].some(prefix => name.startsWith(prefix)) ||
        (name.startsWith('[') && name.endsWith(']')) || !name.includes('.'));
      candidates.append(label); checks.set(p.id,{check,label,isSystem});
    }
    function refreshSelection() {
      selectionVersion++;
      chosenList.replaceChildren();
      for (const p of ps) checks.get(p.id).check.checked = selected.has(p.id);
      for (const id of selected) {
        const p = byId.get(id), row = el('div'), remove = el('button','移除'); remove.type = 'button';
        remove.setAttribute('aria-label','移除合并成员 ' + p.name);
        remove.addEventListener('click',() => { selected.delete(id); refreshSelection(); });
        row.title = processLabel(p);
        row.append(el('span',processLabel(p)),remove); chosenList.append(row);
      }
      root.querySelector('.merge-count').textContent = selected.size;
      apply.disabled = selected.size === 0;
      clearResult(); status.textContent = selected.size ? '选择已变更，请重新计算。' : '请选择同段进程；无选择不生成零值统计。';
    }
    function filterCandidates() {
      const query = search.value.trim().toLocaleLowerCase();
      checks.forEach(({label,isSystem}) => {
        label.hidden = (!mergeSystemToggle.checked && isSystem) || !(label.dataset.search + ' ' + label.title).toLocaleLowerCase().includes(query);
      });
      candidates.scrollTop = 0;
    }
    search.addEventListener('input',filterCandidates);
    mergeSystemToggle.addEventListener('change',filterCandidates);
    filterCandidates();
    root.querySelector('.merge-clear').addEventListener('click',() => { selected.clear(); refreshSelection(); });
    apply.addEventListener('click',() => {
      const members = [...selected].map(id => byId.get(id));
      if (!members.length) return;
      const version = selectionVersion;
      apply.disabled = true; status.textContent = '正在按全部周期对齐并计算…';
      setTimeout(() => {
        if (version !== selectionVersion) return;
        try {
          const result = mergeFull(system,members), sampled = sample(result.points);
          const aggregate = {name:'合并（'+members.length+'个名称）', series:{points:sampled}};
          resultNode.replaceChildren(el('h4','全量合并统计'),mergeTable(result,system.cycles),
            el('h4','合并后有效CPU > 0 的活跃统计'),mergeTable(result,system.cycles,true));
          const complete = result.points.filter(p => fields.every(f => valid(p[f]))).length;
          status.textContent = '已对齐 '+system.cycles+' 个周期；七字段完整 '+complete+'；活跃 '+result.active.length+'。图表和统计均保留全量点。';
          plotCompared('merge-cpu',[aggregate],['cpu1c'],'%');
          plotCompared('merge-rss',[aggregate],['rss_kb'],'MB');
          plotCompared('merge-io',[aggregate],io,'KB/周期');
        } catch (error) { status.textContent = '合并失败：'+error.message; }
        finally { apply.disabled = selected.size === 0; }
      },0);
    });
    refreshSelection();
  }
  data.processes.forEach(prepareMetrics);
  data.systems.forEach(setupSegment);
  function resizeVisible(root=document) {
    instances.forEach((state, node) => {
      if (!root.contains(node) || !node.getClientRects().length || !node.clientWidth) return;
      if (state.chart) state.chart.resize();
      else activate(node, state);
    });
  }
  document.querySelectorAll('details').forEach(details => {
    details.addEventListener('toggle', () => {
      if (details.open) requestAnimationFrame(() => resizeVisible(details));
    });
  });
  let resizeFrame;
  window.addEventListener('resize',() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => resizeVisible());
  });
  let printClosed = [];
  window.addEventListener('beforeprint', () => {
    printClosed = [...document.querySelectorAll('details:not([open])')];
    printClosed.forEach(details => { details.open = true; });
    resizeVisible();
  });
  window.addEventListener('afterprint', () => {
    printClosed.forEach(details => { details.open = false; });
    printClosed = [];
    requestAnimationFrame(() => resizeVisible());
  });
})();