'use strict';
window.setupCompactReport = function(system, shared) {
  const {data, valid, fmt, scaleMetric, metric, exactStats, processLabel, el, draw, setupTable} = shared;
  // Use the report's exact decimal helpers; a floating-point fallback changes results.
  if (![scaleMetric,metric,exactStats,processLabel].every(fn => typeof fn === 'function')) {
    throw new Error('简洁报告需要共享的真实十进制计算、统计及PID helpers');
  }
  for (const [field,column] of [['cpu_total',2],['mem_used_mb',3]]) {
    system.metrics[field] = exactStats(system.chart_samples.map(row => ({[field]:row[column]})),field);
  }
  const root = document.getElementById(system.id);
  const node = role => document.getElementById(system.id + '-' + role);
  const processes = data.processes.filter(p => p.segment === system.segment);
  const colors = ['#2878cc','#db6445','#2e956b','#9261c4','#bb851d','#c45186','#298f9b','#687bc2'];
  function plot(role, series, unit, tooltip) {
    draw(node(role), {series, textStyle:{color:'#334155'}, legend:{show:false},
      ...(tooltip ? {tooltip} : {}),
      yAxis:{type:'value',name:unit,min:0,axisLabel:{formatter:fmt,color:'#576a87'},splitLine:{lineStyle:{color:'#e6eaf0'}}},
      title:series.length ? undefined : {text:'暂无有效数据',left:'center',top:'center',textStyle:{color:'#65748a',fontSize:14}}});
  }
  const sampleIndex = new Map();
  for (const item of [system,...processes]) {
    const times = new Map();
    for (const row of item.chart_samples) {
      if (!valid(row[1])) continue;
      if (!times.has(row[1])) times.set(row[1],new Map());
      const cycles = times.get(row[1]);
      // Duplicate system observations are ambiguous, never silently overwritten.
      cycles.set(row[0],cycles.has(row[0]) ? null : row);
    }
    sampleIndex.set(item.id,times);
  }
  function sampleTooltip(entries, column, unit, chart) {
    return {trigger:'axis',renderMode:'html',confine:false,appendToBody:true,enterable:true,displayTransition:false,transitionDuration:0,
      backgroundColor:'#ffffff',borderColor:'#dce3ed',borderWidth:1,padding:0,
      textStyle:{color:'#243247',fontSize:12},
      extraCssText:'border-radius:12px;box-shadow:0 12px 36px #24324726;white-space:normal;',
      position(point, params, dom, rect, size) {
        const bounds = chart.getBoundingClientRect(), gap = 12;
        const width = document.documentElement.clientWidth, height = document.documentElement.clientHeight;
        let x = bounds.left + point[0] + gap;
        if (x + size.contentSize[0] > width-gap) x = bounds.left + point[0] - size.contentSize[0] - gap;
        const y = Math.max(gap,Math.min(bounds.top + point[1] - 24,height - size.contentSize[1] - gap));
        return [Math.max(gap,Math.min(x,width - size.contentSize[0] - gap))-bounds.left,y-bounds.top];
      },
      formatter(params) {
        const ts = Number(params[0]?.axisValue);
        if (!valid(ts)) return '';
        const cycles = new Set();
        for (const item of [system,...processes]) {
          sampleIndex.get(item.id).get(ts)?.forEach((_,cycle) => cycles.add(cycle));
        }
        const cycle = cycles.size === 1 ? [...cycles][0] : null;
        const box = el('div',null,'compact-tooltip');
        box.append(el('strong',new Date(ts).toLocaleString()+'.'+String(new Date(ts).getMilliseconds()).padStart(3,'0')+
          ' · 周期 '+(cycle ?? '不明确'),'compact-tooltip-heading'));
        const list = el('div',null,'compact-tooltip-list');
        for (const entry of entries) {
          const row = cycle === null ? null : sampleIndex.get(entry.item.id).get(ts)?.get(cycle);
          const value = row && valid(row[column]) ? scaleMetric(row[column],1,entry.divisor || 1) : null;
          const line = el('div',null,'compact-tooltip-row'+(entry.item.id === system.id ? ' is-reference' : ''));
          const dot = el('i',null,'compact-tooltip-dot');
          dot.style.backgroundColor = entry.color;
          line.append(dot,el('span',entry.name,'compact-tooltip-name'),
            el('b',valid(value) ? fmt(value)+' '+unit : '缺失或冲突',
              'compact-tooltip-value'+(valid(value) ? '' : ' is-missing')));
          list.append(line);
        }
        box.append(list);
        return box;
      }};
  }
  function cpuTooltip(items, chart, entries) {
    return sampleTooltip(entries || [
      {item:system,name:'整机总 CPU',color:'#2878cc'},
      ...items.filter(p => p.id !== system.id).map((p,i) => ({item:p,name:processLabel(p),color:colors[i%colors.length]}))
    ],2,'%',chart);
  }
  function curves(item, field, name, color, divisor=1, references=false) {
    const column = field === 'cpu1c' || field === 'cpu_total' ? 2 : 3;
    const series = [];
    let previous = null, group = null;
    for (const row of item.chart_samples) {
      if (!valid(row[1]) || !valid(row[column])) { previous = null; group = null; continue; }
      if (!previous || row[0] !== previous[0]+1 || row[1] <= previous[1]) {
        group = {name,type:'line',data:[],showSymbol:true,symbolSize:3,connectNulls:false,
          lineStyle:{width:1,color},itemStyle:{color},emphasis:{focus:'series'},animation:false};
        series.push(group);
      }
      group.data.push([row[1],scaleMetric(row[column],1,divisor),row[0]]);
      previous = row;
    }
    if (references && series.length) {
      const reference = key => metric(item,field,key);
      series[0].markLine = {silent:true,symbol:'none',animation:false,
        data:['p95','p99'].filter(k => valid(reference(k))).map((k,i) => ({
          name:k.toUpperCase(),yAxis:reference(k),
          lineStyle:{color:i ? '#c45186' : '#bb851d',type:'dashed'},
          label:{show:true,position:i ? 'insideEndTop' : 'insideStartTop',
            formatter:k.toUpperCase()+': '+fmt(reference(k))}
        }))};
    }
    return series;
  }
  plot('system-cpu',curves(system,'cpu_total','整机 CPU','#2878cc',1,true),'单核 %',cpuTooltip(processes,node('system-cpu')));
  plot('memory',curves(system,'mem_used_mb','整机已用内存','#2e956b',1,true),'MB',
    sampleTooltip([{item:system,name:'整机已用内存',color:'#2e956b'}],3,'MB',node('memory')));
  function combination(role, field, systemField, unit, divisor) {
    const eligible = processes.filter(p => !/^\[.*\]$/.test(p.name.trim()));
    eligible.sort((a,b) => (metric(b,field,'avg') ?? -Infinity) -
      (metric(a,field,'avg') ?? -Infinity) || a.name.localeCompare(b.name));
    const entries = [{item:system,field:systemField,name:'整机',color:'#202c40',divisor:1},
      ...eligible.map((p,i) => ({item:p,field,
        name:processLabel(p),
        search:p.name+' '+p.pids.join(', '),color:colors[i%colors.length],divisor}))];
    const selected = new Set(entries.slice(0,16).map(e => e.item.id));
    const controls = root.querySelector('[data-role="'+role+'"] .legend-controls');
    const actions = el('div',null,'legend-actions'), search = el('input');
    search.type = 'search'; search.placeholder = '搜索进程或 PID'; search.setAttribute('aria-label',unit+'组合图搜索');
    const count = el('span',null,'legend-count'), list = el('div',null,'legend-list');
    const boxes = [];
    function refresh() {
      boxes.forEach(({box,entry}) => { box.checked = selected.has(entry.item.id); });
      count.textContent = '已选 '+selected.size+' / '+entries.length+' 条';
      plot(role,entries.filter(e => selected.has(e.item.id)).flatMap(e =>
        curves(e.item,e.field,e.name,e.color,e.divisor)),unit,
        role === 'combined-cpu' ? cpuTooltip(eligible,node(role),entries) :
          sampleTooltip(entries.filter(e => selected.has(e.item.id)),3,'MB',node(role)));
    }
    [['全显',entries.length],['全隐',0],['仅前 15 + 整机',16]].forEach(([label,size]) => {
      const button = el('button',label); button.type = 'button';
      button.addEventListener('click',() => {
        selected.clear(); entries.slice(0,size).forEach(e => selected.add(e.item.id)); refresh();
      });
      actions.append(button);
    });
    for (const entry of entries) {
      const label = el('label'), box = el('input'), text = el('span',entry.name);
      box.type = 'checkbox'; text.style.color = entry.color;
      box.addEventListener('change',() => {
        if (box.checked) selected.add(entry.item.id); else selected.delete(entry.item.id);
        refresh();
      });
      label.append(box,text); list.append(label); boxes.push({box,entry,label});
    }
    search.addEventListener('input',() => {
      const query = search.value.trim().toLocaleLowerCase();
      boxes.forEach(({entry,label}) => { label.hidden = !(entry.search || entry.name).toLocaleLowerCase().includes(query); });
    });
    actions.prepend(search); actions.append(count); controls.append(actions,list); refresh();
  }
  combination('combined-cpu','cpu1c','cpu_total','单核 %',1);
  combination('combined-memory','rss_kb','mem_used_mb','MB',1024);
  const byId = new Map(processes.map(p => [p.id,p]));
  function select(id, focus=true) {
    const process = byId.get(id);
    if (!process) return;
    root.querySelector('.detail-title').textContent = processLabel(process);
    root.querySelectorAll('tr[data-process]').forEach(row => {
      const active = row.dataset.process === id;
      row.classList.toggle('selected',active); row.setAttribute('aria-selected',String(active));
    });
    plot('detail-cpu',curves(process,'cpu1c','CPU','#2878cc',1,true),'单核 %',cpuTooltip([process],node('detail-cpu')));
    plot('detail-memory',curves(process,'rss_kb','RSS','#2e956b',1024,true),'MB',
      sampleTooltip([{item:process,name:processLabel(process),color:'#2e956b',divisor:1024}],3,'MB',node('detail-memory')));
    if (focus) {
      const detail = root.querySelector('.process-detail');
      detail.focus({preventScroll:true}); detail.scrollIntoView({block:'nearest',behavior:'smooth'});
    }
  }
  root.querySelectorAll('[data-table]').forEach(wrap => setupTable(wrap,select));
  if (processes.length) select(processes[0].id,false);
  else {
    plot('detail-cpu',[],'单核 %'); plot('detail-memory',[],'MB');
    root.querySelector('.detail-title').textContent = '没有已采集进程';
  }
};