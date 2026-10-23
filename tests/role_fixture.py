"""Disposable role fixture with a fake transport and isolated database."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life.agent_session import SessionFailure
from life.expression import Expressions
from life.media import MediaService
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime
from life.self_change import SelfChange
from life.voice_catalog import VoiceCatalog


async def immediate(_delay):
    return None


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'persona.md').write_text('You are a curious character.', encoding='utf-8')
        self.life = LifeRuntime(self.root / 'life.db')
        self.scope = self.life.scope('qq:private:1', '1')
        self.cfg = SimpleNamespace(owner=1, persona='persona.md', public_tools=[],
                                   action_per_hour=500, action_per_day=0, idle_seconds=45)
        self.bridge = SimpleNamespace(_generation=1,
            model='offline-review-fixture',
            session_events=lambda *args, **kwargs: dict(events=[], has_more=False, agent_status='idle'),
            session_control=lambda *args, **kwargs: {},
            _proc=SimpleNamespace(poll=lambda: None))
        self.media = MediaService(self.life, self.root / 'assets')
        self.voices = VoiceCatalog(self.life, self.media)
        self.sent = []
        self.expressions = Expressions(self.life, self.media, self.voices, self.transport, sleep=immediate)

        async def send(scope, logical, proposal, sources):
            return await self.expressions(scope, logical, proposal, sources)
        send.accepts_plan = True
        self.role = RoleAgent(self.life, self.bridge, self.cfg, self.root, send)
        self.binding = self.role.sessions.binding(self.scope)
        self.event, _ = self.life.ingest_message(self.scope, 'Review fixture question')

    async def transport(self, action, params):
        self.sent.append(params)
        raise TimeoutError('fake transport: accepted but acknowledgement lost')

    def task(self):
        with self.life.repo.tx():
            result = self.role.intent(self.scope, dict(goal='review fixture research', sources=[self.event.id]), self.binding)
        self.binding = self.role.sessions.require(self.binding['session_id'])
        return result['intent']

    def close(self):
        self.life.close()
        self.temp.cleanup()
