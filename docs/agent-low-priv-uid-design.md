# 低权限 uid 跑子 agent：隔离兜底方案的设计验证

日期：2026-11-17。执行：prerequisites-engineer（t30）。性质：**设计验证，未上生产**。
环境：云端 `47.104.8.84`（Ubuntu 24.04，Node v22.19.0 @ `/opt/qq-deepseek/toolchain/node/bin/node`）。
边界：没有改生产服务的运行用户，没有改 `/opt`、`/var/lib` 的权限，所有权限实验都在 `/tmp/qq-t30-runtime`、`/var/lib/xiaoyu-agent` 里做；没有写生产 `life.db`，没有发 QQ。

> **这层没上生产之前，沙箱仍然是敞开的。** 本文证明的是"这条路走得通"，不是"已经隔离了"。

---

## 1. 为什么要这一层

沙箱修复做了四轮，写路径修了两层仍有绕过，读路径完全没有 fence（`checkedTarget` 只管写，`readText` 不过它）。也就是说：

- DSH 侧的文件工具（`bash`、`read_image`、`working_directory`）在 `danger-full-access` 下可以读到 `/opt/qq-deepseek`、`/var/lib/companion`；
- 任何"用应用层正则/路径检查补读 fence"的做法，都要跟着 DSH 的实现走，DSH 一改就可能漏；
- 内核权限不依赖 DSH 是否实现了 fence：**换个 uid 跑，读不到的路径就是读不到**。

低权限 uid 是纵深防御的第二层（第一层是 landlock），它挡的不是"DSH 的 bug"，而是"任何通过 exec 拿到代码执行的路径"。

---

## 2. 用户设计

实测创建（`useradd --system`）：

```
xiaoyu-agent:x:995:986:xiaoyu low-priv agent:/var/lib/xiaoyu-agent:/usr/sbin/nologin
groups: xiaoyu-agent
shell: /usr/sbin/nologin
sudo_rule: 无
root_group_member: NO
wheel_member: NO
```

要点：
- 独立组 `xiaoyu-agent(986)`，**不在** `root`、`wheel`、`companion`、`napcat` 任何组里；
- shell 是 `/usr/sbin/nologin`，没有密码，不能登录；
- 家目录 `/var/lib/xiaoyu-agent`，由它自己拥有，`750`；
- 不接受任何 sudo 规则（实测 `grep -rl /etc/sudoers*` 无命中）。

---

## 3. 权限设计

| 路径 | 目标 | 现状（实测） | 说明 |
| --- | --- | --- | --- |
| `/opt/qq-deepseek` | `750 companion:companion` | `755 root:root`，`life/` 是 `755 companion:companion` | **必须收**，否则所有人可读源码与配置加载逻辑 |
| `/root` | `700 root:root` | `700 root:root` | 已经满足 |
| `/var/lib/companion` | `700` | `755 root:root`，`xiaoyu/` 是 `700 companion:companion` | 子目录已挡；父目录建议一并收 |
| workdir | agent 可读写 | 新目录 `/var/lib/xiaoyu-agent/work` 属主 agent、`750` | 每任务一个子目录 |
| node / dsh 运行时 | 只读可执行 | `toolchain/node` 是 `admin:admin 755`、node 二进制 `-rwxr-xr-x` | 见 §4 的坑 |
| 出网 | 保留 | 实测 `socket.create_connection(('api.deepseek.com',443))` 成功 | 要调模型 API |

### 3.1 运行时必须单独放出来（本任务最重要的坑）

`/opt/qq-deepseek/toolchain/node/bin/node` 属主 `admin:admin 755`，而 `/opt/qq-deepseek` 根是 `root:root 755`——**现在任何本地用户都能直接执行 node**，因为父目录是 755。

一旦按上表把 `/opt/qq-deepseek` 收成 `750 companion:companion`，node 的路径就**不可达**了：遍历路径上任一层缺 `x` 权限，下面的可执行文件就进不去。实测用 `/tmp` 里的等价款模拟了这一层：

```
=== 4. 同一组探针，在父目录收紧后重跑（/tmp 内模拟，未动生产） ===
$ cat /tmp/qq-t30-runtime/vendor/dsh-runtime/lib/bin.js > /dev/null
cat: ... Permission denied   exit=1
$ echo x > /tmp/qq-t30-runtime/TEST
bash: line 1: ... Permission denied   exit=1
```

结论：**node 与 dsh 运行时必须搬到独立路径**（例如 `/opt/xiaoyu-runtime/`，`root:root 755`，内部文件 `a+rX`），业务代码 `/opt/qq-deepseek` 才能安全收成 750。

### 3.2 运行时副本的布局（第二个坑，调试了很久）

