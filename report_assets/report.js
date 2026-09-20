/* Offline report application. Log strings enter DOM only through textContent. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('report-data').textContent);
  document.documentElement.classList.add('js-enabled');
  const valid = v => typeof v === 'number' && Number.isFinite(v);
  const fmt = v => valid(v) ? v.toLocaleString('zh-CN', {maximumFractionDigits: 2}) : '—';
  const fields = data.process_fields;
  const index = Object.fromEntries(data.full_cycle_schema.map((f, i) => [f, i]));
  const axisIndex = Object.fromEntries(data.cycle_axis_schema.map((f, i) => [f, i]));
  const io = ['rd_kb', 'wr_kb', 'rchar_kb', 'wchar_kb'];
  const labels = {cpu1c:'单核 CPU', cpu_total:'整机 CPU 总占用', cpu_idle:'空闲', cpu_user:'用户态占用', cpu_sys:'内核态占用', cpu_irq:'irq+softirq 占用', cpu_iow:'iowait 占用', rss_kb:'RSS', rd_kb:'物理读', wr_kb:'物理写', rchar_kb:'逻辑读', wchar_kb:'逻辑写', mem_used_mb:'已使用内存', mem_avail_mb:'空闲内存', mem_total_mb:'总内存', mem_percent:'内存使用率'};
  const timeLabel = value => {
    const date = new Date(value);
    return valid(value) && Number.isFinite(date.getTime()) ? date.toISOString().slice(11,19) : '—';
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
    if (points.length <= 600) return points;
    return Array.from({length:600}, (_, i) => points[Math.round(i * (points.length - 1) / 599)]);
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
    return p.full_cycles.map(row => Object.fromEntries([
      ['segment', p.segment], ...['cycle', 'ts', ...fields].map(f => [f, row[index[f]]])
    ]));
  }
  function cumulative(p, fs) {
    const totals = Object.fromEntries(fs.map(f => [f, 0]));
    const points = fullPoints(p);
    for (const point of points) for (const f of fs) {
      if (valid(point[f])) { totals[f] += point[f]; point[f] = totals[f]; }
      else point[f] = null;
    }
    return sample(markRuns(points, fs));
  }
  function lineSeries(points, field, name, color, divisor=1, yAxisIndex=0) {
    const groups = [];
    let previous = null, group = null;
    for (const p of sample(points)) {
      const run = p['_run_' + field];
      const connected = previous && p.segment === previous.segment && run != null &&
        run === previous['_run_' + field] && valid(p.ts) && valid(previous.ts) && p.ts > previous.ts;
      if (!valid(p[field]) || !valid(p.ts)) { previous = null; group = null; continue; }
      if (!group || !connected) {
        group = {name, type:'line', data:[], showSymbol:true, symbolSize:3, connectNulls:false,
          yAxisIndex, lineStyle:{width:1.5, color}, itemStyle:{color}, emphasis:{focus:'series'}, animation:false};
        groups.push(group);
      }
      group.data.push([p.ts, p[field] / divisor]);
      previous = p;
    }
    return groups;
  }
  function overviewSeries(points, field, metric, color, unit, references=false) {
    const series = lineSeries(points, field, labels[field] || field, color);
    if (field === 'cpu_total' || field === 'mem_used_mb') {
      series.forEach(item => { item.lineStyle.width = 3; item.z = 5; });
    }
    if (references && series.length) {
      series[0].markLine = {silent:true, symbol:'none', animation:false,
        lineStyle:{type:'dashed',width:1.5},
        data:['p95','p99'].filter(key => valid(metric?.[key])).map((key, i) => ({
          name:labels[field] + ' ' + key.toUpperCase(), yAxis:metric[key],
          lineStyle:{color:field === 'cpu_total' ? ['#ffd93d','#f472b6'][i] : color},
          label:{show:true,position:i ? 'insideEndTop' : 'insideStartTop',
            formatter:labels[field] + ' ' + key.toUpperCase() + ': ' + fmt(metric[key]) + ' ' + unit,
            color:field === 'cpu_total' ? ['#ffd93d','#f472b6'][i] : color}
        }))};
    }
    return series;
  }
  function overviewOptions(system) {
    const points = system.series.points;
    const cpuFields = data.system_fields.filter(f => f.startsWith('cpu'));
    const legend = (fs, primary) => ({type:'scroll',top:0,
      textStyle:{color:'#cbd5e1'},pageTextStyle:{color:'#cbd5e1'},
      data:fs.map(f => labels[f]),
      selected:Object.fromEntries(fs.map(f => [labels[f], f === primary]))});
    const cpuSeries = cpuFields.flatMap((f,i) => overviewSeries(points,f,system.metrics[f],
      f === 'cpu_total' ? '#ff6b6b' : palette[i%palette.length], '%', f === 'cpu_total'));
    const memFields = ['mem_used_mb','mem_avail_mb'];
    const memSeries = memFields.flatMap((f,i) => overviewSeries(points,f,system.metrics[f],palette[i%palette.length],'MB',true));
    return {
      cpu:{series:cpuSeries,legend:legend(cpuFields,'cpu_total'),
        yAxis:{type:'value',name:'整机 %',min:0,max:100},
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
      xAxis:{type:'time', name:'时间 (UTC)', axisLabel:{formatter:timeLabel,hideOverlap:true},
        axisPointer:{label:{formatter:params => {
          const date = new Date(params.value);
          return Number.isFinite(date.getTime()) ? date.toISOString().replace('T',' ') : '—';
        }}}, splitLine:{show:false}},
      yAxis:{type:'value', splitLine:{lineStyle:{color:'rgba(148,163,184,.12)'}}},
      dataZoom:[{type:'inside',filterMode:'none'}, {type:'slider',bottom:8,height:18,borderColor:'#475569',textStyle:{color:'#94a3b8'},labelFormatter:timeLabel}],
      ...option};
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
  function collectedIO(system, processes) {
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
          if (valid(value)) point[f] = (point[f] ?? 0) + value;
        }
      }
    }
    for (const point of points) for (const f of io) if (!valid(point[f])) point[f] = null;
    return sample(markRuns(points, io));
  }
  function referenceSeries(system, ioPoints, fs) {
    const series = [];
    for (const f of fs) {
      if (f === 'cpu1c') series.push(...lineSeries(system.series.points, 'cpu_total',
        '系统 CPU · 整机占用×8', '#ff6b6b', 1 / 8));
      else if (f === 'rss_kb') series.push(...lineSeries(system.series.points, 'mem_used_mb',
        '系统 · 已使用内存', '#ff6b6b'));
      else if (io.includes(f)) series.push(...lineSeries(ioPoints, f,
        '已采集进程 IO 合计 · ' + labels[f], ['#ff6b6b','#fbbf24','#34d399','#38bdf8'][io.indexOf(f)]));
    }
    series.forEach(s => { s.lineStyle.width = 3; s.lineStyle.type = 'dashed'; s.z = 5; });
    return series;
  }
  function plot(node, members, fs, {total=false, points=null, unit='', fieldLabels=labels, references=[]}={}) {
    const series = [...references];
    members.forEach((p, n) => {
      const source = points || (total ? cumulative(p, fs) : p.series.points);
      fs.forEach((f, j) => series.push(...lineSeries(source, f, p.name + ' · ' + (fieldLabels[f] || f),
        palette[(n * fs.length + j) % palette.length], f === 'rss_kb' ? 1024 : 1)));
    });
    draw(node, {series, yAxis:{type:'value',name:unit,splitLine:{lineStyle:{color:'#334155'}}},
      title:series.length ? undefined : {text:'暂无有效曲线 / 请先选择进程',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}}});
  }
  function rank(ps, field, key, active=false) {
    const value = p => (active ? p.active.metrics : p.metrics)[field]?.[key];
    return ps.filter(p => valid(value(p))).sort((a,b) => value(b)-value(a) || a.name.localeCompare(b.name)).slice(0,30);
  }
  function bars(node, ps, field, key, active=false) {
    const ranked = rank(ps,field,key,active);
    draw(node, {tooltip:{trigger:'axis',renderMode:'richText',confine:true}, legend:{show:false},
      xAxis:{type:'value',name:field === 'rss_kb' ? 'MB' : '单核 %'},
      yAxis:{type:'category',inverse:true,data:ranked.map(p => p.name),axisLabel:{width:250,overflow:'truncate'}},
      series:[{type:'bar',barMaxWidth:18,data:ranked.map((p,i) => ({value:(active ? p.active.metrics : p.metrics)[field][key] / (field === 'rss_kb' ? 1024 : 1),itemStyle:{color:palette[i%palette.length]}}))}],
      dataZoom:[],grid:{left:20,right:65,top:30,bottom:35,containLabel:true}});
  }
  function exactStats(points, f) {
    const values = points.map(p => p[f]).filter(valid).sort((a,b) => a-b);
    const count = values.length;
    const total = count ? values.reduce((a,b) => a+b, 0) : null;
    const percentile = q => count ? values[Math.ceil(q*count)-1] : null;
    return {count, min:count ? values[0] : null, avg:count ? total/count : null,
      p95:percentile(.95), p99:percentile(.99), max:count ? values[count-1] : null, total};
  }
  function mergeFull(system, members) {
    if (!members.length || members.some(p => p.segment !== system.segment)) throw new Error('只能选择同一采集段的进程');
    const maps = members.map(p => new Map(p.full_cycles.map(row => [row[index.cycle], row])));
    const points = system.cycle_axis.map(axis => {
      const cycle = axis[axisIndex.cycle], ts = axis[axisIndex.ts];
      const rows = maps.map(map => map.get(cycle));
      const p = {segment:system.segment, cycle, ts};
      const times = rows.map(row => row?.[index.ts]);
      if (times.some(t => !valid(t) || t !== ts)) p.ts = null;
      for (const f of fields) {
        p[f] = rows.every(row => row && valid(row[index[f]])) ? rows.reduce((sum,row) => sum+row[index[f]],0) : null;
        if (!valid(p[f])) p[f] = null;
      }
      return p;
    });
    markRuns(points, fields);
    const active = points.filter(p => valid(p.cpu1c) && p.cpu1c > 0);
    return {points, active, metrics:Object.fromEntries(fields.map(f => [f,exactStats(points,f)])),
      activeMetrics:Object.fromEntries(fields.map(f => [f,exactStats(active,f)]))};
  }
  function mergeTable(result, cycles, active=false) {
    const container = el('div');
    for (const group of [['cpu1c'], ['rss_kb'], io]) {
      const wrap = el('div', null, 'scroll'), table = el('table'), head = el('thead'), body = el('tbody');
      const tr = el('tr'), isCPU = group[0] === 'cpu1c', isIO = io.includes(group[0]);
      const headers = ['指标 / 单位','有效周期','时段周期覆盖 %','最小','均值','P95','P99','峰值'];
      if (isCPU) headers.push('P95K KDMIPS','峰值K KDMIPS');
      if (isIO) headers.push('有效累计 KB');
      headers.forEach(t => tr.append(el('th',t))); head.append(tr);
      for (const f of group) {
        const m = (active ? result.activeMetrics : result.metrics)[f];
        const divisor = f === 'rss_kb' ? 1024 : 1, row = el('tr');
        const values = [m.count, cycles ? m.count/cycles*100 : null,
          ...['min','avg','p95','p99','max'].map(k => valid(m[k]) ? m[k]/divisor : null)];
        if (isCPU) values.push(...['p95','max'].map(k => valid(m[k]) ? m[k]/100*28.75 : null));
        if (isIO) values.push(m.total);
        row.append(el('td',labels[f] + ' / ' + (f === 'rss_kb' ? 'MB' : isIO ? 'KB/周期' : '单核 %')));
        values.forEach(v => row.append(el('td',fmt(v)))); body.append(row);
      }
      table.append(head,body); wrap.append(table); container.append(wrap);
    }
    return container;
  }
  function setupTable(wrap, toggle) {
    const table = wrap.querySelector('table'), rows = Array.from(table.tBodies[0].rows);
    const input = wrap.querySelector('.table-search'), count = wrap.querySelector('.table-count');
    const filter = () => {
      const query = (input?.value || '').toLocaleLowerCase().trim();
      for (const row of rows) row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
      if (count) count.textContent = rows.filter(row => !row.hidden).length + ' / ' + rows.length + ' 行';
    };
    if (input) input.addEventListener('input',filter);
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
    const root = document.getElementById(system.id);
    const ps = data.processes.filter(p => p.segment === system.segment);
    const byId = new Map(ps.map(p => [p.id,p]));
    const node = role => document.getElementById(system.id + '-' + role);
    const overlay = new Set(), selected = new Set();
    const ioReference = collectedIO(system, ps);
    function plotCompared(role, members, fs, unit) {
      plot(node(role), members, fs, {unit, references:referenceSeries(system, ioReference, fs)});
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
        const button = el('button',p.name + ' ×'); button.type = 'button';
        button.setAttribute('aria-label','移除叠加 ' + p.name);
        button.addEventListener('click',() => toggle(p.id)); tags.append(button);
      }
      plotCompared('overlay-cpu',members,['cpu1c'],'单核 %');
      plotCompared('overlay-rss',members,['rss_kb'],'MB');
      plotCompared('overlay-io',members,io,'KB/周期');
      const ioMembers = members.length ? members : [...ps].filter(p => io.some(f => valid(p.metrics[f]?.total))).sort((a,b) =>
        (b.metrics.rd_kb.total || 0)+(b.metrics.wr_kb.total || 0)-(a.metrics.rd_kb.total || 0)-(a.metrics.wr_kb.total || 0)).slice(0,5);
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
    const overview = overviewOptions(system);
    draw(node('system-cpu'), overview.cpu);
    draw(node('memory'), overview.memory);
    const thresholds = system.cpu_exceedances.thresholds.filter(t => valid(t.threshold));
    draw(node('exceed'), {legend:{show:false},dataZoom:[],xAxis:{type:'category',data:thresholds.map(t => '> '+fmt(t.threshold)+'%')},
      title:thresholds.length ? undefined : {text:'暂无有效系统CPU样本',left:'center',top:'center',textStyle:{color:'#94a3b8',fontSize:14}},
      yAxis:{type:'value',name:'有效样本数',minInterval:1},series:[{type:'bar',barMaxWidth:90,data:thresholds.map((t,i) => ({value:t.count,itemStyle:{color:['#fbbf24','#fb923c','#fb7185'][i]}})),label:{show:true,position:'top',color:'#e2e8f0'}}]});
    bars(node('active-top'),ps,'cpu1c','avg',true);
    bars(node('rss-top'),ps,'rss_kb','avg');
    plot(node('peak-top'),rank(ps,'cpu1c','max'),['cpu1c'],{unit:'%',fieldLabels:{cpu1c:'CPU'}});
    refreshOverlay();
    const search = root.querySelector('.merge-search');
    const candidates = root.querySelector('.candidate-list'), chosenList = root.querySelector('.selected-list');
    const status = root.querySelector('.merge-status'), resultNode = root.querySelector('.merge-result');
    const apply = root.querySelector('.merge-apply');
    function clearResult() {
      resultNode.replaceChildren();
      for (const [role,fs,unit] of [['cpu',['cpu1c'],'单核 %'],['rss',['rss_kb'],'MB'],['io',io,'KB/周期']]) {
        plotCompared('merge-'+role,[],fs,unit);
      }
    }
    const checks = new Map();
    for (const p of ps) {
      const label = el('label'), check = el('input'); check.type = 'checkbox';
      check.value = p.id;
      label.append(check,el('span',p.name + ' · PID ' + p.pids.join(', ') + (p.dp_only ? ' · DP-only' : '')));
      check.addEventListener('change',() => {
        check.checked ? selected.add(p.id) : selected.delete(p.id); refreshSelection();
      });
      candidates.append(label); checks.set(p.id,{check,label});
    }
    function refreshSelection() {
      selectionVersion++;
      chosenList.replaceChildren();
      for (const p of ps) checks.get(p.id).check.checked = selected.has(p.id);
      for (const id of selected) {
        const p = byId.get(id), row = el('div'), remove = el('button','移除'); remove.type = 'button';
        remove.setAttribute('aria-label','移除合并成员 ' + p.name);
        remove.addEventListener('click',() => { selected.delete(id); refreshSelection(); });
        row.append(el('span',p.name),remove); chosenList.append(row);
      }
      root.querySelector('.merge-count').textContent = selected.size;
      apply.disabled = selected.size === 0;
      clearResult(); status.textContent = selected.size ? '选择已变更，请重新计算。' : '请选择同段进程；无选择不生成零值统计。';
    }
    search.addEventListener('input',() => {
      const query = search.value.trim().toLocaleLowerCase();
      checks.forEach(({label}) => { label.hidden = !label.textContent.toLocaleLowerCase().includes(query); });
    });
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
            el('h4','合并后有效单核 CPU > 0 的活跃统计'),mergeTable(result,system.cycles,true));
          const complete = result.points.filter(p => fields.every(f => valid(p[f]))).length;
          status.textContent = '已对齐 '+system.cycles+' 个周期；七字段完整 '+complete+'；活跃 '+result.active.length+'。图表最多600点，统计未采样。';
          plotCompared('merge-cpu',[aggregate],['cpu1c'],'单核 %');
          plotCompared('merge-rss',[aggregate],['rss_kb'],'MB');
          plotCompared('merge-io',[aggregate],io,'KB/周期');
        } catch (error) { status.textContent = '合并失败：'+error.message; }
        finally { apply.disabled = selected.size === 0; }
      },0);
    });
    refreshSelection();
  }
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