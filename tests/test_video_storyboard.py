"""Installation failure/activation boundaries and real-image acceptance criteria."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from life.services.dsh_bridge import DshBridgeError
from life.video_storyboard import PACKAGE, PACKAGE_SPEC, TOOLS, install, media_binary, preflight, smoke, verify_registry


class VideoStoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = Mock(root=self.root, home=self.root/'home', dsh_root=self.root/'dsh')
        self.bridge.profile_dir.return_value = self.root/'profile'
        self.bridge.tools.return_value = [dict(name=name) for name in TOOLS]

    def manifest(self, mounted=True):
        folder = self.bridge.profile_dir()
        folder.mkdir()
        value = dict(dependencies={PACKAGE: PACKAGE_SPEC}, dsh=dict(profile=dict(bundles=[PACKAGE] if mounted else [])))
        (folder/'package.json').write_text(json.dumps(value), encoding='utf-8')

    def test_failed_install_does_not_claim_activation(self):
        self.bridge._run_cli.return_value = (1, 'network unavailable')
        with self.assertRaisesRegex(DshBridgeError, 'network unavailable'):
            install(self.bridge)
        self.bridge.hello.assert_not_called()

    def test_installed_dependency_without_mount_is_not_success(self):
        self.manifest(mounted=False)
        self.bridge._run_cli.return_value = (0, '')
        with self.assertRaisesRegex(DshBridgeError, 'bundle'):
            install(self.bridge)
        self.bridge.hello.assert_not_called()

    def test_missing_registered_tool_fails_even_after_cli_success(self):
        self.manifest()
        self.bridge._run_cli.return_value = (0, '')
        self.bridge.tools.return_value = [dict(name='video_storyboard')]
        with self.assertRaisesRegex(DshBridgeError, 'video_zoom'):
            install(self.bridge)

    def test_install_uses_pinned_native_profile_and_does_not_claim_execution(self):
        self.manifest()
        self.bridge._run_cli.return_value = (0, '')
        result = install(self.bridge)
        self.bridge._run_cli.assert_called_once_with(['plugin', '--profile', 'sdk', 'add', PACKAGE_SPEC])
        self.assertFalse(result['actual_video_call_verified'])
        self.bridge.execute.assert_not_called()

    def test_foreign_profile_handshake_error_is_not_ignored(self):
        self.bridge.hello.side_effect = DshBridgeError('foreign profile')
        with self.assertRaisesRegex(DshBridgeError, 'foreign profile'):
            verify_registry(self.bridge)

    def test_explicit_missing_media_binary_does_not_fall_back(self):
        self.assertEqual(media_binary('ffmpeg', self.bridge.home, {'DSH_FFMPEG_PATH': 'bad.exe'}), 'bad.exe')

    def test_bot_home_binary_precedes_path(self):
        name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
        binary = self.bridge.home/'tools/ffmpeg/bin'/name
        binary.parent.mkdir(parents=True)
        binary.touch()
        with patch('life.video_storyboard.shutil.which', return_value='other-ffmpeg'):
            self.assertEqual(media_binary('ffmpeg', self.bridge.home, {}), str(binary))

    def test_preflight_does_not_mutate_profile_when_runtime_is_missing(self):
        with patch('life.video_storyboard.media_binary', return_value=None):
            result = preflight(self.bridge)
        self.assertFalse(result['ready'])
        self.assertGreaterEqual(len(result['problems']), 5)
        self.bridge.ensure_profile.assert_not_called()
        self.bridge._run_cli.assert_not_called()

    def snapshot(self, tool, image=True):
        content = [dict(type='text', text='00:01.00-00:02.00 qq storyboard fixture')]
        if tool in {'video_storyboard', 'video_zoom'} and image:
            content.append(dict(type='image', attachment=dict(id='synthetic-attachment')))
        return dict(status='result_recorded', settled=True, result=dict(content=content))

    def test_smoke_rejects_text_only_success_for_visual_tool(self):
        self.bridge.execute.side_effect = lambda name, args: self.snapshot(name, image=False)
        with patch('life.video_storyboard.subprocess.run'), self.assertRaisesRegex(DshBridgeError, '图片附件'):
            smoke(self.bridge, 'ffmpeg')

    def test_smoke_rejects_tool_error_inside_successful_transport(self):
        self.bridge.execute.return_value = dict(status='result_recorded', result=dict(isError=True))
        with patch('life.video_storyboard.subprocess.run'), self.assertRaisesRegex(DshBridgeError, '未成功'):
            smoke(self.bridge, 'ffmpeg')

    def test_smoke_exercises_all_four_tools_without_asr(self):
        self.bridge.execute.side_effect = lambda name, args: self.snapshot(name)
        with patch('life.video_storyboard.subprocess.run'):
            result = smoke(self.bridge, 'ffmpeg')
        self.assertTrue(result['actual_video_call_verified'])
        self.assertFalse(result['asr_verified'])
        calls = self.bridge.execute.call_args_list
        self.assertEqual({c.args[0] for c in calls}, TOOLS)
        for call in calls:
            if call.args[0] in {'video_storyboard', 'video_zoom'}:
                self.assertEqual(call.args[1]['transcript'], 'off')
            self.assertTrue(Path(call.args[1]['input']).is_relative_to(self.root))


if __name__ == '__main__':
    unittest.main()
