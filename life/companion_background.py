"""P40 ①：后台进程管理（长任务不再被超时丢掉）。

为什么要它：`command` 是同步等待，超过执行器超时就被丢掉，既拿不到结果也没法查进度。
今天她跑长命令就撞过这个。

设计要点（全部有对应实测，见 docs/p40-background-process-20261118.md）：

* **输出落文件**，不进内存——长任务的输出可能几百 MB，读回内存等于自己造 OOM。
* **进程树整体停**：复用 `life/supervisor.py` 的 `ProcessTree`（POSIX 用
  `start_new_session` + `killpg`，Windows 用 Job Object）。**只 kill 主进程会把
  子进程留成孤儿**，这是这类功能的经典坑。
* **重启后能查到**：pid / 日志路径 / 启动时间写进
  `data/companion/background/jobs.json`，服务重启后 `status/logs/wait` 仍可用。
* **不留孤儿**：服务关闭时 `reap_all()` 把它们全部停掉——不是"尽力而为"，是
  由 `executor.close()` 调用。

状态判定用 ``os.kill(pid, 0)`` 而不是 ``Popen.poll()``：服务重启之后我们手上
没有那个 `Popen` 对象，只有一个 pid。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import uuid

from .supervisor import ProcessTree

# 一次 jobs.json 读到的最多条目：文件是 append 式积累的，不清理会越来越慢。
MAX_JOBS = 200
# `logs` 默认读最后多少行；上限防止她把整个日志读进上下文。
DEFAULT_LINES = 60
MAX_LINES = 400


def _state(pid):
    """进程状态。僵尸要单独认出来——`kill(pid,0)` 对僵尸**也返回成功**。

    实测（2026-11-18）：子进程退出后 /proc/<pid>/stat 立刻变成 Z，但
    `os.kill(pid, 0)` 仍然是 ok。只看信号探测会把一个已经退出的进程永远当成
    "还在跑"，于是 `wait` 永远等不完、`stop` 永远报 still_running。
    """
    try:
        with open('/proc/%d/stat' % int(pid), encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except (OSError, ValueError):
        return None
    # comm 字段可能含空格和括号，从最后一个 ')' 之后取才是状态字符。
    tail = text.rsplit(')', 1)[-1].split()
    return tail[0] if tail else None


def _alive(pid):
    """进程还在不在（僵尸算不在）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    state = _state(pid)
    if state is not None:
        return state not in ('Z', 'X')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于我们：仍然是"在跑"，不能当成已经结束。
        return True
    except OSError:
        return False
    return True


