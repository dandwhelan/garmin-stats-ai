import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const script = readFileSync(fileURLToPath(new URL('../src/garmin_insights/web/static/i18n.js', import.meta.url)), 'utf8');

function setup(lang, saved) {
  const text = {nodeType:3,nodeValue:' Dashboard ',parentElement:{closest:()=>null}};
  const note = {nodeType:3,nodeValue:'Dashboard',parentElement:{closest:()=>({})}};
  const body = {nodeType:1,childNodes:[text,note],closest:()=>null,hasAttribute:()=>false};
  const events = {}, select = {value:'',addEventListener:(k,f)=>events[k]=f};
  const storage = {getItem:()=>saved,setItem:(key,value)=>{storage.saved=[key,value]}};
  let reloads=0,observer,plugin;
  const document = {documentElement:{dataset:{}},body,title:'Dashboard',getElementById:()=>select};
  const window = {GARMIN_ZH_CN:{Dashboard:'总览','Heart Rate':'心率'},location:{reload:()=>reloads++}};
  class Observer {constructor(cb){observer=cb} observe(){}}
  vm.runInNewContext(script,{window,document,navigator:{language:lang},localStorage:storage,
    MutationObserver:Observer,Node:{TEXT_NODE:3,ELEMENT_NODE:1},Chart:{register:p=>plugin=p}});
  return {text,note,select,events,storage,document,window,getReloads:()=>reloads,getObserver:()=>observer,getPlugin:()=>plugin};
}

test('Chinese UI, dynamic labels and chart labels; notes stay untouched',()=>{
  const x=setup('zh-CN');
  assert.equal(x.document.documentElement.lang,'zh-CN');
  assert.equal(x.text.nodeValue,' 总览 ');assert.equal(x.note.nodeValue,'Dashboard');
  assert.equal(x.window.healthTranslate('2 charts'),'2 个图表');
  assert.equal(x.window.healthTranslate('90-Day Trend'),'90 天趋势');
  assert.equal(x.window.healthTranslate('Sleep Architecture (30-Day)'),'睡眠结构（30 天）');
  assert.equal(x.window.healthTranslate('Current SRI: 75.0 / 100'),'当前睡眠规律性（近似）：75.0 / 100');
  const chart={data:{datasets:[{label:'Heart Rate'}]},options:{scales:{}}};
  x.getPlugin().beforeInit(chart);assert.equal(chart.data.datasets[0].label,'心率');
  const added={nodeType:3,nodeValue:'Heart Rate',parentElement:{closest:()=>null}};
  x.getObserver()([{type:'childList',addedNodes:[added]}]);assert.equal(added.nodeValue,'心率');
});

test('saved English preference takes precedence and switching persists',()=>{
  const x=setup('zh-CN','en');assert.equal(x.text.nodeValue,' Dashboard ');
  assert.equal(x.window.healthTranslate('2 charts'),'2 charts');
  x.select.value='zh-CN';x.events.change();
  assert.deepEqual(x.storage.saved,['garmin-ui-language','zh-CN']);assert.equal(x.getReloads(),1);
});