DSH 的 SDK 启动方式是 `node <runtime>/lib/bin.js --profile sdk-minimal`。把 `lib/bin.js` 拷到新位置后，它 import 的 `@deepseek-ai/dsh-app-boot` 是 **ESM 解析**，搜索路径是：

```
<runtime>/lib/node_modules
<runtime>/node_modules
<runtime>/../node_modules   ...
```

实测（作为 agent 跑 node 打印 resolve 路径）拿到了完整的搜索顺序。真正跑通的做法是**整棵已安装的 CLI 树一起搬**：

```
/var/lib/xiaoyu-agent/rt/lib/node_modules/@deepseek-ai/dsh/{lib,node_modules,package.json,...}
```

然后用**包自带的** `@deepseek-ai/dsh/lib/bin.js` 运行（不是 `lib/bin.js` 那份外壳）：

```
--- control: 原始文件 + 原始模块（root） ---   0.2.1-rc.2
--- target: 拷贝的文件 + 拷贝的模块（xiaoyu-agent） ---   0.2.1-rc.2
```

这一步证明了 **agent uid 能执行 node、能读到拷贝的 dsh、能跑出 CLI 版本**。把 bin.js 单独放到 `<root>/lib/bin.js` 而把包放在别处会一直 `ERR_MODULE_NOT_FOUND`，不要那样铺。

---

## 4. 六项关键准入测试（逐条命令与输出）

全部用 `sudo -u xiaoyu-agent -- bash -c '<cmd>'` 执行，生产权限**未改动**。

| # | 命令 | 期望 | 实测结果 |
| --- | --- | --- | --- |
| 1 | `cat /opt/qq-deepseek/life/companion.py` | 拒绝 | `cat: /opt/qq-deepseek/life/companion.py: Permission denied`，exit=1 |
| 2 | `cat /root/.ssh/authorized_keys` | 拒绝 | `cat: /root/.ssh/authorized_keys: Permission denied`，exit=1 |
| 3 | `cat /var/lib/companion/xiaoyu/config.toml` | 拒绝 | `cat: .../config.toml: Permission denied`，exit=1 |
| 4 | `echo x > /opt/qq-deepseek/TEST` | 拒绝 | `bash: line 1: /opt/qq-deepseek/TEST: Permission denied`，exit=1 |
| 5 | `echo x > /var/lib/xiaoyu-agent/work/test.txt` | 成功 | 写入并 `cat` 出 `x`，exit=0 |
| 6 | `/opt/qq-deepseek/toolchain/node/bin/node --version` | 可执行 | `v22.19.0`，exit=0 |

**六项全部符合预期。** 第 6 项现在能过，恰恰是因为 `/opt/qq-deepseek` 还是 755；按 §3.1 收紧后必须改走 `/opt/xiaoyu-runtime`。

补充实测：
- 生产 `life/companion.py` 是 `-rw------- companion:companion`，`config.toml` 同样 `600`——**即使目录权限放开也读不到**，这是当前最有效的一层；
- `life/` 目录是 `755 companion:companion`，意味着**目录可列、文件名可见**，只是文件内容不可读。收紧到 750 才能连文件名一起藏住。

---

## 5. 子 agent 链路实测

| 环节 | 结果 | 证据 |
| --- | --- | --- |
| 启动 node | 通过 | `--version` → `v22.19.0` |
| 启动 dsh CLI | 通过 | 拷贝的 CLI 树 → `0.2.1-rc.2` |
| 读写自己的 workdir | 通过 | `test.txt`、`request.json`、`result.json` 都由 agent 写出 |
| 读到 request.json | 通过 | `cat` 输出完整 JSON |
| 写 result_path | 通过 | `result.json` 落盘，含 `stage`/`error` 字段 |
| 出网（模型 API） | 通过 | `create_connection(('api.deepseek.com',443))` → `egress ok` |
| **跑通一次完整模型调用** | **未完成** | 见下 |

### 5.1 未完成的那一项：为什么会卡

我用本地 stub（`127.0.0.1:18099`，OpenAI 兼容，返回固定 `ok`，不花钱）做模型端点，agent 侧始终在"误读 runtime 路径"处失败：

- `dsh_root` 指错过两次（`.../rt/lib`、`.../rt`），报 `Cannot find module '.../lib/bin.js'`；
- 用 `<root>/lib/bin.js` 外壳那份会一直 `ERR_MODULE_NOT_FOUND: @deepseek-ai/dsh-app-boot`（§3.2）；
- 抄了可用布局之后，SDK 端仍未走到模型请求（stub `POST count: 0`）。

