"""Execute request diagnostics UI logic against synthetic records only."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI logic tests")
class RequestDiagnosticsUiTests(unittest.TestCase):
    def test_prompt_filters_and_safe_bounded_rendering(self):
        html = (Path(__file__).resolve().parents[1] / "requests.html").read_text()
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("range.addEventListener('change',load);load();",
                                "range.addEventListener('change',load);")
        harness = r"""
const assert=require('node:assert/strict');
const nodes=new Map();
const document={getElementById(id){
  if(!nodes.has(id))nodes.set(id,{value:'',innerHTML:'',textContent:'',addEventListener(){}});
  return nodes.get(id);
}};
"""
        checks = r"""
assert.equal(extractPrompt({input:'plain prompt'}),'plain prompt');
assert.equal(extractPrompt({messages:[
  {role:'user',content:'old'},{role:'user',content:[{type:'text',text:'new'}]},
  {role:'assistant',content:'answer'},{role:'tool',content:'tool output'}
]}),'new');
assert.equal(extractPrompt({input:[
  {role:'user',content:[{type:'input_text',text:'look'},{type:'input_image'}]},
  {type:'function_call_output',output:'tool output'}
]}),'look'+String.fromCharCode(10)+'[图片]');
assert.equal(extractPrompt({prompt:'legacy'}),'legacy');
assert.equal(extractPrompt(null),'');
const record={model:'model-a',client_ip:'203.0.113.9',key_id:'sample-key',
  final_status:429,attempts:2,path:'/responses',
  request_body:{input:'Hello <script>bad</script>',system:'hidden diagnostic'}};
assert(matchesRecord(record,{status:'error',model:'model-a',ip:'113',key:'SAMPLE',keyword:'HELLO'}));
assert(matchesRecord(record,{status:'429'}));
assert(matchesRecord(record,{status:'retried'}));
assert(matchesRecord(record,{keyword:'hidden diagnostic'}));
assert(!matchesRecord(record,{status:'2xx'}));
assert(!matchesRecord(record,{model:'model-b'}));
assert(!matchesRecord(record,{ip:'192.0.2'}));
assert(!matchesRecord(record,{key:'other'}));
assert(!matchesRecord(record,{keyword:'absent'}));
assert(matchesRecord({final_status:0},{status:'error'}));
assert(!matchesRecord({final_status:200},{status:'error'}));
assert(matchesRecord({final_status:503},{status:'5xx'}));
assert(matchesRecord({},{key:'透传'}));
const row=render(record,3);
assert(row.includes('&lt;script&gt;'));
assert(!row.includes('<script>'));
assert(!row.includes('hidden diagnostic')); // JSON is populated on expansion only.
assert(row.includes('data-index="3"'));
const long=render({request_body:{input:'x'.repeat(50000)}},0);
assert(long.length<15000);
assert(long.includes('预览已截断'));
records=[{model:'other',request_body:{input:'first'}},record];
controls.model.value='model-a';
applyFilters();
assert(rows.innerHTML.includes('data-index="1"')); // Preserve original download index.
assert(!rows.innerHTML.includes('data-index="0"'));
assert.equal(count.textContent,'匹配 1 条用户消息 / 已加载 2 条请求');
controls.model.value='missing';applyFilters();
assert(rows.innerHTML.includes('没有匹配'));
"""
        result = subprocess.run(["node", "-"], input=harness + script + checks,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
