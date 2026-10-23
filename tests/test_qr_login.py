import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows QR login window')
class QrLoginTests(unittest.TestCase):
    def test_real_window_refresh_closes_on_login_without_locking_napcat_image(self):
        root = Path(__file__).resolve().parents[1]
        powershell = Path(os.environ['WINDIR'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run(
            [str(powershell), '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
             '-File', str(root/'tests/qr_login_probe.ps1'), '-Root', str(root)],
            cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('QR probe passed', result.stdout)
