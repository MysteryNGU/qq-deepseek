import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image

from life.agent_session import SessionFailure
from life.media import MediaService
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.life=LifeRuntime(Path(self.tmp.name)/'life.db');self.addCleanup(self.life.close)
        self.media=MediaService(self.life,Path(self.tmp.name)/'media')
        self.role=RoleAgent(self.life,SimpleNamespace(_generation=1),SimpleNamespace(owner=1),Path(self.tmp.name),AsyncMock(),media=self.media)
        self.scope=self.life.scope('qq:private:1','1');self.event,_=self.life.ingest_message(self.scope,'This is a garden sticker.')
        image=io.BytesIO();Image.new('RGB',(8,8),'green').save(image,format='PNG')
        self.asset=self.media.register(self.scope,self.event.id,'0','image',image.getvalue(),'image/png')

    def collect(self):
        with self.life.repo.tx():return self.role.media_library.change(self.scope,dict(asset_id=self.asset['id'],label='Garden',tags=['garden']))

    def test_collect_deduplicates_searches_and_retires_without_deleting_original(self):
        first=self.collect();self.assertEqual(first['ref'],self.collect()['ref'])
        self.assertEqual(self.role.media_library.list(self.scope,'garden')['total'],1)
        self.assertIsNone(self.role.media_library.list(self.scope)['items'][0]['last_used'])
        with self.life.repo.tx():self.role.media_library.used(self.scope,[dict(asset_id=self.asset['id'])])
        self.assertIsNotNone(self.role.media_library.list(self.scope)['items'][0]['last_used'])
        with self.life.repo.tx():self.role.media_library.change(self.scope,dict(action='retire',ref=first['ref']))
        self.assertEqual(self.role.media_library.list(self.scope)['total'],0)
        self.assertTrue(Path(self.asset['storage_ref']).exists())

    def test_other_scene_cannot_read_or_collect_private_sticker(self):
        self.collect();other=self.life.scope('qq:group:10')
        self.assertEqual(self.role.media_library.list(other)['total'],0)
        with self.assertRaises(SessionFailure):self.role.media_library.change(other,dict(asset_id=self.asset['id'],label='stolen'))

    def test_expiry_and_forgetting_retire_selection(self):
        record=self.collect()
        with self.life.repo.tx():self.life.repo._exec('UPDATE media_library SET expires=0 WHERE id=?',(record['ref'],))
        self.assertEqual(self.role.media_library.list(self.scope)['total'],0)
        self.collect()
        self.life.tombstones.apply(self.life.tombstones.append(self.scope,[],[self.event.id]),self.scope,[],[self.event.id])
        self.assertEqual(self.role.media_library.list(self.scope)['total'],0)


if __name__=='__main__':unittest.main()
