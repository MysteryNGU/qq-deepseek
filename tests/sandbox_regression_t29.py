import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

BUNDLE = Path('/opt/qq-deepseek/toolchain/dsh/lib/node_modules/@deepseek-ai/dsh/node_modules/'
              '@deepseek-ai')

class SandboxRegression:
    """Mixin used by the class appended to test_agent_round2; expects self.manager()."""

    def test_fs_sandbox_is_the_mounted_filesystem(self):
        """dsh-fs-local's writeText takes 4 args and drops the policy: mounting it disables
        the write fence. The profile must keep fs-sandbox mounted."""
        profile = BUNDLE / 'dsh-sdk-minimal/cordis.patch.yml'
        if not profile.is_file():
            self.skipTest('profile not present')
        text = profile.read_text(encoding='utf-8')
        self.assertIn("name: '@deepseek-ai/dsh-fs-sandbox'", text,
                      'filesystem must stay fs-sandbox; fs-local silently drops the sandbox policy')
        self.assertNotIn("name: '@deepseek-ai/dsh-fs-local'", text)

    def test_tool_fs_mutations_stay_disabled(self):
        """bash is the only mutation path; write/edit tools staying off keeps one fence."""
        profile = BUNDLE / 'dsh-sdk-minimal/cordis.patch.yml'
        if not profile.is_file():
            self.skipTest('profile not present')
        text = profile.read_text(encoding='utf-8')
        block = text.split('- id: tool-fs', 1)[-1].split('- id:', 1)[0]
        for key in ('read: false', 'write: false', 'edit: false'):
            self.assertIn(key, block, 'tool-fs %s must stay disabled' % key.split(':')[0])

    def test_patch_carries_read_only_roots(self):
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_ro', node='work'), {})
        text = Path(mgr._sandbox_patch(workdir)).read_text(encoding='utf-8')
        self.assertIn('readOnlyRoots:', text,
                      'read confinement relies on this allowlist reaching landlock')
        self.assertIn(json.dumps(str(workdir)), text, 'the workdir must be readable')

    def test_read_only_roots_exclude_secrets(self):
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_sec', node='work'), {})
        roots = mgr._read_roots(workdir)
        for secret in ('/root', '/var/lib/companion', '/etc/companion'):
            for root in roots:
                self.assertFalse(str(root) == secret or str(root).startswith(secret + '/'),
                                 'read allowlist must not include %s (got %s)' % (secret, root))

    def test_landlock_grants_use_the_allowlist_not_root(self):
        """The vendor landlock profile must not bind "/" when an allowlist is present."""
        path = BUNDLE / 'dsh-sandbox-local/lib/index.js'
        if not path.is_file():
            self.skipTest('sandbox-local not present')
        source = path.read_text(encoding='utf-8', errors='replace')
        self.assertIn('policy.readOnlyRoots', source,
                      'landlock grants must honour readOnlyRoots')
        self.assertNotIn('readOnly: ["/"],', source,
                         'hardcoded readOnly ["/"] means "read everything"')

    def test_verification_unavailable_is_not_reported_as_ineffective(self):
        """A probe that cannot run must say so; blaming the patch sends debugging astray."""
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_unav', node='work'), {})
        workdir.mkdir(parents=True, exist_ok=True)
        verdict = mgr._verify_composed_sandbox('/nonexistent/patch.yaml', workdir)
        self.assertFalse(verdict.get('ok'))
        blob = json.dumps(verdict, ensure_ascii=False)
        self.assertNotIn('鏉冮檺琛ヤ竵娌℃湁鐪熸鐢熸晥', blob,
                         'must not claim the patch failed when the probe itself could not run')

    def test_verification_creates_missing_workdir(self):
        """_spawn calls _sandbox_patch before mkdir; the probe must not die on a missing cwd."""
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_mk', node='work'), {})
        if workdir.exists():
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        patch = mgr._sandbox_patch(workdir)
        self.assertTrue(Path(patch).is_file())

    def test_insert_style_patch_is_rejected_by_verification(self):
        """Appending a second sandbox-policy leaves the original danger-full-access live."""
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_ins', node='work'), {})
        workdir.mkdir(parents=True, exist_ok=True)
        bad = workdir / 'insert.yaml'
        bad.write_text('- insert:\n    - id: sandbox-policy-appended\n'
                       "      name: '@deepseek-ai/dsh-sandbox-policy'\n"
                       '      config:\n        mode: workspace-write\n'
                       '        workspaceRoot: %s\n' % json.dumps(str(workdir)), encoding='utf-8')
        verdict = mgr._verify_composed_sandbox(str(bad), workdir)
        if verdict.get('skipped'):
            self.skipTest('no DSH runtime here')
        self.assertFalse(verdict.get('ok'), 'an insert-style patch must never read as effective')


if __name__ == '__main__':
    unittest.main()
