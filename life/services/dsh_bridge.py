#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 工具桥：QQ 侧决定调用哪个 DSH 工具，走真实 runExternalToolTurn。

三条硬约束（《情绪内驱动与 DSH 工具桥》P16–P24）：

* 读写分离：子进程 stdin 只在短锁内写，stdout 由读线程按 JSON-RPC id 分发到
  各自的等待队列。某个 execute 还在跑时，cancel、status 和服务端审批回调照样能进。
* 先落盘再发请求：操作编号 + 请求指纹写进 life.db 的 bridge_operations，重启后
  逐条对账；同一编号换参数一律拒绝，绝不重复安装或重复改文件。
* 取消要等真停：cancel 只请求停止，只有工具真停了（settled）才写终态，
  没停下就保持 stopping，由调用方继续查。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("qq.dsh_bridge")

#: 已经不需要再对账的终态。
TERMINAL = {"result_recorded", "denied", "failed_before_dispatch", "cancelled"}
#: Bot 在 profile 目录里留的身份标记；DSH 侧 hello 会核实它。
MARKER_NAME = ".qq-bot-owned.json"
PROFILE_NAME = "sdk"


class DshBridgeError(RuntimeError):
    """DSH 进程、协议或远端返回的错误。"""


class DshRpcError(DshBridgeError):
    """A definite RPC rejection, distinct from a lost response."""
    def __init__(self,code,message):
        self.rpc_code = code
        super().__init__(f'DSH 错误 {code}: {message}')


