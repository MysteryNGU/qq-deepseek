"""Deterministic current-runtime checks, with no production DB or QQ access."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8',QQ_OFFLINE='1')
    commands = [
        ['-m','unittest','discover','-s','tests','-q'],
        ['drivetest.py'],
    ]
    failed = []
    for arguments in commands:
        result = subprocess.run([sys.executable,'-B','-X','utf8',*arguments],cwd=ROOT,env=env,
                                capture_output=True,timeout=180)
        output = (result.stdout+result.stderr).decode('utf-8','replace')
        label = ' '.join(arguments)
        print(label+': '+str(result.returncode),flush=True)
        print('\n'.join(output.splitlines()[-5:]),flush=True)
        if result.returncode:
            failed.append(label)
            print(output[-5000:],flush=True)
    return int(bool(failed))


if __name__=='__main__':
    raise SystemExit(main())