class BackgroundJobs:
    """后台任务的登记表 + 启停实现。所有操作都是同步的，调用方放线程里跑。"""

    OPS = ('start', 'status', 'wait', 'stop', 'logs', 'list')

    def __init__(self, home):
        self.home = Path(home)
        self.folder = self.home / 'data/companion/background'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'jobs.json'
        # 本进程真正持有的进程对象：只有它能被 ProcessTree.stop() 连子孙一起停。
        self._trees = {}
        # 2026-11-20：启动对账。服务重启后 Popen 没了，遗留记录的退出码永远拿不到，
        # 以前它们停在 gone（"不知道"），现在明确标成 interrupted（"被打断了"）。
        try:
            self._mark_interrupted_on_start()
        except Exception:  # noqa: BLE001 - 对账失败不该拦住服务启动
            pass

    # ---- 登记表 -------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        rows = sorted(data.items(), key=lambda kv: kv[1].get('started') or 0)
        if len(rows) > MAX_JOBS:
            rows = rows[-MAX_JOBS:]
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(rows), ensure_ascii=False), encoding='utf-8')
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def _record(self, job_id, *, patch=None):
        data = self._load()
        row = data.get(job_id) or {}
        if patch:
            row.update(patch)
        data[job_id] = row
        self._save(data)
        return row

    # ---- 操作 ---------------------------------------------------------
    def start(self, *, command, cwd, shell=None):
        job_id = 'bg_' + uuid.uuid4().hex[:12]
        log_path = self.folder / (job_id + '.log')
        shell = shell or ('pwsh' if os.name == 'nt' else '/bin/sh')
        argv = [shell, '-c', command] if os.name != 'nt' else [shell, '-c', command]
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        with log_path.open('wb') as sink:
            tree = ProcessTree(argv, cwd=str(cwd), env=env, pipes=False,
                               stdout_file=sink, stderr_file=sink)
        # 跑完就没有 stdin 需要关；ProcessTree 在 pipes=False 时不开管道。
        self._trees[job_id] = tree
        row = dict(id=job_id, command=command, cwd=str(cwd), pid=tree.process.pid,
                   log=str(log_path), started=time.time(), stopped=None, exit_code=None)
        self._record(job_id, patch=row)
        return dict(status='completed', op='start', job_id=job_id, pid=tree.process.pid,
                    log=str(log_path), note='已在后台启动；用 op=status 看是否还在跑，'
                                           'op=logs 读输出，op=wait 等它结束，op=stop 停掉整棵树。')

    def _row(self, job_id):
        data = self._load()
        row = data.get(job_id)
        if row is None:
            raise KeyError(job_id)
        return row

    def _exit_code(self, job_id, row):
        """拿到真实退出码。

        两条来源，谁先有值用谁：
        * 本进程持有的 ``Popen``（调用 ``poll()`` 顺带回收僵尸）；
        * 台账里已经记下的 ``exit_code``。
        **服务重启后手上只有 pid**，那时如果进程已经不在、台账里又没退出码，
        就如实报 ``None`` 而不是编一个 0——「不知道成没成功」和「成功」必须分开。
        """
        tree = self._trees.get(job_id)
        if tree is not None:
            code = tree.process.poll()
            if code is not None and row.get('exit_code') is None:
                self._record(job_id, patch=dict(exit_code=code, stopped=time.time()))
                return code
        return row.get('exit_code')

    def list(self, limit=DEFAULT_LINES):
        """列出登记过的后台任务，最近启动的在前。

        2026-11-18：以前 status/logs/wait 都要先有 job_id，等于"记得住才查得到"，
        但 jobs.json 里本来就存着全部。这里把登记表摆出来。

        state=gone 的含义原样保留：进程不在了但拿不到退出码，
        那是"不知道成没成功"，不是成功也不是失败。
        """
        data = self._load()
        rows = sorted(data.items(), key=lambda kv: kv[1].get('started') or 0, reverse=True)
        rows = rows[:max(1, min(int(limit or DEFAULT_LINES), MAX_JOBS))]
        items = []
        for job_id, row in rows:
            # 用和 status() 完全相同的判定，避免两处口径不一致
            pid = row.get('pid')
            running = _alive(pid)
            exit_code = self._exit_code(job_id, row) if not running else None
            if running and exit_code is not None:
                running = False
            # 秒数只算一次，status 和 list 用同一个口径（§0.12）
            elapsed_seconds = None
            if row.get('started'):
                started = float(row['started'])
                if running:
                    elapsed_seconds = round(max(0.0, time.time() - started), 1)
                elif row.get('seconds'):
                    elapsed_seconds = round(float(row['seconds']), 1)
                else:
                    # 已结束但登记表没存秒数：用结束时刻减开始时刻。
                    # 结束时刻：stopped（真实写入的）→ finished（历史字段）→ 日志 mtime。
                    # 2026-11-20：此前只读 finished，而没有任何代码写过这个字段，
                    # 于是永远退回 mtime。stopped 才是 _exit_code/stop() 实际写入的。
                    end = row.get('stopped') or row.get('finished')
                    if not end:
                        log_path = Path(row.get('log') or '')
                        try:
                            if log_path.is_file():
                                end = log_path.stat().st_mtime
                        except OSError:
                            end = None
                    if end:
                        elapsed_seconds = round(max(0.0, float(end) - started), 1)
            if running:
                state = 'running'
            elif exit_code == 0:
                state = 'finished'
            elif exit_code is not None:
                state = 'failed'
            elif row.get('interrupted'):
                # 明确"被重启/被停打断"，不留在 gone 里让人猜。
                state = 'interrupted'
            else:
                state = 'gone'
            items.append(dict(job_id=job_id, state=state,
                              started=row.get('started'),
                              seconds=elapsed_seconds,
                              exit_code=exit_code,
                              interrupted=bool(row.get('interrupted')),
                              interrupted_reason=row.get('interrupted_reason') or '',
                              command=str(row.get('command') or '')[:120],
                              log=row.get('log')))
        if not items:
            return dict(status='completed', op='list', count=0, jobs=[],
                        output='还没有后台任务。用 op=start 起一个。')
        lines = []
        for item in items:
            # 直接用返回值里的同一个数，不再自己另算一遍
            elapsed = ''
            if item.get('seconds') is not None:
                elapsed = '%.0fs' % item['seconds']
            elif item.get('state') in ('running', 'finished', 'failed'):
                # 算不出来就说算不出来，不显示 0s 糊过去
                elapsed = '时长?'
            
            lines.append('%s｜%s｜%s｜%s%s' % (
                item['job_id'], item['state'],
                time.strftime('%m-%d %H:%M', time.localtime(item['started'] or 0)),
                elapsed,
                '｜' + item['command'] if item['command'] else ''))
        gone = sum(1 for item in items if item['state'] == 'gone')
        note = ('\n（其中 %d 个是 gone：进程不在了但拿不到退出码，'
                '那是不知道成没成功，不是成功也不是失败。可以看 op=logs 或重跑。）' % gone
                if gone else '')
        return dict(status='completed', op='list', count=len(items), jobs=items,
                    evidence_scope='stored_index',
                    evidence_note='这是登记表里的目录，不是原文；要输出用 op=logs 加 job_id。',
                    output='共 %d 个后台任务（最近的在前）：\n%s%s'
                           % (len(items), '\n'.join(lines), note))

    def status(self, job_id):
        row = self._row(job_id)
        pid = row.get('pid')
        running = _alive(pid)
        exit_code = self._exit_code(job_id, row) if not running else None
        if running and exit_code is not None:
            running = False
        log = Path(row.get('log') or '')
        size = log.stat().st_size if log.is_file() else 0
        if running:
            state = 'running'
        elif exit_code == 0:
            state = 'finished'
        elif exit_code is not None:
            state = 'failed'
        else:
            # 进程不在了但没记下退出码（典型：服务重启后由别人回收）。
            # 这正是「不知道」——不能写成 finished，也不能写成 failed。
            state = 'gone'
        return dict(status='completed', op='status', job_id=job_id, pid=pid,
                    running=running, state=state, exit_code=exit_code,
                    log=str(log), log_bytes=size,
                    seconds=round(time.time() - (row.get('started') or time.time()), 1),
                    command=row.get('command'),
                    note=None if state != 'gone' else
                    '进程已经不在了，但没有留下退出码（多半是服务重启期间被系统回收），'
                    '所以它到底成没成功不知道；看 logs 里的输出，或重新跑一次。')

    def wait(self, job_id, timeout=60):
        timeout = max(0, min(float(timeout), 600))
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self._row(job_id)
            if not _alive(row.get('pid')):
                return self.status(job_id)
            time.sleep(0.5)
        current = self.status(job_id)
        current['timed_out'] = True
        current['note'] = '等到 %d 秒还没结束，它仍在后台跑；这不是失败。' % int(timeout)
        return current

    def stop(self, job_id):
        row = self._row(job_id)
        pid = row.get('pid')
        tree = self._trees.get(job_id)
        if tree is not None:
            tree.stop()          # 连子孙一起停（POSIX killpg / Windows Job）
            self._trees.pop(job_id, None)
        elif _alive(pid):
            # 服务重启过，手上没有 Popen：只能按进程组发信号。
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                                   capture_output=True)
                else:
                    os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        still = _alive(pid)
        self._record(job_id, patch=dict(stopped=time.time(),
                                     interrupted=True,
                                     interrupted_reason='stopped_by_request'))
        return dict(status='completed', op='stop', job_id=job_id, pid=pid,
                    stopped=not still, still_running=still,
                    note='已停掉' if not still else '发出了停止信号但它还在；再查一次 status')

    def logs(self, job_id, lines=DEFAULT_LINES):
        row = self._row(job_id)
        log = Path(row.get('log') or '')
        if not log.is_file():
            return dict(status='failed', op='logs', job_id=job_id,
                        output='日志文件不在：%s' % log)
        count = max(1, min(int(lines or DEFAULT_LINES), MAX_LINES))
        with log.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # 只从尾部倒读，避免把几百 MB 的日志读进内存。
            chunk = min(size, 256 * 1024)
            handle.seek(size - chunk)
            tail = handle.read().decode('utf-8', errors='replace')
        picked = tail.splitlines()[-count:]
        return dict(status='completed', op='logs', job_id=job_id, lines=len(picked),
                    output='\n'.join(picked), log=str(log), log_bytes=size,
                    truncated=chunk < size)

    def _mark_interrupted_on_start(self):
        """启动对账：把"进程已不在、又没有退出码"的记录标成 interrupted。

        这些记录的共同处境是：上次服务退出时它们还在跑（或被 systemd 一起带走），
        重启后我们手上只剩一个 pid，且 /proc/<pid> 已经没了。
        退出码永远拿不回来——那就明说"被打断"，而不是让它停在 gone 里让人猜。
        已有 exit_code 的记录不动（那是正常结束，退出码是真的）。
        """
        data = self._load()
        changed = 0
        for job_id, row in data.items():
            if row.get('exit_code') is not None or row.get('interrupted'):
                continue
            if not row.get('started') or _alive(row.get('pid')):
                continue
            self._record(job_id, patch=dict(interrupted=True,
                                            interrupted_reason='process_restarted'))
            changed += 1
        return changed

    def reap_all(self):
        """服务关闭时调用：把所有登记在册、还活着的后台任务停掉，不留孤儿。"""
        stopped = []
        for job_id, row in self._load().items():
            if _alive(row.get('pid')):
                try:
                    self.stop(job_id)
                    stopped.append(job_id)
                except Exception:  # noqa: BLE001 - 关闭路径不能因为一个任务抛异常
                    pass
        self._trees.clear()
        return stopped