def _abs(value: str | os.PathLike[str], base: Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _same_path(a: Any, b: Any) -> bool:
    try:
        return os.path.normcase(str(Path(str(a)).resolve())) == \
            os.path.normcase(str(Path(str(b)).resolve()))
    except Exception:
        return False


def request_hash(tool: str, arguments: dict[str, Any] | None) -> str:
    """一次工具请求的稳定指纹：同一个编号只能对应同一份请求。"""
    blob = json.dumps({"tool": tool, "arguments": arguments or {}},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_transport_error(exc: BaseException) -> bool:
    """区分“链路断了、结果未知”和“服务端明确拒绝、没有执行”。"""
    text = str(exc)
    return any(mark in text for mark in (
        "DSH 进程已退出", "DSH 请求超时", "DSH 进程没有在运行"))


class DshBridge:
    """一个常驻 DSH SDK 子进程，多请求并发、按 id 分发应答。"""

    def __init__(self, cfg: Any, root: Path | None = None, ledger: Any | None = None):
        self.cfg = cfg
        self.root = Path(root or ROOT)
        self.ledger = ledger                     # life.services.store.Store
        self.dsh_root = _abs(getattr(cfg, "dsh_root", "") or "../dsh-bridge-work",
                             self.root).resolve()
        self.home = _abs(getattr(cfg, "dsh_home", "data/dsh-home"), self.root).resolve()
        self.node = str(getattr(cfg, "dsh_node", "") or "").strip()
        self.provider = str(getattr(cfg, "dsh_provider", "deepseek-official")
                            or "deepseek-official")
        self.model = str(getattr(cfg,'model','deepseek-flash') or 'deepseek-flash')
        self.timeout = float(getattr(cfg, "dsh_timeout", 180.0) or 180.0)
        self._scope_base = f"qq-life-{uuid.uuid4().hex[:16]}"
        self._lock = threading.RLock()           # 进程生命周期
        self._write_lock = threading.Lock()      # stdin 单写者
        self._pending_lock = threading.Lock()    # id -> 等待队列
        self._pending: dict[int, queue.Queue] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._tree = None
        self._reader: threading.Thread | None = None
        self._stderr = None
        self._next_id = 0
        self._generation = 0
        self.runtime_access = {}
        self._notification_handler = None
        self._reconciled = False
        self._closed = False
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            # 服务端反向请求默认全部拒绝（fail closed），由 set_approver 接管。
            "approval/request": self._deny_server_request,
            "qq_life/approval": self._deny_server_request,
        }
        self._log_path = self.root / "data" / "logs" / "dsh-bridge.log"

    @property
    def scope(self) -> str:
        """每个 worker 代数一个独立执行域。

        进程被换掉时旧会话不再复用：DSH 会话日志里只要有一次没走完的工具调用，
        再 resume 就会报 SessionFormatError；对账只认桥这边的操作编号。
        """
        return f"{self._scope_base}-g{self._generation}"

    # ---------------------------------------------------------------- 进程

    @staticmethod
    def _node_major(exe: str) -> int:
        """问一下这个 node 是几版；问不出来按 0 算。"""
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  timeout=15)
            text = (proc.stdout or proc.stderr or "").strip().lstrip("vV")
            return int(text.split(".")[0])
        except Exception:
            return 0

    def _node_exe(self) -> str:
        """找 node：配置 → 环境 → DSH 自带运行时 → 本地 node22 → PATH。

        PATH 上的旧版 node 起 sdk profile 会静默退出，桥只看到 initialize 超时，
        所以这里按版本挑，而不是谁先命中谁算。
        """
        cached = getattr(self, "_node_cache", "")
        if cached and Path(cached).is_file():
            return cached
        seen: list[str] = []
        runtime = sorted(Path.home().glob(".dsh/dsh-runtimes/*/dependencies/node*/node.exe"))
        candidates = [str(getattr(self.cfg, "dsh_node", "") or self.node or ""),
                      os.environ.get("QQ_LIFE_NODE", ""),
                      *[str(x) for x in runtime],
                      r"D:\projects\tools\node-v22.19.0-win-x64\node.exe",
                      shutil.which("node") or ""]
        best, best_major = "", 0
        for cand in candidates:
            if not cand or cand in seen or not Path(cand).is_file():
                continue
            seen.append(cand)
            major = self._node_major(cand)
            if major >= 22:
                self._node_cache = str(Path(cand))
                return self._node_cache
            if major > best_major:
                best, best_major = str(Path(cand)), major
        if best:
            log.warning("DSH 桥：只找到 node %s（主版本 %s），DSH 可能要 22", best, best_major)
            self._node_cache = best
            return best
        raise DshBridgeError("找不到 node.exe；在配置里写 dsh_node")

    def _pnpm_entry(self) -> str:
        """定位随 DSH 安装的 pnpm 入口。

        插件管理按 PATH 找 `pnpm`，而 DSH 自带运行时只放了 pnpm.cjs/.mjs，
        没有 .cmd 垫片；这里找到入口，由 Bot 自己生成那条命令。
        """
        candidates = [str(getattr(self.cfg, "dsh_pnpm_entry", "") or ""),
                      str(os.environ.get("QQ_LIFE_PNPM_ENTRY", "") or "")]
        runtimes = Path.home() / ".dsh" / "dsh-runtimes"
        if runtimes.is_dir():
            candidates.extend(str(p) for p in sorted(
                runtimes.glob("*/dependencies/pnpm/bin/pnpm.mjs")))
        candidates.append(
            "D:/projects/dsh/resources/runtime/primary-runtime/dependencies/pnpm/bin/pnpm.mjs")
        for cand in candidates:
            if cand and Path(cand).is_file():
                return str(Path(cand))
        return ""

    def _ensure_pnpm_shim(self, shim_dir: Path, node: str, entry: str) -> None:
        """在 Bot 自己的 data/bin 里维护 pnpm.cmd。

        插件管理用 scrubbedParentEnv 起 pnpm：DSH_* 与含 KEY/TOKEN 的变量会被清掉，
        所以垫片里写绝对路径，只依赖 PATH 能找到它。
        """
        lines = [
            "@echo off",
            "rem qq-life managed shim: run the DSH-bundled pnpm with the Bot node.",
            '"' + node + '" --require "' + str(Path(__file__).with_name('pnpm_windows_links.cjs')) + '" "' + entry + '" %*',
            ""]
        text = chr(13) + chr(10) + (chr(13) + chr(10)).join(lines)
        target = shim_dir / "pnpm.cmd"
        shim_dir.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.read_text(encoding="ascii", errors="ignore") == text:
            return
        with open(target, "w", encoding="ascii", newline="") as fh:
            fh.write(text)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DSH_HOME"] = str(self.home)
        env["QQ_LIFE_HOME"] = str(self.home)
        env["NO_COLOR"] = "1"
        # Bot 自己的专用 worker：沙箱档位由 Bot 的同意门把关，这里不再二次询问；
        # 桥仍然默认拒绝审批回调，只有接了审批人（set_approver）才放行。
        env['DSH_PERMISSION_MODE'] = 'danger-full-access'
        env["TERM"] = "dumb"
        # 插件管理要调 pnpm：给它一条由 Bot 维护的命令，装在随 DSH 安装的运行时上。
        node = self._node_exe()
        env["QQ_LIFE_NODE"] = node
        entry = self._pnpm_entry()
        if entry:
            env["QQ_LIFE_PNPM_ENTRY"] = entry
            shim_dir = self.root / "data" / "bin"
            self._ensure_pnpm_shim(shim_dir, node, entry)
            env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
        key = str(getattr(self.cfg, "api_key", "") or "").strip()
        if key:
            env["DEEPSEEK_API_KEY"] = key
        endpoint = str(getattr(self.cfg,'base_url','') or '').strip()
        if endpoint:
            if endpoint.rstrip('/')=='https://api.deepseek.com':
                endpoint = endpoint.rstrip('/')+'/anthropic'
            env['DEEPSEEK_BASE_URL'] = endpoint
        tmp = str(getattr(self.cfg, "dsh_tmp", "") or r"D:\projects\tools\tmp")
        Path(tmp).mkdir(parents=True, exist_ok=True)
        env["TMP"] = tmp
        env["TEMP"] = tmp
        return env

    def profile_dir(self) -> Path:
        return self.home / "profiles" / PROFILE_NAME

    def ensure_profile(self) -> dict[str, Any]:
        """建好 Bot 自己的 SDK profile 目录并留下归属标记。

        DSH 启动时会补 package.json；这里先写成与 initProfile 相同的形状，
        让身份核验不依赖“刚好已经启动过”。
        """
        prof = self.profile_dir()
        prof.mkdir(parents=True, exist_ok=True)
        manifest = prof / "package.json"
        if not manifest.is_file():
            manifest.write_text(json.dumps({
                "name": f"dsh-profile-{PROFILE_NAME}",
                "private": True,
                "dependencies": {},
                # 必须与 DSH 的 sdk 模板一致，否则只起 dsh-base、没有 JSON-RPC 服务端。
                "dsh": {"profile": {"bundles": [
                    "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-sdk-app",
                ]}},
            }, indent=2) + "\n", encoding="utf-8")
        node = self._node_exe()
        entry = self._pnpm_entry()
        pnpm_command = ""
        if entry:
            shim_dir = self.root / "data" / "bin"
            self._ensure_pnpm_shim(shim_dir, node, entry)
            pnpm_command = str(shim_dir / "pnpm.cmd")
        self._ensure_profile_patch(prof, pnpm_command)
        marker = prof / MARKER_NAME
        payload = {"owner": "qq-life", "home": str(self.home),
                   "profile": PROFILE_NAME, "createdAt": time.time()}
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return {"profile": PROFILE_NAME, "dir": str(prof),
                "marker": str(marker), "manifest": str(manifest)}

    def _ensure_profile_patch(self, prof: Path, pnpm_command: str) -> None:
        """维护 Bot 专用 profile 的工具、主角色接口和长截图接收配置。

        这一段属于 Bot 自己的 profile（有归属标记），因此由桥写入而不是让用户手改。
        pnpmCommand 配在 plugin-manager 服务那一行：execa 不按 PATHEXT 解析裸 `pnpm`，
        指向 Bot 维护的垫片才起得来。
        """
        patch = prof / "cordis.patch.yml"
        rows = [
            "# 由 qq-life 桥维护：Bot 工具、主角色接口与长截图接收；其它沿用 DSH 默认。",
            "- id: tool-plugin-manager",
            "  disabled: false",
            "- id: system-prompt",
            "  config:",
            "    personaPrefix: ''",
            # Long QQ screenshots exceed 8192px; keep total-pixel/byte limits
            # and DSH normalization unchanged, while admitting their full source.
            '- id: attachment-local',
            '  config:',
            '    maxImageDimension: 32768',
        ]
        if pnpm_command:
            rows.extend([
                "- id: plugin-manager",
                "  config:",
                "    pnpmCommand: '" + pnpm_command + "'",
            ])
        # The configured deployment launches the source CLI with tsx. Keep its
        # bot adapter on the same source revision while developing the bridge.
        if (self.dsh_root / 'packages/sdk/server/src/index.ts').is_file():
            rows.extend(['- id: sdk-jsonrpc-server',
                         "  name: '@deepseek-ai/dsh-sdk-jsonrpc-server/src/index.ts'"])
        backend = self._prepare_bili_backend()
        if backend:
            # Reuse the installed plugin's real MCP primitives, without its
            # independent scan scheduler, fixed prompts or posting policy.
            rows.extend(['- insert:', '    - id: qq-life-bili',
                "      name: '@deepseek-ai/dsh-mcp-client'",'      config:',
                '        serverName: bili','        transport: stdio',
                '        command: '+json.dumps(backend['python']),
                '        args: ['+json.dumps(backend['script'])+']',
                '        cwd: '+json.dumps(backend['cwd']),
                '        toolCallTimeoutMs: 120000','        env:',
                "          PYTHONUTF8: '1'","          PYTHONIOENCODING: utf-8"])
        rows.append("")
        body = chr(10).join(rows)
        # The core is a bundle layer; the profile patch remains owned by plugins/the role.
        # Migrate only byte-equivalent rows of the old bridge-owned document.
        existing=patch.read_text('utf-8') if patch.is_file() else ''
        chunks=lambda value: [part for part in re.split(r'(?m)(?=^- (?:id:|insert:))',value) if part.strip()]
        core_chunks=chunks(body)
        if existing.startswith('# 由 qq-life 桥维护：'):
            known={part.strip() for part in core_chunks}
            kept=[part for part in chunks(existing) if part.strip() not in known]
            remaining=''.join(kept) or '[]\n'
            backup=prof/'qq-life-profile-before-split.yml'
            if not backup.exists():
                backup.write_text(existing,encoding='utf-8')
            patch.write_text(remaining,encoding='utf-8')
            existing=remaining
        elif not existing.strip():
            patch.write_text('[]\n',encoding='utf-8')
            existing='[]\n'
        if re.search(r'(?m)^\s*-?\s*id:\s*qq-life-bili\s*$',existing):
            core_chunks=[part for part in core_chunks if not re.search(r'(?m)^\s*-?\s*id:\s*qq-life-bili\s*$',part)]
        core=prof/'qq-life-core'
        core.mkdir(exist_ok=True)
        package=dict(name='@qq-life/bridge-core',version='1.0.0',private=True,
            dsh=dict(bundle=dict(patch='./cordis.patch.yml')))
        (core/'package.json').write_text(json.dumps(package,indent=2)+'\n',encoding='utf-8')
        (core/'cordis.patch.yml').write_text(''.join(core_chunks),encoding='utf-8')
        installed=prof/'node_modules/@qq-life/bridge-core'
        if installed.is_symlink() or (hasattr(installed,'is_junction') and installed.is_junction()):
            if installed.resolve()!=core.resolve():
                raise DshBridgeError('Bot 核心 bundle 链接不属于当前 profile')
        else:
            if (installed/'package.json').is_file() and json.loads((installed/'package.json').read_text('utf-8')).get('name')!=package['name']:
                raise DshBridgeError('Bot 核心 bundle 位置已由其他包占用')
            installed.mkdir(parents=True,exist_ok=True)
            for name in ('package.json','cordis.patch.yml'):
                shutil.copyfile(core/name,installed/name)
        manifest=prof/'package.json'
        value=json.loads(manifest.read_text('utf-8'))
        value.setdefault('dependencies',{})['@qq-life/bridge-core']='file:./qq-life-core'
        bundles=value.setdefault('dsh',{}).setdefault('profile',{}).setdefault('bundles',[])
        bundles=[name for name in bundles if name!='@qq-life/bridge-core']
        position=max((i+1 for i,name in enumerate(bundles) if name in {'@deepseek-ai/dsh-base','@deepseek-ai/dsh-sdk-app'}),default=0)
        bundles.insert(position,'@qq-life/bridge-core')
        value['dsh']['profile']['bundles']=bundles
        manifest.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')

    def _prepare_bili_backend(self):
        value = str(getattr(self.cfg,'bili_plugin','') or '')
        if not value:
            return None
        package = Path(value).resolve()
        source = package/'vendor/bilibili-mcp/mcp_server.py'
        if not source.is_file():
            log.warning('B站能力不可用：安装的插件中没有 MCP 后端')
            return None
        folder = self.home/'bili-duty'
        folder.mkdir(parents=True,exist_ok=True)
        if self.home not in folder.resolve().parents:
            raise DshBridgeError('B站工具状态目录不能通过链接指向其他应用')
        script = folder/'mcp_server.py'
        metadata = folder/'backend-source.json'
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        old = json.loads(metadata.read_text('utf-8')) if metadata.is_file() else {}
        current = hashlib.sha256(script.read_bytes()).hexdigest() if script.is_file() else None
        if current is None or (current==old.get('installed_hash') and checksum!=current):
            shutil.copyfile(source,script)
            license_file = source.parent/'LICENSE'
            if license_file.is_file():
                shutil.copyfile(license_file,folder/'LICENSE.bilibili-mcp')
            metadata.write_text(json.dumps(dict(source=str(source),installed_hash=checksum,
                package=json.loads((package/'package.json').read_text('utf-8'))['version'])),encoding='utf-8')
        python = self.root/'.venv/Scripts/python.exe'
        return dict(script=str(script),cwd=str(folder),python=str(python) if python.is_file() else sys.executable)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._closed:
                raise DshBridgeError("DSH 桥已关闭")
            if self._proc is not None and self._proc.poll() is None:
                return
            self._spawn_locked()
            self._handshake_locked()

    def _spawn_locked(self) -> None:
        bin_ts = self.dsh_root / "apps" / "cli" / "src" / "bin.ts"
        tsx = self.dsh_root / "node_modules" / "tsx" / "package.json"
        if not bin_ts.is_file():
            raise DshBridgeError(f"DSH 副本不完整：找不到 {bin_ts}")
        if not tsx.is_file():
            raise DshBridgeError(f"DSH 依赖不完整：找不到 {tsx}")
        self.ensure_profile()
        (self.root / "data" / "tmp").mkdir(parents=True, exist_ok=True)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = self._log_path.open("ab", buffering=0)
        cmd = self._command()
        self._generation += 1
        generation = self._generation
        try:
            from life.supervisor import ProcessTree
            self._tree = ProcessTree(cmd,cwd=str(self.dsh_root),env=self._env(),
                                     pipes=True,text=True,stderr_file=self._stderr)
            self._proc = self._tree.process
        except Exception:
            self._stderr.close()
            self._stderr = None
            raise
        from life.process_access import worker_access
        self.runtime_access = worker_access(self._proc.pid)
        if self.runtime_access.get('warning'):
            log.warning('DSH Windows 运行权限：%s host=%s worker=%s',self.runtime_access['warning'],
                        self.runtime_access['host'],self.runtime_access['worker'])
        self._reader = threading.Thread(target=self._read_loop,
                                        args=(self._proc, generation),
                                        name="dsh-bridge-reader", daemon=True)
        self._reader.start()

    def _command(self) -> list[str]:
        """真正启动 DSH 的命令行；测试用假服务端时可覆盖这一处。"""
        return [self._node_exe(), "--import", "tsx/esm",
                "apps/cli/src/bin.ts", "--profile", PROFILE_NAME]

    def _handshake_locked(self) -> None:
        try:
            init = self._call_locked("initialize", {
                "cwd": str(self.root), "provider": self.provider,
                "model": self.model,
            }, timeout=60.0)
            hello = self._call_locked("qq_life/hello", self._hello_parameters(), timeout=30.0)
            self._verify_identity(hello)
            self._schedule_reconcile()
        except Exception:
            self._kill_locked()
            raise
        log.info("DSH 桥已启动 pid=%s gen=%s scope=%s home=%s server=%s",
                 self._proc.pid if self._proc else "?", self._generation,
                 self.scope, self.home, (init or {}).get("serverInfo"))

    def _schedule_reconcile(self) -> None:
        """握手后对账上一轮遗留的操作；失败只记日志，不影响这一轮使用。"""
        if self._reconciled or self.ledger is None:
            return
        self._reconciled = True

        def run() -> None:
            try:
                done = self.reconcile()
                if done:
                    log.info("启动后对账完成，%s 条遗留操作已落定", len(done))
            except Exception as e:                   # 对账失败不该拖垮对话
                log.warning("启动后对账失败：%s", e)

        threading.Thread(target=run, name="dsh-bridge-reconcile", daemon=True).start()

    def _verify_identity(self, hello: dict[str, Any]) -> None:
        """握手时核对身份：真的是 Bot 自己的 profile 才继续。"""
        if str(hello.get("profile") or "") != PROFILE_NAME:
            raise DshBridgeError(
                f"DSH profile 不是 Bot 的 {PROFILE_NAME}：{hello.get('profile')}")
        if not hello.get("profileOwnedByBot"):
            reason = hello.get("profileOwnedReason") or "未给出原因"
            raise DshBridgeError(f"DSH profile 身份核验失败：{reason}")
        home = hello.get("home")
        if not home or not _same_path(home, self.home):
            raise DshBridgeError(
                f"DSH 运行目录与配置不一致：hello.home={home!r} 配置={str(self.home)!r}")

    def _read_loop(self, proc: subprocess.Popen[str], generation: int) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("DSH stdout 非 JSON：%s", line[:300])
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("method"):
                    if "id" in obj:
                        threading.Thread(target=self._serve_request, args=(obj, proc, generation),
                                         name="dsh-bridge-srv-req",
                                         daemon=True).start()
                    else:
                        if generation == self._generation and proc is self._proc and self._notification_handler:
                            self._notification_handler(obj, generation)
                    continue
                rid = obj.get("id")
                with self._pending_lock:
                    waiter = self._pending.pop(rid, None) if isinstance(rid, int) else None
                if waiter is None:
                    log.debug("DSH 应答没有等待者：%s", str(obj)[:200])
                    continue
                try:
                    waiter.put_nowait(obj)
                except queue.Full:
                    pass
        except Exception as e:                       # 进程被杀、管道关闭
            log.debug("DSH stdout 读取结束：%s", e)
        finally:
            self._fail_pending(proc, generation)

    def _fail_pending(self, proc: subprocess.Popen[str] | None = None,
                      generation: int | None = None) -> None:
        """进程没了就叫醒所有等待者，别让他们等到超时。"""
        # Startup waits for initialize while owning the lifecycle lock. The
        # reader must still wake that waiter if the process exits during boot.
        with self._pending_lock:
            if proc is not None and generation is not None:
                if generation != self._generation or self._proc is not proc:
                    return
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            try:
                waiter.put_nowait(None)
            except queue.Full:
                pass

    def _write(self, wire: dict[str, Any], *, expected_proc=None, expected_generation=None) -> None:
        with self._write_lock:
            proc = self._proc
            if ((expected_proc is not None and proc is not expected_proc)
                    or (expected_generation is not None and self._generation != expected_generation)):
                raise DshBridgeError('旧连接的回调不能写入新 worker')
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise DshBridgeError("DSH 进程没有在运行")
            try:
                proc.stdin.write(json.dumps(wire, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise DshBridgeError(f"写 DSH 请求失败：{e}") from e

    def _send(self, method: str, params: dict[str, Any] | None) -> tuple[int, queue.Queue]:
        with self._pending_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise DshBridgeError("DSH 进程没有在运行")
            self._next_id += 1
            rid = self._next_id
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[rid] = waiter
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                         "params": params or {}})
        except Exception:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise
        return rid, waiter

    def _await(self, rid: int, waiter: queue.Queue, method: str,
               timeout: float | None) -> Any:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                self._discard(rid)
                raise DshBridgeError(f"DSH 请求超时：{method}")
            try:
                msg = waiter.get(timeout=remain)
            except queue.Empty:
                self._discard(rid)
                raise DshBridgeError(f"DSH 请求超时：{method}") from None
            if msg is None:
                self._discard(rid)
                code = self._proc.poll() if self._proc else None
                raise DshBridgeError(
                    f"DSH 进程已退出（code={code}）：{method}\n"
                    f"{self._log_tail()}{self._exit_hint()}")
            err = msg.get("error")
            if err:
                if isinstance(err, dict):
                    raise DshRpcError(err.get('code'),err.get('message'))
                raise DshBridgeError(f"DSH 错误：{err}")
            return msg.get("result")

    def _discard(self, rid: int) -> None:
        with self._pending_lock:
            self._pending.pop(rid, None)

    def _call_locked(self, method: str, params: dict[str, Any] | None = None,
                     timeout: float | None = None) -> Any:
        rid, waiter = self._send(method, params)
        return self._await(rid, waiter, method, timeout)

    def _call(self, method: str, params: dict[str, Any] | None = None,
              timeout: float | None = None) -> Any:
        self._ensure_started()
        return self._call_locked(method, params, timeout)

    def _log_tail(self, n: int = 2000) -> str:
        try:
            return self._log_path.read_bytes()[-n:].decode("utf-8", "replace")
        except Exception:
            return ""

    def _exit_hint(self) -> str:
        """进程刚起就退时，把最常见的环境问题直接说清楚。"""
        tail = self._log_tail()
        if "EPERM" not in tail:
            return ""
        return ("\n提示：DSH 启动时写不了自己的 profile 配置（EPERM）。"
                "把 node.exe 与 DSH 目录从受控文件夹访问或杀软限制里放行，"
                "或者把 dsh_home 指到一个 node 能写的目录。")

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        tree,self._tree = self._tree,None
        if tree is not None:
            tree.stop()
        elif proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if proc is not None:
            if self._reader and self._reader is not threading.current_thread():
                self._reader.join(timeout=2)
            for stream in (proc.stdin,proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self._stderr is not None:
            try:
                self._stderr.close()
            except Exception:
                pass
            self._stderr = None
        self._fail_pending()

    def _stop_worker_locked(self) -> None:
        """停掉 worker 但不关闭桥：卸载/替换插件后下一次调用会重新拉起。"""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self._call_locked("shutdown", {}, timeout=5.0)
            except Exception:
                pass
        self._kill_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_worker_locked()

    def restart_worker(self) -> None:
        """换掉当前 worker，下次调用按最新 profile 重新拉起（新插件在这里生效）。"""
        with self._lock:
            self._stop_worker_locked()

    def purge_sessions(self,session_ids):
        """Erase retired bot session copies with the owned SDK fully stopped."""
        ids = set(map(str,session_ids))
        import re
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+',sid) for sid in ids):
            raise ValueError('invalid_retired_session_id')
        with self._lock:
            self._stop_worker_locked()
            marker = json.loads((self.home/'profiles/sdk/.qq-bot-owned.json').read_text('utf-8'))
            if marker.get('owner')!='qq-life' or Path(marker['home']).resolve()!=self.home.resolve():
                raise ValueError('bot_home_ownership_mismatch')
            candidates = [p for p in (self.home/'sessions').glob('*/*') if p.name in ids]
            for target in candidates:
                for entry in [target,*target.rglob('*')]:
                    if entry.is_symlink() or getattr(entry.lstat(),'st_file_attributes',0)&0x400:
                        raise ValueError('linked_session_purge_target')
                    if self.home.resolve() not in entry.resolve().parents:
                        raise ValueError('session_purge_outside_home')
            admissions = self.home/'qq-life/inputs.jsonl'
            if admissions.exists():
                lines = [line for line in admissions.read_text('utf-8').splitlines()
                         if line.strip() and json.loads(line)['sessionId'] not in ids]
                temporary = admissions.with_suffix('.purging')
                with temporary.open('w',encoding='utf-8',newline='\n') as output:
                    output.write(''.join(line+'\n' for line in lines)); output.flush(); os.fsync(output.fileno())
                os.replace(temporary,admissions)
            for target in candidates:
                shutil.rmtree(target)
            return dict(sessions=len(ids),directories=len(candidates))

    def _run_cli(self, args: list[str], timeout: float = 300.0) -> tuple[int, str]:
        """用 DSH 自己的 CLI 跑一次管理工作（如 plugin remove）。

        本 profile 没有 HMR：卸载必须由外部进程完成，不能在 worker 里做。
        """
        cmd = [self._node_exe(), "--import", "tsx/esm",
               "apps/cli/src/bin.ts", *args]
        try:
            proc = subprocess.run(cmd, cwd=str(self.dsh_root), env=self._env(),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return 124, f"DSH CLI 超时：{' '.join(args)}"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def plugin_remove(self, bundle: str, timeout: float = 300.0) -> dict[str, Any]:
        """卸掉一个插件包：先停 worker，再让 CLI 把 remove 转发给 pnpm。"""
        name = str(bundle or "").strip()
        if not name:
            raise DshBridgeError("卸载插件要给出包名")
        with self._lock:
            self._stop_worker_locked()
        code, output = self._run_cli(
            ["plugin", "--profile", PROFILE_NAME, "remove", name], timeout)
        return {"bundle": name, "exitCode": code, "output": output[-4000:]}

    # ---------------------------------------------------------------- 服务端反向请求

    def register_handler(self, method: str,
                         handler: Callable[[dict[str, Any]], Any]) -> None:
        """注册服务端发起请求的处理函数（审批、授权等）。"""
        self._handlers[str(method)] = handler

    def set_notification_handler(self, handler):
        """Install a nonblocking queue adapter; the reader never writes SQLite."""
        self._notification_handler = handler

    def session_input(self, payload):
        return self._call('qq_life/session/input', payload)

    def input_status(self, session_id, input_id):
        return self._call('qq_life/session/input_status', dict(session_id=session_id, input_id=input_id))

    def session_events(self, session_id, after_seq, limit=200, generation=None):
        return self._call('qq_life/session/events', dict(session_id=session_id, after_seq=after_seq, limit=limit,generation=generation))

    def resource_events(self,session_id,after_seq=-1,*,generation=None):
        return self._call('qq_life/session/usage',dict(session_id=session_id,after_seq=after_seq,generation=generation))

    def original_result(self,session_id,result_ref,*,generation=None):
        return self._call('qq_life/session/original',dict(session_id=session_id,result_ref=result_ref,generation=generation))

    def session_control(self, session_id, generation, **control):
        return self._call('qq_life/session/control', dict(session_id=session_id, generation=generation, **control))

    def set_approver(self,
                     approver: Callable[[str, dict[str, Any]], Any] | None) -> None:
        """把审批人接到 DSH 的反向请求上；None 恢复 fail closed。"""
        if approver is None:
            self._handlers["approval/request"] = self._deny_server_request
            self._handlers["qq_life/approval"] = self._deny_server_request
            return

        def _bridge(method: str):
            def handler(params: dict[str, Any]):
                return approver(method, params)
            return handler

        for name in ("approval/request", "qq_life/approval"):
            self._handlers[name] = _bridge(name)

    def _deny_server_request(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"outcome": "rejected", "reason": "桥没有接审批人，按拒绝处理"}

    def _serve_request(self, msg: dict[str, Any], proc=None, generation=None) -> None:
        if proc is not None and (self._proc is not proc or self._generation != generation):
            return
        method = str(msg.get("method") or "")
        rid = msg.get("id")
        handler = self._handlers.get(method)
        try:
            if handler is None:
                raise DshBridgeError(f"没有注册处理器的服务端请求：{method}")
            params = dict(msg.get('params') or {})
            if generation is not None:
                params['_bridge_generation'] = generation
            result = handler(params)
            self._write({"jsonrpc": "2.0", "id": rid, "result": result},
                        expected_proc=proc, expected_generation=generation)
        except Exception as e:
            log.warning("DSH 服务端请求 %s 失败：%s", method, e)
            try:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32001,
                                       "message": f"{type(e).__name__}: {e}"}},
                            expected_proc=proc, expected_generation=generation)
            except Exception:
                pass

    # ---------------------------------------------------------------- 账本

    def _ledger_put(self, op: str, tool: str, args: dict[str, Any], rhash: str,
                    status: str, *, scope: str | None = None, plan_id: str = "",
                    step_id: str = "", note: str = "") -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.bridge_op_put({
                "id": op, "scope_id": scope or self.scope, "plan_id": plan_id,
                "step_id": step_id, "request_hash": rhash,
                "context": json.dumps({"tool": tool}, ensure_ascii=False),
                "tool_name": tool, "arguments": json.dumps(args, ensure_ascii=False),
                "status": status, "worker_generation": str(self._generation),
                "tool_generation": 0, "result": "", "artifact_refs": "[]",
                "updated_at": time.time(), "note": note[:500],
            })
        except Exception as e:
            raise DshBridgeError(f"DSH 操作没能落盘（{op}）：{e}") from e

    def _ledger_update(self, op: str, status: str,
                       snap: dict[str, Any] | None = None,
                       note: str | None = None) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.bridge_op_update(
                op, status,
                result=json.dumps(snap, ensure_ascii=False) if snap else None,
                note=note, worker_generation=str(self._generation))
        except Exception as e:
            log.warning("DSH 操作状态落盘失败 %s：%s", op, e)

    # ---------------------------------------------------------------- 协议

    def hello(self) -> dict[str, Any]:
        hello = self._call("qq_life/hello", self._hello_parameters(), timeout=30.0)
        self._verify_identity(hello)
        self.loaded_adapter_version = str(hello.get('roleAdapterVersion') or '')
        return hello

    def _hello_parameters(self):
        return dict(continuity_protocol=1,continuity_enabled=bool(getattr(self.cfg,'continuity_enabled',False)),
                    capability_protocol=1,media_adapter_protocol=1)

    def current_agent_catalog(self,session_id,generation):
        return self._call('qq_life/session/catalog',dict(session_id=session_id,generation=generation),timeout=30)

    def invalidate_methods(self,session_id,generation):
        return self._call('qq_life/session/methods_changed',dict(session_id=session_id,generation=generation),timeout=30)

    def tools(self, limit: int = 200, *, scope: str | None = None) -> list[dict[str, Any]]:
        self._ensure_started()      # 先确定 worker 代数，再取执行域
        rows: list[dict[str, Any]] = []
        cursor = 0
        for _ in range(20):
            page = self._call("qq_life/tools/list", {
                "executionScope": scope or self.scope, "limit": int(limit), "cursor": cursor,
            }, timeout=60.0)
            rows.extend(page.get("tools") or [])
            nxt = page.get("nextCursor")
            if nxt is None:
                return rows
            cursor = int(nxt)
        raise DshBridgeError("DSH 工具列表超过 20 页，停止分页")

    def execute(self, tool: str, arguments: dict[str, Any] | None = None,
                *, operation_id: str | None = None, deadline_ms: int = 120000,
                scope: str | None = None, plan_id: str = "",
                step_id: str = "") -> dict[str, Any]:
        """执行一次真实工具调用；同一编号重放返回旧结果，绝不重复执行。"""
        tool = str(tool or "").strip()
        if not tool:
            raise DshBridgeError("工具名不能为空")
        args = arguments or {}
        if not isinstance(args, dict):
            raise DshBridgeError("arguments 必须是对象")
        op = operation_id or f"op-{uuid.uuid4().hex[:20]}"
        rhash = request_hash(tool, args)
        if self.ledger is not None:
            row = self.ledger.bridge_op_get(op)
            if row is not None:
                if plan_id and str(row.get('plan_id') or '') != plan_id:
                    raise DshBridgeError('操作编号属于其他任务，拒绝跨任务回读或重放')
                if str(row.get("request_hash") or "") != rhash:
                    raise DshBridgeError(
                        f"操作编号 {op} 已经用于另一份请求（参数不同），拒绝复用")
                if row.get("status") in TERMINAL and row.get("result"):
                    try:
                        return json.loads(str(row["result"]))
                    except Exception:
                        pass
                return self._reconcile_one(op, note="启动后按编号对账")
        self._ensure_started()      # 先确定 worker 代数，再取执行域
        self._ledger_put(op, tool, args, rhash, "dispatching", scope=scope,
                         plan_id=plan_id, step_id=step_id, note="已发出")
        try:
            snap = self._call("qq_life/execute", {
                "operationId": op, "executionScope": scope or self.scope,
                "tool": tool, "arguments": args, "deadlineMs": int(deadline_ms),
            }, timeout=max(10.0, deadline_ms / 1000.0 + 20.0))
        except DshBridgeError as e:
            snap = self._readback(op)
            if snap is not None and str(snap.get("requestHash") or rhash) != rhash:
                denied = {"operationId": op, "status": "denied", "settled": True,
                          "tool": tool,
                          "error": f"操作编号 {op} 已经用于另一份请求（参数不同），"
                                   "拒绝复用；旧结果没有被当成这次的结果"}
                self._ledger_update(op, "denied", snap=denied, note="编号复用被拒绝")
                return denied
            if snap is not None:
                self._ledger_update(op, str(snap.get("status") or "unknown"),
                                    snap=snap, note="按错误回读")
                return snap
            rejected = not _is_transport_error(e)
            status = "denied" if rejected else "unknown"
            snap = {"operationId": op, "status": status, "settled": rejected,
                    "tool": tool,
                    "error": ("DSH 拒绝了这次调用：" if rejected else "传输层没拿到结果：")
                             + str(e)}
            self._ledger_update(op, status, snap=snap, note=str(e)[:300])
            return snap
        self._ledger_update(op, str(snap.get("status") or "unknown"), snap=snap)
        return snap

    def _readback(self, op: str) -> dict[str, Any] | None:
        """出错时先问 DSH 自己记了什么，别用本地猜测覆盖真实状态。"""
        try:
            return self.status(op)
        except DshBridgeError:
            return None

    def status(self, operation_id: str) -> dict[str, Any]:
        snap = self._call("qq_life/status", {"operationId": operation_id}, timeout=30.0)
        self._ledger_update(operation_id, str(snap.get("status") or "unknown"), snap=snap)
        return snap

    def cancel(self, operation_id: str, *, wait_ms: int = 15000) -> dict[str, Any]:
        """请求停止并等它真的停；没停下就保持 stopping，不谎报已取消。"""
        snap = self._call("qq_life/cancel", {
            "operationId": operation_id, "waitMs": int(wait_ms),
        }, timeout=max(30.0, wait_ms / 1000.0 + 20.0))
        settled = bool(snap.get("settled"))
        self._ledger_update(
            operation_id, str(snap.get("status") or "unknown"), snap=snap,
            note="取消已确认" if settled else "取消请求已发出，工具仍在停止")
        return snap

    def _reconcile_one(self, op: str, note: str = "") -> dict[str, Any]:
        try:
            return self.status(op)
        except DshBridgeError as e:
            snap = {"operationId": op, "status": "unknown", "settled": False,
                    "error": f"DSH 不认识这个操作：{e}"}
            self._ledger_update(op, "unknown", snap=snap, note=(note or "对账失败"))
            return snap

    def reconcile(self, limit: int = 50) -> list[dict[str, Any]]:
        """重启后把没落终态的操作逐条问一遍，结果写回 life.db。"""
        if self.ledger is None:
            return []
        out: list[dict[str, Any]] = []
        for row in self.ledger.bridge_op_unsettled(limit):
            out.append(self._reconcile_one(str(row["id"]), note="重启对账"))
        if out:
            log.info("DSH 桥重启对账 %s 条", len(out))
        return out


