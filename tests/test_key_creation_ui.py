import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required for UI tests')
class KeyCreationUiTests(unittest.TestCase):
    def test_creation_errors_remain_visible_and_success_closes_dialog(self):
        html = (Path(__file__).resolve().parents[1] / 'key_pool.html').read_text()
        script = html.split('function showCreationErrors(', 1)[1]
        script = 'function showCreationErrors(' + script.split('async function clearKeys(', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
let closed=false, busy=false, state={}, groupCatalog=[], activeSource='test';
const errorNode={textContent:'',hidden:true};
const $=id=>id==='groupCreationErrors'?errorNode:{classList:{remove(){closed=true}}};
const setGroupBusy=value=>{busy=value};
const render=()=>{}, renderGroups=()=>{}, notice=()=>{};
const pollOperation=async request=>{try{await request}catch{}};
let response, failure, catalogFailure;
async function api(path, options){
  if(path.startsWith('catalog')){if(catalogFailure)throw new Error('catalog failed');return {groups:[{id:'ok',key_count:1}]}}
  assert.equal(JSON.parse(options.body).only_missing,true);
  if(failure)throw new Error(failure);
  return response;
}
'''
        checks = r'''
(async()=>{
  response={state:{},creation:{created:[{}],errors:[{group_name:'<b>group</b>',error:'quota denied'}]}};
  await createKeys(true);
  assert.equal(closed,false);assert.equal(busy,false);assert.equal(errorNode.hidden,false);
  assert(errorNode.textContent.includes('<b>group</b>：quota denied'));
  assert.equal(errorNode.innerHTML,undefined);
  assert.equal(groupCatalog[0].key_count,1);
  catalogFailure=true;await createKeys(true);
  assert(errorNode.textContent.includes('quota denied'));
  assert(errorNode.textContent.includes('catalog failed'));
  failure='HTTP 502';await createKeys(true);
  assert.equal(errorNode.textContent,'HTTP 502');assert.equal(busy,false);
  failure=null;response={state:{},creation:{created:[{}],errors:[]}};
  await createKeys(true);
  assert.equal(closed,true);assert.equal(errorNode.hidden,true);assert.equal(busy,false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-'], input=harness + script + checks,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
