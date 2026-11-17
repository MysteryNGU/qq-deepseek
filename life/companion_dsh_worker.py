"""Isolated SDK execution worker; its parent owns the entire descendant tree."""
import json
import os
from pathlib import Path
import subprocess
import sys


class HiddenPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        if os.name == 'nt':
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


def execute(request, *, initialize_only=False, harness_class=None, config_class=None):
    root = Path(request['dsh_root'])
    if harness_class is None:
        sys.path.insert(0, str(root/'python/sdk/src'))
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
        harness_class, config_class = DeepSeekHarness, DeepSeekHarnessConfig
    selected_home = request.get('dsh_home') or str(Path(request['home'])/'data/dsh-home')
    Path(selected_home).mkdir(parents=True, exist_ok=True)
    config = config_class(provider=request['provider'], model=request['model'],
                          max_tokens=4096, cwd=request['cwd'], runtime_cwd=str(root),
                          profile='sdk-minimal', dsh_home=selected_home,
                          env=dict(DSH_HOME=selected_home),
                          initialize_timeout_seconds=90)
    entry = request.get('entry') or 'apps/cli/src/bin.ts'
    if entry.endswith('.ts'):
        launch = (request['node'], '--import', 'tsx/esm', entry, '--profile', 'sdk-minimal')
    else:
        launch = (request['node'], str(root/entry), '--profile', 'sdk-minimal')
    harness = harness_class(config, _launch_args=launch)
    try:
        if initialize_only:
            harness.start()
            print('DSH SDK initialized', flush=True)
        else:
            result = harness.run(request['goal'])
            print(result.final_response, flush=True)
            if request.get('result_path'):
                # Generic SDK events do not account for all retries/compaction requests.
                # Preserve unknown billing rather than invent a complete usage total.
                Path(request['result_path']).write_text(json.dumps(dict(usage=None, model=request['model'],
                    finish_reason=result.finish_reason, session_id=getattr(result,'session_id',None))), encoding='utf-8')
            if result.finish_reason != 'completed':
                raise RuntimeError('DSH execution did not complete')
    finally:
        harness.close()


if __name__ == '__main__':
    # This private worker owns every SDK subprocess; none needs a visible console.
    subprocess.Popen = HiddenPopen
    try:
        execute(json.loads(Path(sys.argv[1]).read_text('utf-8')),
                initialize_only='--initialize-only' in sys.argv[2:])
    except Exception:
        # SDK exceptions may contain request bodies and credential-bearing URLs.
        print('DSH execution failed; inspect runtime availability and model settings.', file=sys.stderr)
        raise SystemExit(1) from None