def result_text(snap: dict[str, Any] | None, limit: int = 4000) -> str:
    """把 execute 快照翻成给 Bot 看的文本：状态、正文、错误都要留着。"""
    if not isinstance(snap, dict):
        return "DSH 没有返回结果快照。"
    status = str(snap.get("status") or "unknown")
    op = str(snap.get("operationId") or "")
    parts: list[str] = [f"DSH {snap.get('tool') or '工具'}：{status}"
                        + (f"（{op}）" if op else "")]
    result = snap.get("result")
    if isinstance(result, dict):
        if result.get("isError"):
            parts.append("工具报告失败。")
        blocks = result.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        parts.append(text)
        if not any(isinstance(b, dict) and b.get("type") == "text"
                   for b in (blocks or [])):
            value = result.get("value")
            if value is not None:
                parts.append(json.dumps(value, ensure_ascii=False)[:limit])
        err = result.get("error")
        if isinstance(err, dict) and err.get("message"):
            parts.append(f"错误：{err['message']}")
    elif result is not None:
        parts.append(json.dumps(result, ensure_ascii=False)[:limit])
    if snap.get("error") and not isinstance(result, dict):
        parts.append(f"错误：{snap['error']}")
    if status == "denied":
        parts.append("这次请求被拒绝，工具没有执行。")
    if status == "unknown":
        parts.append("结果未知：重启后或断线时按 operation_id 查一次再决定，别重复执行。")
    elif status == "stopping":
        parts.append("还在停：工具没有真正结束，稍后再查状态。")
    text = "\n".join(p for p in parts if p)
    return text[:limit]
