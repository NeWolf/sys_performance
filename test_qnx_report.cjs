'use strict';
// Offline runtime checks: node test_qnx_report.cjs (no browser/dependencies needed).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const echarts = require('./report_assets/vendor/echarts.min.js');
const script = fs.readFileSync(require.resolve('./report_assets/qnx_report.js'), 'utf8');
const malicious = '<img src=x onerror=alert(1)>';
const point = (ts, value, run = 1) => ({ts, value, run, source:malicious, line:2, qnx_relative_s:ts / 1000});
const item = {start:0, end:5000, max:50, module:'cpu', metric:'totalcpu', segment:0,
  unit:malicious, points:[point(0, 0), point(1000, 10), point(2000, 20, 2),
    point(3000, null, 3), point(4000, 40, 4), point(5000, 50, 4)]};
function harness(data = item, useObserver = true, failInit = false, useSizeObserver = true) {
  const classes = new Set(), charts = [], frames = [], events = {};
  const button = {addEventListener:(_, fn) => { button.click = fn; }};
  const node = {clientWidth:900, dataset:{data:'payload'}, parentElement:{
    classList:{add:name => classes.add(name), remove:name => classes.delete(name)},
    querySelector:() => button}};
  const h = {classes, charts, frames, events, button, node};
  const context = {
    document:{querySelectorAll:() => [node], getElementById:() => ({textContent:JSON.stringify(data)}),
      createElement:() => ({style:{}, children:[], append(child) { this.children.push(child); },
        set innerHTML(_) { throw Error('unsafe HTML'); }})},
    window:{addEventListener:(name, fn) => { events[name] = fn; }},
    requestAnimationFrame:fn => { frames.push(fn); },
    echarts:{init:() => {
      const chart = {handlers:{}, actions:[], disposed:false, resized:0,
        setOption(opt) {
          if (failInit) throw Error('simulated renderer failure');
          if (opt.series) this.option = opt;
          else this.selection = opt.legend.selected;
        },
        on(name, fn) { this.handlers[name] = fn; },
        getOption() { return {dataZoom:[this.zoom || {start:0, end:100}]}; },
        dispatchAction(action) { this.actions.push(action); this.zoom = action; },
        dispose() { this.disposed = true; }, resize() { this.resized++; }};
      charts.push(chart);
      return chart;
    }}
  };
  if (useObserver) context.IntersectionObserver = class {
    constructor(callback, options) { h.intersect = visible => callback([{target:node, isIntersecting:visible}]);
      assert.equal(options.rootMargin, '240px'); }
    observe(target) { assert.equal(target, node); }
  };
  if (useSizeObserver) context.ResizeObserver = class {
    constructor(callback) { h.resizeContainer = () => callback([{target:node}]); }
    observe(target) { assert.equal(target, node); }
  };
  vm.runInNewContext(script, context);
  return h;
}
const h = harness();
assert.equal(h.charts.length, 0, 'charts must load lazily');
h.intersect(true);
const chart = h.charts[0], option = chart.option;
assert(h.classes.has('chart-ready'));
assert.equal(option.series[0].connectNulls, false);
assert.equal(option.series[0].smooth, false);
const line = option.series[0].data;
assert.equal(line[0].value[1], 0, 'zero is a real observation');
assert.equal(line[2][1], null, 'unsampled discontinuity inserts a gap');
assert.equal(line[4][1], null, 'explicit null breaks the line');
assert.equal(option.series[1].data.length, 1);
assert.equal(option.series[1].data[0].point.value, null);
assert(option.xAxis.axisLabel.formatter(0).includes('08:00:00'));
const tooltip = option.tooltip.formatter({data:line[0]});
assert(tooltip.children[1].textContent.includes('1970-01-01 08:00:00.000 UTC+8'));
assert.equal(tooltip.children[2].textContent, '0 ' + malicious);
assert.equal(tooltip.children[4].textContent, malicious + ':2');
assert.equal(option.tooltip.formatter({data:option.series[1].data[0]}).children[2].textContent, '显式空值（非零）');
chart.zoom = {start:20, end:60};
chart.handlers.datazoom();
chart.handlers.legendselectchanged({selected:{'观测值':false}});
h.intersect(false);
assert(chart.disposed);
assert(!h.classes.has('chart-ready'));
h.intersect(true);
const restored = h.charts[1];
assert.equal(restored.actions[0].start, 20);
assert.equal(restored.actions[0].end, 60);
assert.equal(restored.selection['观测值'], false);
h.button.click();
assert.equal(restored.actions.at(-1).start, 0);
assert.equal(restored.actions.at(-1).end, 100);
h.events.resize(); h.events.resize();
assert.equal(h.frames.length, 1);
h.frames.shift()();
assert.equal(restored.resized, 1);
const manual = harness(item, false);
assert.equal(manual.charts.length, 0);
assert.equal(manual.button.textContent, '加载 / 重置图表');
manual.button.click();
assert.equal(manual.charts.length, 1);
const failure = harness(item, true, true);
failure.intersect(true);
assert(failure.charts[0].disposed);
assert(!failure.classes.has('chart-ready'));
assert(failure.node.textContent.includes('静态图'));
const zeroWidth = harness();
zeroWidth.node.clientWidth = 0;
zeroWidth.intersect(true);
assert.equal(zeroWidth.charts.length, 0);
zeroWidth.resizeContainer();
zeroWidth.frames.shift()();
assert.equal(zeroWidth.charts.length, 0, 'still zero-width: no initialization');
zeroWidth.node.clientWidth = 900;
zeroWidth.resizeContainer(); zeroWidth.events.resize();
assert.equal(zeroWidth.frames.length, 1, 'container and window resize share throttling');
zeroWidth.frames.shift()();
assert.equal(zeroWidth.charts.length, 1, 'visible chart loads when container width recovers');
zeroWidth.intersect(false);
zeroWidth.resizeContainer();
zeroWidth.frames.shift()();
assert.equal(zeroWidth.charts.length, 1, 'offscreen chart stays disposed on resize');
const resizeFallback = harness(item, true, false, false);
resizeFallback.node.clientWidth = 0;
resizeFallback.intersect(true);
resizeFallback.node.clientWidth = 900;
resizeFallback.events.resize();
resizeFallback.frames.shift()();
assert.equal(resizeFallback.charts.length, 1, 'window resize recovers without ResizeObserver');
const unobserved = harness(item, false);
unobserved.resizeContainer();
unobserved.frames.shift()();
assert.equal(unobserved.charts.length, 0, 'without visibility observer loading remains manual');
const extremeItem = {...item, end:253402300799999, max:1.7e308,
  points:[point(0, 1.7e308), point(253402300799999, 1.7e308)]};
