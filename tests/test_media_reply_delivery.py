import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from life.agent_session import SessionFailure
from tests.role_fixture import Fixture


class MediaReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.cfg.handoff_seconds = 0
        self.f.role.sessions.deliver = AsyncMock(return_value={})
        async def confirmed(action,params):
            self.f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=len(self.f.sent)))
        self.f.expressions.transport = confirmed

    def tearDown(self):
        self.f.close()

    def result(self,key):
        return self.f.life.ingest(self.f.scope,'media_result','tool_observation','job done',
            external_id=key,source_event_ids=[self.f.event.id])[0]

    async def say(self,text,sources,**extra):
        return await self.f.role.say(self.f.scope,dict(text=text,sources=sources,**extra),self.f.binding,text)

    def texts(self):
        return [p['message'][0]['data']['text'] for p in self.f.sent]

    async def test_result_during_bubbles_waits_without_superseding_reply(self):
        f = self.f
        started,release = asyncio.Event(),asyncio.Event()
        async def typing(delay):
            started.set()
            await release.wait()
        f.expressions.sleep = typing
        sending = asyncio.create_task(self.say('first\nsecond',[f.event.id]))
        await started.wait()
        event = self.result('during-send')
        notifying = asyncio.create_task(f.role.ingest(event))
        await asyncio.sleep(0)
        self.assertFalse(notifying.done())
        self.assertFalse(f.role.sessions.pending(f.binding['session_id']))
        release.set()
        self.assertTrue((await sending)['sent_confirmed'])
        await notifying
        self.assertEqual(self.texts(),['first','second'])
        self.assertEqual([p['input_id'] for p in f.role.sessions.pending(f.binding['session_id'])],[event.id])

    async def test_actual_user_message_still_interrupts_reply(self):
        f = self.f
        async def typing(delay):
            if not f.role.sessions.pending(f.binding['session_id']):
                newer,_ = f.life.ingest_message(f.scope,'stop')
                await f.role.ingest(newer)
        f.expressions.sleep = typing
        with self.assertRaises(SessionFailure) as error:
            await self.say('old answer',[f.event.id])
        self.assertEqual(error.exception.code,'input_pending')
        self.assertFalse(f.sent)
        await asyncio.gather(*f.role._workers)

    async def test_result_and_visual_blocks_are_one_durable_input_even_on_retry(self):
        f = self.f
        f.role.media = type('Media',(),dict(capture=lambda *a:None,
            input_blocks=AsyncMock(return_value=[dict(type='text',text='metadata'),
                dict(type='image',mimeType='image/png',data='fake')])) )()
        event = self.result('single-input')
        event.payload['parts'] = [dict(kind='image',asset_id='asset-test')]
        await f.role.ingest(event)
        await f.role.ingest(event)
        rows = f.life.repo._all('SELECT payload FROM agent_inputs')
        self.assertEqual(len(rows),1)
        blocks = json.loads(rows[0]['payload'])['content_blocks']
        self.assertEqual(blocks[0]['text'],'job done')
        self.assertEqual(blocks[-1]['type'],'image')
        self.assertFalse(f.life.repo._one("SELECT 1 FROM events WHERE kind='perception'"))

    async def test_two_result_sources_reuse_confirmed_bubbles_but_send_new_content(self):
        f = self.f
        await self.say('answer\nwaiting',[f.event.id])
        first = await self.say('answer\nfirst picture ready',[self.result('one').id])
        second = await self.say('answer\nsecond picture ready',[self.result('two').id])
        self.assertTrue(first['sent_confirmed'] and second['sent_confirmed'])
        self.assertEqual(self.texts(),['answer','waiting','first picture ready','second picture ready'])
        self.assertEqual(first['delivery_ids'][0],second['delivery_ids'][0])

    async def test_unknown_bubble_cannot_be_retried_via_media_source(self):
        f = self.f
        f.expressions.transport = f.transport
        with self.assertRaises(TimeoutError):
            await self.say('answer',[f.event.id])
        with self.assertRaises(SessionFailure) as error:
            await self.say('answer',[self.result('unknown').id])
        self.assertEqual(error.exception.code,'outcome_unknown')
        self.assertEqual(len(f.sent),1)

    async def test_identical_media_in_two_results_is_delivered_once(self):
        f = self.f
        for kind,mime in [('image','image/png'),('audio','audio/wav')]:
            with self.subTest(kind=kind):
                asset = f.media.register(f.scope,f.event.id,kind,kind,b'media fixture',mime)
                async def share(event):
                    return await f.role.say(f.scope,dict(parts=[dict(kind=kind,asset_id=asset['id'])],
                        sources=[event.id]),f.binding,event.id)
                before = len(f.sent)
                first = await share(self.result(kind+'-one'))
                second = await share(self.result(kind+'-two'))
                self.assertEqual(first['delivery_ids'],second['delivery_ids'])
                self.assertEqual(len(f.sent)-before,1)
                self.assertEqual(f.sent[-1]['message'][0]['type'],'image' if kind=='image' else 'record')

    async def test_speech_result_and_transcription_are_one_input(self):
        f = self.f
        f.role.media = type('Media',(),dict(capture=lambda *a:None,
            input_blocks=AsyncMock(return_value=[dict(type='text',text='audio metadata'),
                dict(type='text',text='transcribed speech')])) )()
        event = self.result('speech-complete')
        event.payload.update(kind='speech',parts=[dict(kind='audio',asset_id='audio-test')])
        await f.role.ingest(event)
        await f.role.ingest(event)
        rows = f.life.repo._all('SELECT payload FROM agent_inputs')
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0]['payload'])['content_blocks'][-1]['text'],'transcribed speech')
        self.assertFalse(f.life.repo._one("SELECT 1 FROM events WHERE kind='perception'"))

    async def test_retry_of_previously_enqueued_result_keeps_original_payload(self):
        f = self.f
        event = self.result('old-input-format')
        f.role.sessions.enqueue(f.binding,event)
        original = f.life.repo._one('SELECT payload FROM agent_inputs')['payload']
        await f.role.ingest(event)
        self.assertEqual(f.life.repo._one('SELECT payload FROM agent_inputs')['payload'],original)

    async def test_new_user_input_and_explicit_resend_allow_same_text(self):
        f = self.f
        await self.say('answer',[f.event.id])
        await self.say('answer',[self.result('explicit').id],new_message=True)
        f.life.ingest_message(f.scope,'please say it again')
        await self.say('answer',[self.result('new-user').id])
        self.assertEqual(self.texts(),['answer']*3)


if __name__=='__main__':
    unittest.main()
