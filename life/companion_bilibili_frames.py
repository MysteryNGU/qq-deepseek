"""Cached visual materials from the MIT whale sampler; no DSH or model calls."""
import asyncio
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time

from .supervisor import ProcessTree

log = logging.getLogger(__name__)

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'vendor/bilibili-watch/tools/commands/bili-watch.mjs'

# 原始视频是抽帧用的临时中间产物：为一次分析下载，几秒内用完，之后只占磁盘。
# 2026-11-17 因此塞满过磁盘（两天 49 个 mp4）。做法是「用完即删」+ 两层兜底：
# 启动时清过期残留、总量超阈值时清最旧的。
VIDEO_SUFFIXES=('.mp4','.m4s','.flv','.webm','.mkv')
RETENTION_SECONDS=24*3600
MAX_BYTES=2*1024**3
KEEP_SHEET_SECONDS=24*3600
# 本进程正在用的视频：兜底清理和并发流程都不许把它删掉。
_BUSY=set()
_BUSY_GUARD=threading.Lock()


def _unlink(path):
    """删文件。失败只记日志：清理不能把主流程带崩。"""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        log.warning('无法删除临时视频 %s：%s',path,error)
        return False


def _claim(path):
    with _BUSY_GUARD:
        _BUSY.add(path)


def _release(path):
    with _BUSY_GUARD:
        _BUSY.discard(path)