**结论：`node + DSH 启动` 与 `权限边界` 都已实测通过；`SDK 端到端跑通一次模型调用` 没有跑通，是没做完，不是证伪。** 这条要在上线前补测，验收标准就是 stub 收到 ≥1 次 `POST` 且 `finish_reason=completed`。

---

## 6. 对生产的影响面

上线时要动的东西（按风险排序）：

1. **新增系统用户与目录**：`xiaoyu-agent`、`/var/lib/xiaoyu-agent`（`750`）、每任务 workdir。风险低。
2. **新建 `/opt/xiaoyu-runtime`**：node + 完整 dsh CLI 树（约 858M，硬链接可省空间），`root:root 755`，内部 `a+rX`。这是唯一给 agent 的新读取面，必须只含运行时，**不含** `life/`、`config.toml`、`data/`。
3. **收紧 `/opt/qq-deepseek` → `750 companion:companion`**。风险中：任何以其他身份读它的脚本/服务会同时断掉（要逐项核对 `companion.service`、`companion-embedding.service`、nginx、备份脚本、`tools/*` 里以 root 跑的东西）。服务本身以 `companion` 身份运行，属主不变，不受影响。
4. **收紧 `/var/lib/companion` → `700` 并确认 `xiaoyu/` 仍是 `700 companion`。** 服务同身份，不受影响。
5. **执行层改动**：`companion_executor.py` 的 DSH 分支用 `setpriv --reuid=995 --regid=986 --init-groups` 起子进程，cwd 指到 workdir，`DSH_HOME` 指到 agent 自己的 home，环境里**只放** `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`（不放 `HOME`、不放 `companion` 的任何路径）。
6. **预算/并发**：uid 与并发配额无关，仍需应用层信号量。

要特别注意：**子 agent 拿不到 DSH profile 里的插件依赖**。现在生产的 `sdk` profile 依赖 `@qq-life/bridge-core` 等本地 link，这些在 `/opt/qq-deepseek` 收 750 后 agent 读不到。要么把 agent 用的 profile 单独固化进 `/opt/xiaoyu-runtime`，要么接受 agent 只能用 `sdk-minimal`。

---

## 7. 落地步骤

1. `groupadd --system xiaoyu-agent && useradd --system --gid xiaoyu-agent --home-dir /var/lib/xiaoyu-agent --shell /usr/sbin/nologin xiaoyu-agent`（本任务已在生产执行这一步，无副作用：没有任何服务引用它）。
2. 建 `/opt/xiaoyu-runtime`：拷 node + 完整 `dsh/lib` 树，`chown -R root:root`，`chmod -R a+rX`，**验证** `sudo -u xiaoyu-agent -- /opt/xiaoyu-runtime/toolchain/node/bin/node --version` 与拷贝的 CLI `--version`。
3. 建 `/var/lib/xiaoyu-agent/{work,bin,dsh-home}`，属主 agent，`750`，`dsh-home` 从生产 `sdk-minimal` profile 拷一份模板。
4. 先**不改** `/opt/qq-deepseek` 权限，只把执行层改成 `setpriv` 起子进程，用一个测试任务跑通（stub 模型）。
5. 跑通后，再把 `/opt/qq-deepseek` 收成 `750 companion:companion`，立刻回归：服务能起、能回话、能派一次 phone/computer 动作。
6. 逐项补测 §5 的端到端，并写进验收清单。

## 8. 回滚

- 权限回滚：`chmod 755 /opt/qq-deepseek /var/lib/companion`（一条命令，无数据变化）。
- 执行层回滚：把 `setpriv` 那行改回原样（`command` 直接起子进程），重启 `companion.service`。
- 用户回滚：`userdel xiaoyu-agent && groupdel xiaoyu-agent`，删 `/var/lib/xiaoyu-agent`、`/opt/xiaoyu-runtime`。
- 全部动作都不碰 `life.db`、`config.toml`、人格文件；回滚不需要动数据。

## 9. 本任务在生产上留下的东西

- 新增系统用户 `xiaoyu-agent`（uid 995 / gid 986）与组——**这是唯一的生产变更**，无人引用，删掉只需 `userdel/groupdel`。
- `/tmp/qq-t30-runtime`（副本，可删）、`/var/lib/xiaoyu-agent/`（`rt/` 858M 硬链接副本 + `work/` + `bin/`，可删）。
- 生产 `/opt`、`/var/lib` 权限**未改**；`companion.service` **未重启**；`life.db` 未写；未发 QQ。

## 10. 一句话

权限模型在这一层是成立的：六项准入测试全对，node 与 dsh 能以 `xiaoyu-agent` 跑起来，生产代码与主人数据都读不到。**但 SDK 端到端那一步没跑完，而且这层还没上生产——所以现在的小鱼沙箱仍然是敞开的，不能对外说"已经隔离"。**
