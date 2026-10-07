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