def _busy():
    with _BUSY_GUARD:
        return set(_BUSY)


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

    # ---------------------------------------------------------------- 清理
    def sweep(self,*,now=None,retention=RETENTION_SECONDS,max_bytes=MAX_BYTES):
        """兜底清理：删过期的原始视频，再把总量压到 max_bytes 以下（先删最旧的）。

        只动原始视频和过期的九宫格图片，不碰 manifest、分析结果和当天的抽样图。
        正在被本进程使用的文件跳过。任何一步失败都只记日志。
        """
        now=time.time() if now is None else float(now)
        removed=[];freed=0;failed=0
        try:
            folder=self.folder
            if not folder.is_dir():return dict(removed=0,freed=0,failed=0)
            busy=_busy()
            videos=[];pictures=[]
            for path in folder.rglob('*'):
                try:
                    if not path.is_file() or path.is_symlink():continue
                    if str(path) in busy:continue
                    stat=path.stat()
                except OSError:continue
                if path.suffix.lower() in VIDEO_SUFFIXES:
                    videos.append((stat.st_mtime,stat.st_size,path))
                elif path.suffix.lower() in {'.jpg','.jpeg','.png'}:
                    pictures.append((stat.st_mtime,stat.st_size,path))
            for _mtime,size,path in videos:
                # 过期的先删
                try:
                    expired=now-path.stat().st_mtime>retention
                except OSError:
                    continue
                if expired and _unlink(path):
                    removed.append(path);freed+=size
            alive=[]
            for mtime,size,path in videos:
                if path.exists():alive.append((mtime,size,path))
            total=sum(size for _mtime,size,_path in alive)
            for _mtime,size,path in sorted(alive):
                if total<=max_bytes:break
                if _unlink(path):
                    removed.append(path);freed+=size;total-=size
                else:failed+=1
            for _mtime,size,path in pictures:
                try:
                    stale=now-path.stat().st_mtime>KEEP_SHEET_SECONDS
                except OSError:
                    continue
                if stale and _unlink(path):
                    removed.append(path);freed+=size
        except Exception as error:  # noqa: BLE001 - 清理失败不影响主流程
            log.warning('bilibili-frames 清理失败：%s',error)
            failed+=1
        if removed or failed:
            log.info('bilibili-frames 清理：删除 %d 个文件、释放 %d 字节、失败 %d',len(removed),freed,failed)
        return dict(removed=len(removed),freed=freed,failed=failed)

    def discard(self,video_path,*,folder=None):
        """用完即删。路径必须在这个缓存目录里，避免误删别人的文件。"""
        if not video_path:return False
        try:
            path=Path(video_path).resolve()
            if not path.is_relative_to(self.folder.resolve()):
                log.warning('拒绝删除缓存目录之外的视频：%s',path)
                return False
        except (OSError,ValueError):
            return False
        _release(str(path))
        return _unlink(path)

    async def startup_sweep(self):
        """启动时跑一次兜底清理；失败不阻塞启动。"""
        try:return await asyncio.to_thread(self.sweep)
        except Exception as error:  # noqa: BLE001
            log.warning('bilibili-frames 启动清理失败：%s',error)
            return dict(removed=0,freed=0,failed=1)

    # ---------------------------------------------------------------- 抽取
    @staticmethod
    def _newest_video(folder):
        """这个目录里最新的原始视频（不管子进程有没有在 manifest 里报出来）。"""
        newest=None
        try:
            for path in folder.rglob('*'):
                if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
                    if newest is None or path.stat().st_mtime>newest.stat().st_mtime:
                        newest=path
        except OSError:
            return None
        return str(newest) if newest else None

    async def _capture(self,command,env,timeout):
        tree=ProcessTree(command,cwd=ROOT,env=env,pipes=True)
        try:
            stdout,_=await asyncio.wait_for(asyncio.to_thread(tree.process.communicate),timeout)
            if tree.process.returncode:raise RuntimeError('frames_process_failed')
            return stdout.decode('utf-8','replace')
        finally:
            # Own and stop the whole ffmpeg child tree on cancellation/timeout.
            await asyncio.to_thread(tree.stop)

    def _environment(self):
        env=dict(os.environ,BILI_DATA_HOME=str(self.home/'data/companion/bilibili/visual'),
                 BILI_CREDENTIALS_FILE=str(self.home/'data/companion/bilibili/bili_credential.json'),
                 BILI_WHALE_CONFIG=str(self.home/'data/companion/bilibili/visual/config.json'))
        ffmpeg=os.environ.get('FFMPEG') or shutil.which('ffmpeg')
        if ffmpeg:env['FFMPEG']=ffmpeg
        return env,ffmpeg

    # 这两层抽出来是为了让「依赖在不在」可测：真流程里就是 node / ffmpeg 的实际路径。
    def _node(self):
        return shutil.which('node')

    def _has_credentials(self,env):
        return Path(env['BILI_CREDENTIALS_FILE']).is_file()

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
        # 所有临时原片都落在这一层。你以为子进程会在任何失败前删 --out-dir 里的东西，
        # 它们不会；进程被 kill / 下载中断都会留下文件，所以这里统一兜。
        # 以前放过子目录、而且只在 manifest 里记视频位置；现在一律清掉。
        for stale in folder.iterdir():
            if stale.is_file() and stale.suffix.lower() in VIDEO_SUFFIXES:
                self.discard(stale)
        manifest=folder/'manifest.json';base=None;cached=False
        if manifest.is_file():
            try:
                candidate=json.loads(manifest.read_text('utf-8'))
                # 原片用完即删，所以缓存命中只看九宫格在不在；manifest 里若还指着原片，
                # 说明是旧缓存，顺手清掉，也不让它再出现在回执里。
                self.discard(candidate.pop('video_path',None))
                if self._valid_file(candidate['sheet_path'],folder) and type(candidate.get('duration')) in {int,float}\
                        and math.isfinite(candidate['duration']) and candidate['duration']>0:
                    base=candidate;cached=True
            except (OSError,ValueError,KeyError,TypeError):pass
        env,ffmpeg=self._environment()
        if base is None:
            node=self._node()
            if not node or not ffmpeg:
                return dict(status='failed',code='frames_dependency_missing',output='画面抽样需要Node.js和ffmpeg，当前依赖不完整。')
            if not self._has_credentials(env):
                return dict(status='failed',code='frames_login_missing',output='尚未找到共享B站登录凭据，无法读取视频流。')
            command=[node,str(SCRIPT),bvid,'3x3',str(folder),'--part',str(part),'--tile',str(tile)]
            if uniform:command.append('--uniform')
            if substrip:command.append('--substrip')
            video=None
            try:
                output=await self._capture(command,env,3600)
                lines=[line[len('MANIFEST: '):] for line in output.splitlines() if line.startswith('MANIFEST: ')]
                if not lines:raise ValueError('frames_manifest_missing')
                received=json.loads(lines[-1])
                base={name:received[name] for name in ('sheet_path','video_path','timestamps','duration','method')}
                video=base['video_path'] or None
                if video:_claim(str(Path(video).resolve()))
                if not self._valid_file(base['sheet_path'],folder):raise ValueError('frames_output_missing')
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
                # 落盘之前就要去掉：回执和历史记录不该指向一个马上要删掉的文件。
                base={name:value for name,value in base.items() if name!='video_path'}
                self._save(manifest,base)
            finally:
                # 用完即删：九宫格已经成形，原片再没有别的用途。失败路径也走这里。
                # 这里要在 finally 内重新找一遍：下载/超时可能已经落了一半，而那时
                # manifest 根本没机会被解析，video 还是 None。
                self.discard(video or self._newest_video(folder))
        result=self._result(base,manifest,part,cached=cached)
        if time_seconds is None:
            self.sweep()
            return result
        if time_seconds>=base['duration']:
            return dict(status='failed',code='frame_out_of_range',output='放大时间点超出了该分P时长。')
        if not ffmpeg:return dict(status='failed',code='ffmpeg_missing',output='单帧放大需要ffmpeg。')
        second=round(float(time_seconds),3)
        frame=folder/('frame-'+format(second,'.3f')+'.jpg')
        cached=frame.is_file() and frame.stat().st_size>0
        if not cached:
            await self._single_frame(bvid,part,tile,base,folder,frame,second,env,ffmpeg)
            if not self._valid_file(str(frame),folder):raise ValueError('single_frame_missing')
        self.sweep()
        return dict(self._result(base,manifest,part,cached=cached),sheet_path=str(frame),grid_sheet_path=base['sheet_path'],
            frame_path=str(frame),timestamps=[second],cache_key=key+':'+format(second,'.3f'),
            coverage=dict(part=part,kind='single_frame',sample_count=1,video_duration=base['duration'],
                method='requested_timestamp',not_full_video=True))

    def _result(self,base,manifest,part,*,cached):
        return dict(base,status='completed',cached=cached,mime='image/jpeg',manifest_path=str(manifest),
            coverage=dict(part=part,kind='nine_frame_sample',sample_count=9,video_duration=base['duration'],
                          method=base['method'],not_full_video=True))

    async def _single_frame(self,bvid,part,tile,base,folder,frame,second,env,ffmpeg):
        """抽一张指定时间点的原尺寸画面。

        原片用完即删，所以这里没有能直接 seek 的视频：让采样器为同一分P单独下一次，
        抽完立刻删。失败路径同样删（finally）。代价是单帧放大要多下 30–60 秒（720P 短视频），
        换来的是磁盘上不留原片。
        """
        node=shutil.which('node')
        if not node:raise ValueError('node_missing')
        work=folder/('frame-'+format(second,'.3f')+'.work');work.mkdir(parents=True,exist_ok=True)
        video=None
        try:
            output=await self._capture([node,str(SCRIPT),bvid,'3x3',str(work),'--part',str(part),'--tile',str(tile)],env,3600)
            lines=[line[len('MANIFEST: '):] for line in output.splitlines() if line.startswith('MANIFEST: ')]
            if not lines:raise ValueError('frames_manifest_missing')
            video=json.loads(lines[-1]).get('video_path')
            if not video or not Path(video).is_file():raise ValueError('frames_video_missing')
            _claim(str(Path(video).resolve()))
            await self._capture([ffmpeg,'-y','-loglevel','error','-ss',format(second,'.3f'),'-i',video,
                                 '-frames:v','1','-q:v','2','-pix_fmt','yuv420p',str(frame)],env,120)
        finally:
            # work 目录整个删掉，里面就算有没报出来的半截原片也一并清掉。
            self.discard(video or self._newest_video(work))
            shutil.rmtree(work,ignore_errors=True)
