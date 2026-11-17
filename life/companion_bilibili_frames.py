"""Cached visual materials from the MIT whale sampler; no DSH or model calls."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

from .supervisor import ProcessTree

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'vendor/bilibili-watch/tools/commands/bili-watch.mjs'


class Frames:
    def __init__(self,core):
        self.home=Path(core.home).resolve()
        self.folder=self.home/'data/companion/artifacts/bilibili-frames'
        self.lock=asyncio.Lock()

    @staticmethod
    def _save(path,value):
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temp.replace(path)

    @staticmethod
    def _valid_file(value,folder):
        path=Path(value).resolve()
        return path.is_relative_to(folder.resolve()) and path.is_file() and path.stat().st_size>0

    async def _capture(self,command,env,timeout):
        tree=ProcessTree(command,cwd=ROOT,env=env,pipes=True)
        try:
            stdout,_=await asyncio.wait_for(asyncio.to_thread(tree.process.communicate),timeout)
            if tree.process.returncode:raise RuntimeError('frames_process_failed')
            return stdout.decode('utf-8','replace')
        finally:
            # Own and stop the whole ffmpeg child tree on cancellation/timeout.
            await asyncio.to_thread(tree.stop)

    async def prepare(self,bvid,*,time_seconds=None,part=1,tile=720,uniform=False,substrip=False):
        if not isinstance(bvid,str) or not re.fullmatch(r'BV[a-zA-Z0-9]{10}',bvid):
            return dict(status='failed',code='invalid_bvid',output='需要有效BV号。')
        if type(part) is not int or part<1 or type(tile) is not int or not 240<=tile<=1280:
            return dict(status='failed',code='invalid_sampling',output='分P与抽帧尺寸参数无效。')
        if time_seconds is not None and (type(time_seconds) not in {int,float} or not math.isfinite(time_seconds) or time_seconds<0):
            return dict(status='failed',code='invalid_frame_time',output='放大时间点需要非负秒数。')
        async with self.lock:
            try:return await self._prepare(bvid,time_seconds,part,tile,uniform,substrip)
            except asyncio.CancelledError:raise
            except Exception as error:
                # Never return raw subprocess stderr: media URLs can be signed.
                return dict(status='failed',code='frames_timeout' if isinstance(error,asyncio.TimeoutError) else 'frames_failed',
                            output='视频画面素材未取得（'+type(error).__name__+'）；不能据此声称看到了画面。')

    async def _prepare(self,bvid,time_seconds,part,tile,uniform,substrip):
        parameters=dict(version=1,bvid=bvid,part=part,tile=tile,grid='3x3',uniform=bool(uniform),substrip=bool(substrip))
        key=hashlib.sha256(json.dumps(parameters,sort_keys=True).encode()).hexdigest()
        folder=self.folder/key;folder.mkdir(parents=True,exist_ok=True)
        manifest=folder/'manifest.json';base=None;cached=False;known_video=None
        if manifest.is_file():
            try:
                candidate=json.loads(manifest.read_text('utf-8'))
                if self._valid_file(candidate['video_path'],folder) and type(candidate.get('duration')) in {int,float} and math.isfinite(candidate['duration']) and candidate['duration']>0:
                    known_video=candidate
                if all(self._valid_file(candidate[name],folder) for name in ('sheet_path','video_path')):
                    base=candidate;cached=True
            except (OSError,ValueError,KeyError,TypeError):pass
        ffmpeg=os.environ.get('FFMPEG') or shutil.which('ffmpeg')
        env=dict(os.environ,BILI_DATA_HOME=str(self.home/'data/companion/bilibili/visual'),
                 BILI_CREDENTIALS_FILE=str(self.home/'data/companion/bilibili/bili_credential.json'),
                 BILI_WHALE_CONFIG=str(self.home/'data/companion/bilibili/visual/config.json'))
        if ffmpeg:env['FFMPEG']=ffmpeg
        if base is None:
            node=shutil.which('node')
            if not node or not ffmpeg:
                return dict(status='failed',code='frames_dependency_missing',output='画面抽样需要Node.js和ffmpeg，当前依赖不完整。')
            if known_video is None and not Path(env['BILI_CREDENTIALS_FILE']).is_file():
                return dict(status='failed',code='frames_login_missing',output='尚未找到共享B站登录凭据，无法读取视频流。')
            command=[node,str(SCRIPT),bvid,'3x3',str(folder),'--part',str(part),'--tile',str(tile)]
            if known_video:command.extend(['--video',known_video['video_path'],'--duration',str(known_video['duration'])])
            if uniform:command.append('--uniform')
            if substrip:command.append('--substrip')
            output=await self._capture(command,env,3600)
            lines=[line[len('MANIFEST: '):] for line in output.splitlines() if line.startswith('MANIFEST: ')]
            if not lines:raise ValueError('frames_manifest_missing')
            received=json.loads(lines[-1])
            base={name:received[name] for name in ('sheet_path','video_path','timestamps','duration','method')}
            if not all(self._valid_file(base[name],folder) for name in ('sheet_path','video_path')):raise ValueError('frames_output_missing')
            times=base['timestamps']
            if not isinstance(times,list) or len(times)!=9 or any(type(t) not in {int,float} or not math.isfinite(t) or t<0 for t in times):
                raise ValueError('frames_invalid_timestamps')
            if type(base['duration']) not in {int,float} or not math.isfinite(base['duration']) or base['duration']<=0:
                raise ValueError('frames_invalid_duration')
            for name in ('quality','cid','title','subtitle_strip_path'):
                if name in received:
                    if name=='subtitle_strip_path' and not self._valid_file(received[name],folder):continue
                    base[name]=received[name]
            base.update(cache_key=key,bvid=bvid,part=part)
            self._save(manifest,base)
        result=dict(base,status='completed',cached=cached,mime='image/jpeg',manifest_path=str(manifest),
            coverage=dict(part=part,kind='nine_frame_sample',sample_count=9,video_duration=base['duration'],
                          method=base['method'],not_full_video=True))
        if time_seconds is None:return result
        if time_seconds>=base['duration']:
            return dict(status='failed',code='frame_out_of_range',output='放大时间点超出了该分P时长。')
        if not ffmpeg:return dict(status='failed',code='ffmpeg_missing',output='单帧放大需要ffmpeg。')
        second=round(float(time_seconds),3)
        frame=folder/('frame-'+format(second,'.3f')+'.jpg')
        cached=frame.is_file() and frame.stat().st_size>0
        if not cached:
            await self._capture([ffmpeg,'-y','-loglevel','error','-ss',format(second,'.3f'),'-i',base['video_path'],
                                 '-frames:v','1','-q:v','2','-pix_fmt','yuvj420p',str(frame)],env,120)
            if not self._valid_file(str(frame),folder):raise ValueError('single_frame_missing')
        return dict(result,sheet_path=str(frame),grid_sheet_path=base['sheet_path'],frame_path=str(frame),timestamps=[second],cached=cached,
            cache_key=key+':'+format(second,'.3f'),coverage=dict(part=part,kind='single_frame',sample_count=1,
                video_duration=base['duration'],method='requested_timestamp',not_full_video=True))
