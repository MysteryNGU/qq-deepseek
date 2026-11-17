"""B站原始视频用完即删：成功、失败、缓存命中、并发、兜底清理。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from life import companion_bilibili_frames as frames_module
from life.companion_bilibili_frames import Frames, RETENTION_SECONDS

VIDEO=b'x'*4096
BVID='BV1234567890'


class StubFrames(Frames):
    """把「下载+抽帧」那个子进程换成可控的假实现，其余逻辑全用真的。"""

    def __init__(self,home,*,emit=None):
        super().__init__(SimpleNamespace(home=home))
        self.emit=emit
        self.calls=0

    def _environment(self):
        return dict(FFMPEG='/usr/bin/ffmpeg'),'/usr/bin/ffmpeg'

    def _node(self):
        return '/usr/bin/node'

    def _has_credentials(self,env):
        return True

    async def _capture(self,command,env,timeout):
        # 单帧那条路径还会用 ffmpeg 截一张，不是采样器；真 ffmpeg 在测试里不能跑。
        if command[0].endswith('ffmpeg'):
            Path(command[-1]).write_bytes(b'\xff\xd8\xff'+b'f'*256)
            return ''
        # command = [node, SCRIPT, bvid, '3x3', outDir, '--part', ...]
        return self.emit(command,Path(command[4]))


def manifest_for(out_dir,video,*,duration=120.0,timestamps=None):
    sheet=Path(out_dir)/(BVID+'-sheet.jpg')
    sheet.write_bytes(b'\xff\xd8\xff'+b'z'*1024)
    return 'MANIFEST: '+json.dumps(dict(bvid=BVID,part=1,cid='1',title='t',duration=duration,quality=64,
        sheet_path=str(sheet),video_path=str(video),
        timestamps=timestamps or [0,13,26,39,52,65,78,91,104],method='等间隔'))


class FramesCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
        self.frames=StubFrames(self.home)
        self.folder=self.home/'data/companion/artifacts/bilibili-frames'
        self.folder.mkdir(parents=True)

    def tearDown(self):
        frames_module._BUSY.clear()
        self.tmp.cleanup()

    def _video(self,name='old.mp4',age=0):
        path=self.folder/name;path.write_bytes(VIDEO)
        if age:
            stamp=time.time()-age;os.utime(path,(stamp,stamp))
        return path

    def _sheet(self,name='sheet.jpg',age=0):
        path=self.folder/name;path.write_bytes(b'\xff\xd8\xff'+b'y'*512)
        if age:
            stamp=time.time()-age;os.utime(path,(stamp,stamp))
        return path

    def _mp4s(self):
        return list(self.folder.rglob('*.mp4'))

    # ---------------------------------------------------------------- 立即删
    async def test_success_deletes_video_keeps_sheet_and_manifest(self):
        video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,folder):
            video.write_bytes(VIDEO)
            return manifest_for(folder,video)
        frames=StubFrames(self.home,emit=emit)
        result=await frames.prepare(BVID)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self._mp4s(),[],'原片必须已经删掉')
        self.assertTrue(Path(result['sheet_path']).is_file(),'九宫格要留下')
        self.assertTrue(Path(result['manifest_path']).is_file())
        self.assertNotIn('video_path',result,'回执不该再指着一个已删的文件')

    async def test_failure_manifest_invalid_still_deletes_video(self):
        video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,folder):
            video.write_bytes(VIDEO)
            return 'MANIFEST: '+json.dumps(dict(bvid=BVID,part=1,sheet_path=str(folder/'missing.jpg'),
                video_path=str(video),timestamps=[],duration=120.0,method='等间隔'))
        frames=StubFrames(self.home,emit=emit)
        result=await frames.prepare(BVID)
        self.assertEqual(result['status'],'failed',result)
        self.assertIn('不能据此声称看到了画面',result['output'])
        self.assertEqual(self._mp4s(),[],'失败路径同样要删原片')

    async def test_failure_process_error_still_deletes_video(self):
        video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,out_dir):
            video=out_dir/(BVID+'-20261117.mp4');video.write_bytes(VIDEO)
            raise TimeoutError()
        frames=StubFrames(self.home,emit=emit)
        result=await frames.prepare(BVID)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['code'],'frames_timeout')
        self.assertEqual(self._mp4s(),[])

    async def test_cancellation_still_deletes_video(self):
        """取消（含 3.11+ 的 asyncio.TimeoutError，它是 CancelledError 的别名）也要删。"""
        video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,out_dir):
            video=out_dir/(BVID+'-20261117.mp4');video.write_bytes(VIDEO)
            raise asyncio.CancelledError()
        frames=StubFrames(self.home,emit=emit)
        with self.assertRaises(asyncio.CancelledError):
            await frames.prepare(BVID)
        self.assertEqual(self._mp4s(),[],'被取消时也不能把原片留在磁盘上')

    async def test_missing_manifest_line_still_deletes_video(self):
        video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,out_dir):
            video=out_dir/(BVID+'-20261117.mp4');video.write_bytes(VIDEO)
            return 'nothing useful'
        frames=StubFrames(self.home,emit=emit)
        result=await frames.prepare(BVID)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self._mp4s(),[])

    # ---------------------------------------------------------------- 缓存与单帧
    async def test_cache_hit_does_not_download_and_purges_legacy_video(self):
        key=hashlib.sha256(json.dumps(dict(version=1,bvid=BVID,part=1,tile=720,grid='3x3',
            uniform=False,substrip=False),sort_keys=True).encode()).hexdigest()
        folder=self.folder/key;folder.mkdir(parents=True)
        video=folder/'legacy.mp4';video.write_bytes(VIDEO)
        sheet=folder/'legacy.jpg';sheet.write_bytes(b'\xff\xd8\xff'+b'q'*512)
        (folder/'manifest.json').write_text(json.dumps(dict(sheet_path=str(sheet),video_path=str(video),
            timestamps=[0]*9,duration=120.0,method='等间隔')),encoding='utf-8')
        frames=StubFrames(self.home,emit=lambda command,folder: self.fail('缓存命中时不该再下载'))
        result=await frames.prepare(BVID)
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(result['cached'],'九宫格还在就该命中缓存')
        self.assertEqual(self._mp4s(),[],'旧 manifest 指着的原片要被删')
        self.assertNotIn('video_path',result)

    async def test_single_frame_downloads_again_then_deletes(self):
        """单帧放大要重新下一次、抽完立刻删，不能指望原片还在。"""
        main_video=self.folder/(BVID+'-20261117.mp4')
        def emit(command,out_dir):
            video=out_dir/('probe-'+str(emit.n)+'.mp4');emit.n+=1
            video.write_bytes(VIDEO)
            return manifest_for(out_dir,video)
        emit.n=0
        frames=StubFrames(self.home,emit=emit)
        first=await frames.prepare(BVID)
        self.assertEqual(first['status'],'completed',first)
        self.assertEqual(self._mp4s(),[])
        second=await frames.prepare(BVID,time_seconds=13.0)
        self.assertEqual(second['status'],'completed',second)
        self.assertEqual(second['coverage']['kind'],'single_frame')
        self.assertTrue(Path(second['frame_path']).is_file(),'单帧图要留下')
        self.assertEqual(self._mp4s(),[],'单帧用的原片也要删')
        self.assertEqual(emit.n,2,'单帧走的是第二次下载')

    # ---------------------------------------------------------------- 兜底清理
    def test_sweep_removes_expired_keeps_fresh(self):
        old=self._video('old.mp4',age=RETENTION_SECONDS+3600)
        fresh=self._video('fresh.mp4',age=60)
        result=self.frames.sweep()
        self.assertFalse(old.exists(),'超过 24 小时的残留要清')
        self.assertTrue(fresh.exists(),'新鲜的原始视频不动')
        self.assertGreaterEqual(result['removed'],1)

    def test_sweep_enforces_total_size_oldest_first(self):
        old=self._video('oldest.mp4',age=7200)
        newer=self._video('newer.mp4',age=3600)
        newest=self._video('newest.mp4',age=60)
        self.frames.sweep(max_bytes=len(VIDEO)*2)
        self.assertFalse(old.exists(),'超总量时先删最旧的')
        self.assertTrue(newer.exists() and newest.exists(),'降到阈值以内就停手')

    def test_sweep_skips_files_in_use(self):
        busy=self._video('busy.mp4',age=RETENTION_SECONDS+3600)
        frames_module._claim(str(busy.resolve()))
        self.frames.sweep()
        self.assertTrue(busy.exists(),'正在处理的视频不许被兜底清理删掉')

    def test_sweep_removes_stale_sheets_keeps_fresh_and_manifests(self):
        stale=self._sheet('stale.jpg',age=frames_module.KEEP_SHEET_SECONDS+3600)
        fresh=self._sheet('fresh.jpg',age=60)
        manifest=self.folder/'manifest.json';manifest.write_text('{}',encoding='utf-8')
        self.frames.sweep()
        self.assertFalse(stale.exists(),'过期的抽样图可以清')
        self.assertTrue(fresh.exists(),'当天的抽样图留着给她回看')
        self.assertTrue(manifest.exists(),'分析结果永远不清')

    def test_sweep_survives_missing_directory(self):
        empty=StubFrames(self.home/'not-created')
        self.assertEqual(empty.sweep(),dict(removed=0,freed=0,failed=0))

    def test_sweep_never_raises(self):
        with patch.object(frames_module.Path,'rglob',side_effect=OSError('io')):
            with self.assertLogs('life.companion_bilibili_frames',level='WARNING'):
                result=self.frames.sweep()
        self.assertEqual(result['failed'],1)

    async def test_startup_sweep_is_idempotent(self):
        old=self._video('old.mp4',age=RETENTION_SECONDS+3600)
        first=await self.frames.startup_sweep()
        second=await self.frames.startup_sweep()
        self.assertFalse(old.exists())
        self.assertEqual(first['removed'],1)
        self.assertEqual(second['removed'],0)


if __name__=='__main__':
    unittest.main()
