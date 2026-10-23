"""Use disposable Python children, never the real Bot or its stop script."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from life.supervisor import HostSupervisor

WATCHDOG = Path(__file__).resolve().parents[1]/'tools/bot-watchdog.ps1'


@unittest.skipUnless(os.name=='nt','Windows PowerShell outer watchdog')
class WatchdogTests(unittest.TestCase):
    def run_fixture(self, source):
        with tempfile.TemporaryDirectory(prefix='qq-watch-test-') as folder:
            root = Path(folder)
            (root/'supervise.py').write_text(source,encoding='utf-8')
            result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                '-File',str(WATCHDOG),'-Root',str(root),'-Python',sys.executable,'-RetryBaseSeconds','1'],
                capture_output=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            return (root/'data/logs/watchdog.log').read_text('utf-8'),(root/'count').read_text(),root

    def test_real_child_crash_is_logged_and_restarted(self):
        logs,count,_ = self.run_fixture('''from pathlib import Path
import os
p=Path(__file__).parent
c=p/'count'
n=int(c.read_text())+1 if c.exists() else 1
c.write_text(str(n))
if n==1: os._exit(9)
(p/'data/life-control/stop.requested').write_text('fixture intentional stop')
''')
        self.assertEqual(count,'2')
        self.assertIn('exit_code=9',logs)
        self.assertIn('retrying in 1s',logs)
        self.assertIn('Explicit stop requested',logs)

    def test_explicit_stop_prevents_restart_even_for_nonzero_exit(self):
        logs,count,_ = self.run_fixture('''from pathlib import Path
import os
p=Path(__file__).parent
(p/'count').write_text('1')
(p/'data/life-control/stop.requested').write_text('fixture intentional stop')
os._exit(17)
''')
        self.assertEqual(count,'1')
        self.assertIn('exit_code=17',logs)
        self.assertNotIn('retrying',logs)


class SupervisorRecoveryTests(unittest.TestCase):
    def test_quick_failures_cool_down_then_retry_instead_of_permanently_stopping(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = SimpleNamespace(control=Path(folder),queued_deployments=lambda:[])
            supervisor = HostSupervisor(controller,Path(folder)/'config.toml')
            supervisor.health = lambda:'failed'
            supervisor.failures = 2
            supervisor.tree = SimpleNamespace(process=SimpleNamespace(pid=123,poll=lambda:1))
            supervisor.stop = Mock(side_effect=lambda:setattr(supervisor,'tree',None))
            supervisor.start = Mock()
            with patch('life.supervisor.time.monotonic',return_value=100):
                supervisor.started = 90
                self.assertEqual(supervisor.tick(),'blocked')
            supervisor.start.assert_not_called()
            with patch('life.supervisor.time.monotonic',return_value=161):
                self.assertEqual(supervisor.tick(),'restarting')
            supervisor.start.assert_called_once()


if __name__=='__main__':
    unittest.main()
