"""Independent native publisher. Stops the owned worker tree before switching.

Only code is replaced; conversation databases, tombstones and job checkpoints
are never restored from a release snapshot. An idle observation remains pending.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import logging

log = logging.getLogger(__name__)


class ProcessTree:
    def __init__(self, command, *, cwd, env, pipes=False,text=False,stderr_file=None,stdout_file=None):
        self.job = None
        self.process = subprocess.Popen(command, cwd=cwd, env=env,
            stdin=subprocess.PIPE if pipes else None, stdout=stdout_file if stdout_file is not None else (subprocess.PIPE if pipes else None),
            stderr=stderr_file if stderr_file is not None else (subprocess.PIPE if pipes else None),
            text=text,encoding='utf-8' if text else None,errors='replace' if text else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
        if os.name == "nt":
            class Basic(ctypes.Structure):
                _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                            ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t),
                            ("MaxWorking", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
            class Limits(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                            ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            job = kernel.CreateJobObjectW(None, None)
            limits = Limits()
            limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(self.process._handle))):
                if job:
                    kernel.CloseHandle(job)
                self.process.kill()
                self.process.wait()
                raise OSError(ctypes.get_last_error(), "cannot_fence_owned_worker_tree")
            self.job, self.kernel = job, kernel

    def owns_pid(self, pid):
        """A venv launcher and its Python child can have different process IDs."""
        try:
            pid = int(pid)
            if pid <= 0 or self.process.poll() is not None:
                return False
            if os.name != "nt":
                return os.getpgid(pid) == self.process.pid
            kernel = self.kernel
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                owned = wintypes.BOOL()
                return bool(kernel.IsProcessInJob(handle, self.job, ctypes.byref(owned)) and owned.value)
            finally:
                kernel.CloseHandle(handle)
        except (OSError, ValueError, TypeError):
            return False

    def stop(self):
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif self.process.poll() is None:
            if os.name != "nt":
                import signal
                os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.process.kill()
        self.process.wait(timeout=10)


class HostSupervisor:
    def __init__(self, controller, config, *, startup_timeout=60):
        self.controller = controller
        self.config = Path(config).resolve()
        self.startup_timeout = startup_timeout
        self.tree = None
        self.token = ""
        self.health_path = controller.control / "worker-health.json"
        self.current_deployment = ""
        self.started = 0.0
        self.failures = 0
        self.retry_after = 0.0
        self._last_health = None
        self._channel_started = None
        self.health_failure = ''

    def start(self):
        if self.tree is not None:
            raise RuntimeError("worker_already_owned")
        from supervise import validate_data_compatibility
        validate_data_compatibility(self.controller.source,self.config)
        self.token = uuid.uuid4().hex
        self._last_health = None
        self._channel_started = None
        self.health_path.unlink(missing_ok=True)
        env = dict(os.environ, QQ_WORKER_TOKEN=self.token, QQ_WORKER_HEALTH=str(self.health_path))
        self.tree = ProcessTree([sys.executable, "-B", str(self.controller.source / "bot.py"),
                                 "--config", str(self.config)], cwd=self.controller.source, env=env)
        self.started = time.monotonic()
        log.info('Bot 已启动 pid=%s',self.tree.process.pid)

    def stop(self):
        if self.tree is not None:
            log.info('停止 Bot 进程树 pid=%s exit_code=%s',self.tree.process.pid,self.tree.process.poll())
            self.tree.stop()
            self.tree = None

    def publish(self, identity):
        state,reason = self.controller.publication_state(identity)
        if state!='eligible':
            if state=='cancelled':
                self.controller.deployment_phase(identity,'cancelled',reason)
            return False
        self.stop()
        try:
            phase_before = self.controller.deployment(identity)['phase']
            self.controller.install_source(identity)
            self.current_deployment = identity
            if phase_before!='installed' and self.controller.controller_changed(identity):
                self.restart_controller = True
                return True
            self.start()
            self.controller.deployment_phase(identity, "observing")
        except PermissionError as error:
            if str(error).startswith('publication_'):
                if phase_before=='prepared':
                    self.controller.rollback_source(identity)
                    self.controller.deployment_phase(identity,'cancelled',str(error))
                self.start()
                return False
            self.controller.rollback_source(identity)
            raise
        except BaseException:
            self.controller.rollback_source(identity)
            self.current_deployment = ""
            raise

    def health(self):
        if self.tree is None or self.tree.process.poll() is not None:
            return "failed"
        try:
            try:
                health = json.loads(self.health_path.read_text(encoding="utf-8"))
            except OSError:
                # A Windows sharing lock must not kill a healthy paid stream.
                # Reuse only a previously authenticated sample, under exactly
                # the same ownership and 20-second freshness checks below.
                if self._last_health is None:
                    self.health_failure='heartbeat_read_unavailable'
                    raise
                health=self._last_health
            if health.get('token')!=self.token:
                self.health_failure='heartbeat_token_mismatch'
                raise ValueError('invalid_health')
            if not self.tree.owns_pid(health.get('pid')):
                self.health_failure='heartbeat_pid_not_owned'
                raise ValueError('invalid_health')
            if not -5<=time.time()-float(health['at'])<=20:
                self.health_failure='heartbeat_expired'
                raise ValueError('stale_health')
            self._last_health=health
            self.health_failure=''
            if health.get('channel_connected') is False:
                return 'channel_offline'
            if self._channel_started is None:
                self._channel_started=time.monotonic()
            if health.get('cognition_ready') is False:
                self.health_failure='cognition_not_ready'
                return 'starting' if time.monotonic()-self._channel_started<self.startup_timeout else 'failed'
            # Startup stability does not establish behavior or improvement.
            return "observed" if int(health.get("completed_turns", 0)) >= 3 else "evidence_pending"
        except (OSError, ValueError, KeyError, TypeError):
            return "starting" if time.monotonic() - self.started < self.startup_timeout else "failed"

    def tick(self):
        state = self.health()
        if state == "failed" and self.current_deployment:
            identity = self.current_deployment
            self.stop()
            self.controller.rollback_source(identity)
            self.current_deployment = ""
            self.start()
            return "rolled_back"
        pending = self.controller.queued_deployments()
        if state == "failed" and not self.current_deployment and not pending:
            if self.tree is not None:
                code = self.tree.process.poll()
                ran = time.monotonic() - self.started
                log.error('Bot 健康检查失败 pid=%s exit_code=%s uptime=%.1fs reason=%s',
                          self.tree.process.pid,code,ran,'process_exited' if code is not None else self.health_failure or 'heartbeat_or_cognition_unhealthy')
                self.failures = 0 if code == 42 or ran >= 60 else self.failures + 1
                self.stop()
                self.retry_after = time.monotonic() + (60 if self.failures>=3 else min(30, 2 ** min(self.failures, 5)))
            if self.failures >= 3:
                # Do not permanently abandon a recoverable runtime after three
                # quick failures. Back off, preserving a stop opportunity.
                if time.monotonic()<self.retry_after:
                    return "blocked"
                log.warning('连续启动失败后的冷却已结束，重新尝试启动 Bot')
                self.failures = 0
            if time.monotonic() >= self.retry_after:
                self.start()
            return "restarting"
        if state == "observed" and self.current_deployment:
            self.controller.deployment_phase(self.current_deployment, "observed")
        for identity in pending:
            if identity == self.current_deployment:
                continue
            deployment = self.controller.deployment(identity)
            if deployment["phase"] == "observing":
                # Prior supervisor died. Its Windows job closed all workers;
                # install_source recovers either side of the atomic replacement.
                self.controller.deployment_phase(identity, "installed")
            self.publish(identity)
            return "observing"
        return state
