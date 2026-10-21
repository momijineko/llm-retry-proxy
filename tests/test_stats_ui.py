import re
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required for UI tests')
class StatsUiTests(unittest.TestCase):
    def test_busy_response_preserves_existing_statistics(self):
        html = (Path(__file__).resolve().parents[1] / 'stats.html').read_text()
        load = re.search(r"async function load\(\)\{.*?\n\}\n", html, re.S).group(0)
        script = """
const assert=require('node:assert/strict');
const lastUpdate={textContent:'previous'};
const document={getElementById(id){
  assert.equal(id,'lastUpdate');return lastUpdate;
}};
const currentRange='today',selectedProviders=new Set(),selectedModels=new Set();
const getPlanStart=()=>'',getRateMode=()=>'';
const window={location:{origin:'https://test.invalid'}};
const fetch=async()=>({ok:false,status:503,json:async()=>({detail:'分析进行中，请稍后重试'})});
""" + load + """
load().then(()=>assert.equal(lastUpdate.textContent,'加载失败: 分析进行中，请稍后重试'));
"""
        result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upstream_model_badges_and_table_render_declared_names(self):
        html = (Path(__file__).resolve().parents[1] / 'stats.html').read_text()
        start = html.index('function renderAggTable(')
        end = html.index('function keyHealthMeta(')
        snippet = html[start:end]
        script = """
const assert=require('node:assert/strict');
const nodes=new Map();
const document={getElementById(id){
  if(!nodes.has(id))nodes.set(id,{innerHTML:''});
  return nodes.get(id);
}};
const esc=s=>String(s||'').replace(/[<>&"']/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]));
const pctPill=p=>String(p),fmtTok=n=>String(n),fmtTokFull=n=>String(n);
const cachedCell=()=>'-',CATCOL={'2xx':'#111','4xx':'#222','5xx':'#333'};
""" + snippet + """
upstreamModelsByModel={};
upstreamModelsByModel['test/deepseek-v4.1-flash']=[
  {name:'deepseek/deepseek-flash',requests:7},
  {name:'opencode/deepseek-v4.1-flash',requests:3},
];
renderModelTable('modelTbody',[{model:'test/deepseek-v4.1-flash',requests:10,retries:0,
  avg_retries:0,max_retries:0,upstream_availability_pct:100,availability_pct:100,
  total_tokens:1,cached_tokens:0,prompt_tokens:1,p95_duration:1,
  dominant_fail_status:null,fail_status_count:0}],'model');
const out=nodes.get('modelTbody').innerHTML;
assert(out.includes('deepseek/deepseek-flash'));
assert(out.includes('opencode/deepseek-v4.1-flash'));
assert.equal((out.match(/model-declared/g)||[]).length,2);
assert(out.includes('7 次请求'));
// A model with no declaration keeps the plain cell.
renderModelTable('otherTbody',[{model:'test/plain',requests:1,retries:0,avg_retries:0,
  max_retries:0,upstream_availability_pct:100,availability_pct:100,total_tokens:0,
  cached_tokens:0,prompt_tokens:0,p95_duration:0,dominant_fail_status:null,
  fail_status_count:0}],'model');
assert(!nodes.get('otherTbody').innerHTML.includes('model-declared'));
// Declared names are escaped before reaching the DOM.
upstreamModelsByModel['test/x']=[{name:'<img src=x>',requests:1}];
renderModelTable('escapeTbody',[{model:'test/x',requests:1,retries:0,avg_retries:0,
  max_retries:0,upstream_availability_pct:100,availability_pct:100,total_tokens:0,
  cached_tokens:0,prompt_tokens:0,p95_duration:0,dominant_fail_status:null,
  fail_status_count:0}],'model');
assert(!nodes.get('escapeTbody').innerHTML.includes('<img'));
"""
        result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
