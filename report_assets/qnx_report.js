/* QNX-only offline charts. Log strings are data, never executable markup. */
(() => {
  'use strict';
  const offset = 8 * 3600 * 1000;
  function timeLabel(ts, full = false) {
    const date = new Date(ts + offset);
    const iso = date.toISOString();
    if (iso.startsWith('+')) return new Date(ts).toISOString() + ' UTC';
    return full ? iso.replace('T', ' ').replace('Z', ' UTC+8') : iso.slice(5, 23).replace('T', '\n');
  }
  const fmt = value => Number.isFinite(value) ? Number(value.toPrecision(8)).toString() : '—';
  const states = new Map();
  function option(item) {
    // Normalize only extreme plotting coordinates to avoid chart-engine overflow.
    // Tick labels and tooltips continue to use the original values and units.
    const scale = item.max > 1e100 ? item.max : 1;
    const valid = [], missing = [];
    let previous = null;
    for (const point of item.points) {
      const x = (point.ts - item.start) / 1000;
      if (point.value === null) {
        missing.push({value:[x, 0], point});
        previous = null;
        valid.push([x, null]);
      } else {
        if (previous && (point.run !== previous.run || point.ts <= previous.ts)) valid.push([x, null]);
        valid.push({value:[x, point.value / scale], point});
        previous = point;
      }
    }
    const labels = value => timeLabel(item.start + value * 1000);
    return {
      animation:false, backgroundColor:'transparent', textStyle:{color:'#52647a'},
      grid:{left:24, right:28, top:40, bottom:100, containLabel:true},
      legend:{data:['观测值', '显式空值（非零）'], top:5, textStyle:{color:'#52647a'}},
      tooltip:{trigger:'item', renderMode:'html', confine:true, formatter:params => {
        const point = params.data.point;
        const box = document.createElement('div');
        box.style.cssText = 'white-space:normal;overflow-wrap:anywhere;max-width:480px;line-height:1.6';
        if (!point) return box;
        const lines = [item.module + '/' + item.metric + ' · 分段 ' + item.segment,
          timeLabel(point.ts, true),
          point.value === null ? '显式空值（非零）' : fmt(point.value) + ' ' + item.unit,
          'qnx_relative_s=' + (point.qnx_relative_s === null ? '—' : fmt(point.qnx_relative_s)),
          point.source + ':' + point.line];
        for (const text of lines) {
          const row = document.createElement('div');
          row.textContent = text;
          box.append(row);
        }
        return box;
      }},
      xAxis:{type:'value', min:0, max:Math.max((item.end - item.start) / 1000, 0.001),
        name:'北京时间 UTC+8', nameLocation:'middle', nameGap:55,
        axisLabel:{formatter:labels, hideOverlap:true}, splitLine:{show:false}},
      yAxis:{type:'value', min:0, max:scale !== 1 ? 1 : undefined,
        axisLabel:{formatter:value => fmt(value * scale)}, splitLine:{lineStyle:{color:'#e1e7ee'}}},
      dataZoom:[{type:'inside', filterMode:'none'},
        {type:'slider', filterMode:'none', bottom:8, height:22, labelFormatter:labels}],
      series:[{type:'line', name:'观测值', data:valid, connectNulls:false, smooth:false,
        showSymbol:true, symbolSize:5, clip:true, lineStyle:{width:2, color:'#2563b9'},
        itemStyle:{color:'#2563b9'}, emphasis:{scale:1.5}},
      {type:'scatter', name:'显式空值（非零）', data:missing, symbol:'diamond', symbolSize:9,
        itemStyle:{color:'#bb7000'}}]
    };
  }
  function activate(node) {
    const state = states.get(node);
    if (!state || state.chart || !node.clientWidth) return;
    let chart;
    try {
      const item = JSON.parse(document.getElementById(node.dataset.data).textContent);
      chart = echarts.init(node, null, {renderer:'canvas'});
      chart.setOption(option(item));
      if (state.zoom) chart.dispatchAction({type:'dataZoom', start:state.zoom.start, end:state.zoom.end});
      chart.on('datazoom', () => {
        const zoom = chart.getOption().dataZoom[0];
        state.zoom = {start:zoom.start, end:zoom.end};
      });
      chart.on('legendselectchanged', event => { state.selected = event.selected; });
      if (state.selected) chart.setOption({legend:{selected:state.selected}});
      state.chart = chart;
      node.parentElement.classList.add('chart-ready');
    } catch (_) {
      if (chart) chart.dispose();
      node.parentElement.classList.remove('chart-ready');
      node.textContent = '交互图加载失败，以下保留静态图和全量统计。';
    }
  }
  function dispose(node) {
    const state = states.get(node);
    if (state.chart) state.chart.dispose();
    state.chart = null;
    node.parentElement.classList.remove('chart-ready');
  }
  const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      states.get(entry.target).visible = entry.isIntersecting;
      if (entry.isIntersecting) activate(entry.target);
      else dispose(entry.target);
    }
  }, {rootMargin:'240px'}) : null;
  const sizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(scheduleResize) : null;
  for (const node of document.querySelectorAll('.qnx-chart')) {
    states.set(node, {chart:null, zoom:null, selected:null, visible:false});
    if (sizeObserver) sizeObserver.observe(node);
    const reset = node.parentElement.querySelector('button');
    reset.addEventListener('click', () => {
      const state = states.get(node);
      state.zoom = null;
      if (state.chart) state.chart.dispatchAction({type:'dataZoom', start:0, end:100});
      else activate(node);
    });
    if (observer) observer.observe(node);
    else reset.textContent = '加载 / 重置图表';
  }
  let resizePending = false;
  function scheduleResize() {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      resizePending = false;
      for (const [node, state] of states) {
        if (state.chart) state.chart.resize();
        else if (state.visible) activate(node);
      }
    });
  }
  window.addEventListener('resize', scheduleResize);
})();