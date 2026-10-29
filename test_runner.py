"""Exercise unattended failure/recovery with real state files and no network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
BASH = 'C:/Program Files/Git/bin/bash.exe' if Path('C:/Program Files/Git/bin/bash.exe').exists() else shutil.which('bash')

FAKE_COLLECTOR = '''import json, os
from pathlib import Path
counter = Path('attempts')
attempt = int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(attempt))
scenario = os.environ['SCENARIO']
failed = scenario in ('blocked','failed') or (scenario == 'recover' and attempt == 1)
state = {'status':'blocked' if scenario == 'blocked' else 'failed' if failed else 'succeeded',
         'complete':not failed,'newest_refresh_complete':not failed,'page':2120}
Path('../data/state.json').write_text(json.dumps(state))
raise SystemExit(1 if failed else 0)
'''

WRAPPER = '''#!/usr/bin/env bash
git() {
  if [[ "$*" == *"diff --cached --quiet"* ]]; then return 1; fi
  if [[ "$*" == *"push origin HEAD:data"* ]]; then echo push >> ../data/pushes; fi
  return 0
}
sleep() { echo "$1" >> ../data/waits; }
python() {
  if [ "$1" = collector.py ]; then
    "$TEST_PYTHON" "$TEST_COLLECTOR"
  else
    "$TEST_PYTHON" "$@"
  fi
}
source "$1"
'''


@unittest.skipUnless(BASH, 'Bash is required to test the GitHub runner')
class RunnerTests(unittest.TestCase):
    def exercise(self, scenario):
        scratch = ROOT / '.local'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            folder = Path(temporary)
            self.assertEqual(folder.resolve().parent, scratch.resolve())
            app, data = folder / 'app', folder / 'data'
            app.mkdir()
            data.mkdir()
            (app/'fake_collector.py').write_text(FAKE_COLLECTOR, encoding='utf-8')
            (app/'wrapper.sh').write_text(WRAPPER, encoding='utf-8', newline='\n')
            environment = {**os.environ,'SCENARIO':scenario,'BATCH_COUNT':'24',
                'COLLECT_SECONDS':'1','PDF_SECONDS':'0','TEST_PYTHON':Path(sys.executable).as_posix(),
                'TEST_COLLECTOR':(app/'fake_collector.py').as_posix()}
            result = subprocess.run([BASH,(app/'wrapper.sh').as_posix(),(ROOT/'runner.sh').as_posix()],
                cwd=app, env=environment, capture_output=True, text=True, timeout=30)
            attempts = int((app/'attempts').read_text())
            pushes = (data/'pushes').read_text().splitlines()
            waits = (data/'waits').read_text().splitlines() if (data/'waits').exists() else []
            state = json.loads((data/'state.json').read_text())
            return result, attempts, pushes, waits, state

    def test_transient_error_publishes_failure_then_recovers_same_batch(self):
        result, attempts, pushes, waits, state = self.exercise('recover')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(attempts,2)
        self.assertEqual(len(pushes),2)
        self.assertEqual(waits,['180'])
        self.assertEqual(state['page'],2120)
        self.assertEqual(state['status'],'succeeded')

    def test_access_restriction_stops_without_an_unattended_retry(self):
        result, attempts, pushes, waits, state = self.exercise('blocked')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertEqual(attempts,1)
        self.assertEqual(len(pushes),1)
        self.assertEqual(waits,[])
        self.assertEqual(state['status'],'blocked')

    def test_persistent_source_failure_is_bounded_and_retains_checkpoint(self):
        result, attempts, pushes, waits, state = self.exercise('failed')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertEqual(attempts,3)
        self.assertEqual(len(pushes),3)
        self.assertEqual(waits,['180','360'])
        self.assertEqual(state['page'],2120)
        self.assertEqual(state['status'],'failed')


if __name__ == '__main__':
    unittest.main()
