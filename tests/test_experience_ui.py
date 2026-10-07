import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI tests")
class ExperienceUiTests(unittest.TestCase):
    def test_modes_show_relevant_fields_and_preserve_saved_configuration(self):
        html = (Path(__file__).resolve().parents[1] / "key_pool.html").read_text()
        prefixes = ("const experienceDefaults=", "function experienceTransform(",
                    "function updateExperienceRule(", "function fillExperienceForm(")
        script = "\n".join(line for line in html.splitlines()
                           if line.startswith(prefixes))
        harness = """
const assert=require('node:assert/strict');
const elements={};
const $=id=>elements[id]||=( {value:'',hidden:false} );
const document={getElementById:$};
const renderExperienceQueryParams=()=>{},experienceSourceSummary=()=>'',fmtTime=()=>'';
"""
        checks = """
fillExperienceForm({experience:{transform:{detection_mode:'pass_ratio',
pass_path:'checks.good',fail_path:'checks.bad',healthy_threshold:85,
warning_threshold:60,detection_map:{pass:'正常'}}}});
assert.equal($('experienceFieldRule').hidden,true);
assert.equal($('experienceRatioRule').hidden,false);
assert.equal(experienceTransform().healthy_threshold,85);
assert.equal(experienceTransform().detection_map.pass,'正常');
$('experienceDetectionMode').value='field';updateExperienceRule();
assert.equal($('experienceFieldRule').hidden,false);
assert.equal($('experienceRatioRule').hidden,true);
$('experienceDetectionMode').value='pass_ratio';updateExperienceRule();
assert.equal(experienceTransform().pass_path,'checks.good');
assert.equal(experienceTransform().warning_threshold,60);
fillExperienceForm({});
assert.equal($('experienceRatioRule').hidden,true);
assert.equal($('experienceFieldRule').hidden,false);
"""
        result = subprocess.run(["node", "-"], input=harness + script + checks,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
