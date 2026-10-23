"""Shared action accounting; cognition and execution consult the same ledger."""
import time
from .work_queue import WorkQueue, calendar_day


class Budget:
    def __init__(self, store, cfg):
        self.store, self.cfg, self.repo = store, cfg, store.repo
        self.queue = WorkQueue(self.repo)
        self.root = 'agent:' + str(getattr(cfg, 'life_agent', 'default'))

    @property
    def per_hour(self):
        return max(0, int(getattr(self.cfg, 'action_per_hour', 500)))

    @property
    def per_day(self):
        return max(0, int(getattr(self.cfg, 'action_per_day', 0)))

    def used(self, category):
        row = self.repo._one('SELECT used FROM budget_ledger WHERE root_id=? AND day=? AND category=?',
            (self.root, calendar_day(time.time()), category))
        return float(row['used']) if row else 0

    def actions_today(self):
        return int(self.used('actions'))

    def actions_last_hour(self):
        return int(self.used('actions_hour:' + str(int(time.time()) // 3600)))

    def seconds_today(self):
        return int(self.used('execution_seconds'))

    def allow_act(self, ignore_quiet=False, ignore_hourly=False):
        if self.per_day and self.actions_today() >= self.per_day:
            return False, '今日行动预算已用完'
        if self.per_hour and not ignore_hourly and self.actions_last_hour() >= self.per_hour:
            return False, '本小时行动预算已用完'
        return True, ''

    def reserve(self, operation_id):
        key = 'budget_action:' + operation_id
        with self.repo.tx():
            if self.repo._one('SELECT 1 FROM meta WHERE k=?', (key,)):
                return True
            if not self.allow_act()[0]:
                return False
            for category, limit in [('actions', self.per_day),
                    ('actions_hour:' + str(int(time.time()) // 3600), self.per_hour)]:
                if not self.queue.spend(self.root, category, 1, limit or float('1e30')):
                    raise RuntimeError('budget_reservation_conflict')
            self.repo._exec('INSERT INTO meta(k,v) VALUES(?,?)', (key, str(time.time())))
            return True

    def charge_time(self, operation_id, seconds):
        key = 'budget_time:' + operation_id
        with self.repo.tx():
            if self.repo._one('SELECT 1 FROM meta WHERE k=?', (key,)):
                return
            self.queue.spend(self.root, 'execution_seconds', max(0, seconds), float('1e30'))
            self.repo._exec('INSERT INTO meta(k,v) VALUES(?,?)', (key, '1'))

    def summary(self):
        day = str(self.per_day) if self.per_day else '不限'
        hour = str(self.per_hour) if self.per_hour else '不限'
        return f'今天动手 {self.actions_today()} 次（每日上限 {day}），本小时 {self.actions_last_hour()} 次（上限 {hour}），约 {self.seconds_today()//60} 分钟'
