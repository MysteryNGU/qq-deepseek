#!/bin/sh
# 只读：确认 dsh_bin（必须是一个真实可执行文件，SDK 会用 subprocess.Popen 直接起它）
N=/opt/qq-deepseek/toolchain/node/bin/node
R=/opt/qq-deepseek/vendor/dsh-runtime
echo "node exists: $([ -x "$N" ] && echo yes || echo no)  path=$N"
echo "bin.js exists: $([ -f "$R/lib/bin.js" ] && echo yes || echo no)  path=$R/lib/bin.js"
echo
echo '=== 如果 dsh_bin 指到 node，SDK 会把 bin.js 当脚本传进去；argv 变成：'
echo "  \"$N\" --profile sdk-minimal"
echo '  —— bin.js 根本不在 argv 里，node 会报「无法打开 main 模块」'
echo
echo '=== 对照：executor 走的 companion_dsh_worker 是怎么起它的（用 _launch_args）'
grep -n "_launch_args\|launch =" /opt/qq-deepseek/life/companion_dsh_worker.py
echo
echo '=== 现有 SDK 包能否解析出 bundled 运行时'
/opt/qq-deepseek/.venv-companion/bin/python -c "
try:
    from deepseek_harness_runtime import resolve_bundled_launch_args
    print('bundled args:', resolve_bundled_launch_args())
except Exception as e:
    print('bundled unavailable:', type(e).__name__, e)
"
