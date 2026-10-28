"""Failed QQ image reads can recover verified pixels into the same inbox."""
import base64
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

from life.agent_session import SessionFailure
from life.media import MediaService
from tests.role_fixture import Fixture


class MediaInputRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.f.role.media=self.f.media
        self.f.bridge.home=self.f.root/'dsh-home'
        buffer=io.BytesIO();Image.new('RGB',(32,24),'blue').save(buffer,format='PNG');self.png=buffer.getvalue()

    async def test_qq_local_cache_precedes_a_broken_cdn_and_preserves_path_checks(self):
        cache=self.f.root/'qq-cache';cache.mkdir();path=cache/'input.png';path.write_bytes(self.png)
        async def platform(action,params):
            self.assertEqual(action,'get_image')
            return dict(retcode=0,data=dict(file=str(path),url='https://unreachable.invalid/picture'))
        media=MediaService(self.f.life,self.f.root/'images',platform,local_roots=[cache])
        event,_=self.f.life.ingest(self.f.scope,'message','user_report','',payload=dict(parts=[dict(
            kind='image',part_id='0',ordinal=0,source_ref=dict(file='platform-ticket.png',url='https://unreachable.invalid/picture'))]))
        media.capture(event);aid=event.payload['parts'][0]['asset_id']
        with patch('urllib.request.urlopen',side_effect=OSError('network unavailable')) as network:
            asset=await media.resolve(self.f.scope,aid)
        self.assertEqual(media.read_bytes(self.f.scope,aid),self.png)
        self.assertEqual(asset['mime'],'image/png');network.assert_not_called()
        with self.assertRaises(SessionFailure):media._read(self.f.root/'outside-cache.png')

    async def test_image_reread_admits_pixels_instead_of_only_returning_metadata(self):
        asset=self.f.media.register(self.f.scope,self.f.event.id,'image','image',self.png,'image/png')
        payloads=[]
        self.f.bridge.session_input=lambda payload:(payloads.append(payload) or dict(state='accepted',message_id='image-view'))
        result=await self.f.role.read_capability(self.f.scope,dict(kind='media',asset_id=asset['id']),
            self.f.binding,call_id='image-read')
        self.assertEqual(result['visual_input']['state'],'accepted')
        image=next(b for b in payloads[0]['content_blocks'] if b['type']=='image')
        self.assertEqual(base64.b64decode(image['data']),self.png)
        self.assertEqual(self.f.sent,[])
        other=self.f.life.scope('qq:private:2','2')
        with self.assertRaises(SessionFailure):
            await self.f.role.read_capability(other,dict(kind='media',asset_id=asset['id']),self.f.binding,call_id='foreign')

    async def test_local_screenshot_import_also_admits_pixels_and_missing_id_is_actionable(self):
        path=self.f.root/'screenshot.png';path.write_bytes(self.png)
        self.f.bridge.session_input=lambda payload:dict(state='accepted',message_id='import-view')
        result=await self.f.role.read_capability(self.f.scope,dict(kind='media',action='import',path=str(path)),
            self.f.binding,call_id='import-read')
        self.assertIn('visual_input',result)
        with self.assertRaisesRegex(SessionFailure,'asset_id'):
            await self.f.role.read_capability(self.f.scope,dict(kind='media'),self.f.binding)


if __name__=='__main__':unittest.main()