const extreme = harness(extremeItem);
extreme.intersect(true);
const extremeOption = extreme.charts[0].option;
assert.equal(extremeOption.series[0].data[0].value[1], 1);
assert.equal(extremeOption.yAxis.axisLabel.formatter(1), '1.7e+308');
const extremeTip = extremeOption.tooltip.formatter({data:extremeOption.series[0].data[1]});
assert.equal(extremeTip.children[1].textContent, '9999-12-31T23:59:59.999Z UTC');
// Exercise the actual bundled ECharts engine offline, not only the lifecycle stub.
for (const options of [option, extremeOption]) {
  const real = echarts.init(null, null, {renderer:'svg', ssr:true, width:1000, height:360});
  try {
    real.setOption(options);
    const svg = real.renderToSVGString();
    assert(svg.includes('<svg'));
    assert(!svg.includes('NaN'));
    assert(!svg.includes('Infinity'));
    assert(!svg.includes(malicious));
    real.dispatchAction({type:'dataZoom', start:20, end:80});
    assert(real.renderToSVGString().includes('<svg'));
  } finally { real.dispose(); }
}
console.log('QNX chart offline checks passed: data gaps, time, tooltip, zoom, lifecycle, fallback, ECharts SSR');

// Shared standard/compact report regressions; keep cycle identity even within one second.
{
  class ReportNode {
    constructor() { this.children=[]; this.style={}; this.dataset={}; }
    append(...nodes) { this.children.push(...nodes); }
    querySelector() { return this.children[0]; }
    querySelectorAll() { return []; }
    getClientRects() { return []; }
  }
  const payload = {session:{summary:{source_format:'top'}},processes:[],
    full_cycle_schema:[],cycle_axis_schema:[]};
  const context = {window:{},document:{
    getElementById:() => ({textContent:JSON.stringify(payload)}),
    createElement:() => new ReportNode(),documentElement:{classList:{add(){}}}}};
  let source = fs.readFileSync(require.resolve('./report_assets/report.js'),'utf8');
  source = source.slice(0,source.indexOf('  function setupFocusTable'));
  vm.runInNewContext(source + '\nwindow.api = {data,valid,fmt,scaleMetric,metric,exactStats,processLabel,el,draw,instances,lineSeries,fullTimeLabel,timeLabel,overlayStream,overlayTooltip};})();',context);
  const api = context.window.api, node = new ReportNode();
  const ts = new Date(2026,10,18,12,34,56,123).getTime();
  const points = [41,42,43].map((cycle,i) => ({segment:0,cycle,ts:ts+i*100,cpu1c:i,pids:[1]}));
  const series = api.lineSeries(points,'cpu1c','CPU','red');
  const snapshot = JSON.stringify(series);
  api.draw(node,{series});
  const opt = api.instances.get(node).option;
  assert.equal(JSON.stringify(series),snapshot);
  assert.equal(series.flatMap(s=>s.data).length,3);
  assert.equal(opt.xAxis.type,'value');
  assert.match(opt.xAxis.name,/真实时间.*周期等距/);
  for (const cycle of [41,42,43]) {
    assert.equal(opt.xAxis.axisLabel.formatter(cycle),'12:34:56');
    assert.equal(opt.dataZoom.find(z => z.type === 'slider').labelFormatter(cycle),'12:34:56');
    assert.equal(opt.xAxis.axisPointer.label.formatter({value:cycle}),'2026-11-18 12:34:56 · 周期 '+cycle);
  }
  for (const cycle of [40,41.5,44]) assert.equal(opt.xAxis.axisLabel.formatter(cycle),'');
  assert.equal(opt.dataZoom.length,1);
  assert.equal(opt.dataZoom[0].type,'slider');
  assert.equal(opt.dataZoom[0].filterMode,'none');
  const tip = opt.tooltip.formatter([{axisValue:42,seriesName:'CPU',data:[41,999,0]}]);
  assert.match(tip,/CPU：1.00/);
  assert.match(tip,/2026-11-18 12:34:56/);
  assert(!tip.includes('.223'));
  const overlay = api.overlayTooltip([{name:'CPU',field:'cpu1c',divisor:1,
    stream:api.overlayStream(points,['cpu1c'])}],'%',node);
  const shown = overlay.formatter([{axisValue:42}]);
  assert.equal(shown.children[0].textContent,'2026-11-18 12:34:56 · 周期 42');
  assert.equal(shown.children[1].children[0].children[2].textContent,'1.00 %');
  api.draw(node,{series:[{name:'a',data:[[41,1,ts]]},{name:'b',data:[[41,2,ts+100]]}]});
  assert.equal(api.instances.get(node).option.xAxis.axisLabel.formatter(41),'');
  assert.match(api.instances.get(node).option.xAxis.axisPointer.label.formatter({value:41}),/不唯一/);
  api.draw(node,{series:[],xAxis:{type:'category',data:['a']},dataZoom:[]});
  assert.equal(api.instances.get(node).option.xAxis.type,'category');
  assert.equal(api.instances.get(node).option.tooltip.formatter,undefined);
  let compact = fs.readFileSync(require.resolve('./report_assets/report_compact.js'),'utf8');
  compact = compact.slice(0,compact.indexOf("  plot('system-cpu'"));
  vm.runInNewContext(compact+'\nreturn {curves,sampleTooltip}; };',context);
  const process = {id:'proc',segment:0,chart_samples:points.map(p=>[p.cycle,p.ts,p.cpu1c,1024,p.pids])};
  const system = {id:'sys',segment:0,metrics:{},chart_samples:[[41,ts,10,1]]};
  api.data.processes = [process];
  const c = context.window.setupCompactReport(system,api);
  assert.equal(JSON.stringify(c.curves(process,'cpu1c','CPU','red').flatMap(s=>s.data)),
    JSON.stringify(series.flatMap(s=>s.data)));
  const ct = c.sampleTooltip([{item:process,name:'CPU',color:'red'}],2,'%',node);
  assert.equal(ct.formatter([{axisValue:42}]).children[0].textContent,shown.children[0].textContent);
  assert.equal(ct.formatter([{axisValue:42}]).children[1].children[0].children[2].textContent,'1.00 %');
  assert.equal(ct.formatter([{axisValue:40}]).children[1].children.length,0);
  const duplicate = {...process,chart_samples:[process.chart_samples[0],process.chart_samples[0]]};
  api.data.processes = [duplicate];
  const d = context.window.setupCompactReport(system,api);
  assert.equal(d.sampleTooltip([{item:duplicate,name:'CPU'}],2,'%',node)
    .formatter([{axisValue:41}]).children[1].children.length,0);
  // Verify the real bundled chart retains points and slider zoom, not only mocked options.
  const real = echarts.init(null,null,{renderer:'svg',ssr:true,width:1000,height:360});
  try {
    real.setOption(opt);
    assert(real.renderToSVGString().includes('12:34:56'));
    assert.equal(real.getModel().getSeriesByIndex(0).getData().count(),3);
    real.dispatchAction({type:'dataZoom',start:20,end:80});
    assert.equal(real.getOption().dataZoom.find(z => z.type === 'slider').start,20);
    assert.equal(real.getOption().dataZoom.some(z => z.type === 'inside'),false);
  } finally { real.dispose(); }
  for (const [file,cls] of [['report.css','overlay'],['report_compact.css','compact']]) {
    const css = fs.readFileSync(require.resolve('./report_assets/'+file),'utf8');
    const rule = css.match(new RegExp('\\.'+cls+'-tooltip-list\\{([^}]+)\\}'))[1];
    assert(rule.includes('overflow:auto'));
    assert(rule.includes('overscroll-behavior:auto'));
  }
  console.log('Standard/compact checks passed: real-second labels, exact cycles, tooltips, wheel policy, slider, ECharts SSR');
}