"""Run the bot under its independent native release/rollback controller."""
import argparse
from pathlib import Path
import time
import tomllib
import logging
from logging.handlers import RotatingFileHandler

log = logging.getLogger('life.supervisor')

from life.release import NativeWorker, ReleaseController
from life.supervisor import HostSupervisor


def main():
    root = Path(__file__).resolve().parent
    logs = root/'data/logs'
    logs.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[logging.StreamHandler(),RotatingFileHandler(logs/'lifesupervisor.log',
            maxBytes=4*1024*1024,backupCount=2,encoding='utf-8')],force=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(root / "config.toml"))
    parser.add_argument("--control")
    args = parser.parse_args()
    config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    database = Path(config.get("life_db", "data/life.db"))
    if not database.is_absolute():
        database = root / database
    control = Path(args.control) if args.control else database.parent / "life-control"
    controller = ReleaseController(root, control, NativeWorker())
    supervisor = HostSupervisor(controller, args.config)
    # A separate supervisor cannot share this kernel-held singleton lock.
    lock_path = controller.control / "supervisor.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if not controller.queued_deployments():
                supervisor.start()
            previous = ""
            while True:
                state = supervisor.tick()
                if getattr(supervisor,'restart_controller',False):
                    return 42
                if state != previous:
                    notes = {"starting": "正在启动", "evidence_pending": "运行中，等待实际任务验证",
                             "channel_offline": "运行中，QQ通道未连接；请检查 NapCat 登录",
                             "observed": "运行正常", "rolled_back": "已恢复上一版本",
                             "restarting": "正在重启", "blocked": "连续启动失败，请查看日志"}
                    log.info("运行状态：%s",notes.get(state, state))
                    previous = state
                time.sleep(2)
        except KeyboardInterrupt:
            log.info('收到主动中止，停止 Bot')
        except Exception:
            log.exception('发布控制器异常退出')
            raise
        finally:
            supervisor.stop()


if __name__ == "__main__":
    raise SystemExit(main())
