import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path

from life.agent_session import SessionFailure
from life.expression import Expressions
from life.media import MediaService,accept_onebot
from life.runtime import LifeRuntime
from life.voice_catalog import VoiceCatalog


class MediaExpressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.life = LifeRuntime(Path(self.tmp.name)/'life.db')
        self.scope = self.life.scope('qq:private:1','1')
        self.media = MediaService(self.life,Path(self.tmp.name)/'assets')
        self.syntheses,self.sent = [],[]
        self.delays,self.elapsed = [],0
        async def sleep(seconds):
            self.assertFalse(self.life.repo.db.in_transaction)
            self.delays.append(seconds)
            self.elapsed += seconds
            await asyncio.sleep(0)
        self.sleep = sleep
        async def engine(text,voice,parameters,path):
            self.syntheses.append((text,voice))
            path.write_bytes(b'ID3'+b'FAKE_AUDIO_FOR_TEST_ONLY'*10)
        self.voices = VoiceCatalog(self.life,self.media,engine)
        self.voices.register(dict(id='test',engine='edge-tts',engine_voice='test-native-voice'))
        self.event,_ = self.life.ingest_message(self.scope,'test')

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    async def test_order_and_all_images_preserved_with_actor_identity(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB',(2,2)).save(image,format='PNG')
        encoded = 'base64://'+base64.b64encode(image.getvalue()).decode()
        segments = [dict(type='text',data={'text':'first'})]
        segments += [dict(type='image',data={'file':encoded}) for _ in range(6)]
        segments += [dict(type='reply',data={'id':'prior'}),dict(type='record',data={'file':'platform-audio'})]
        raw = dict(user_id=55,group_id=3,message_type='group',message_id=42,self_id=99,message=segments)
        event,created = accept_onebot(self.life,raw)
        self.assertTrue(created)
        blocks = await self.media.input_blocks(event)
        self.assertEqual(event.actor_id,'55')
        self.assertEqual(event.scope.participant_id,'')
        self.assertEqual(len([b for b in blocks if b['type']=='image']),6)
        self.assertEqual([p['ordinal'] for p in event.payload['parts']],list(range(9)))
        self.assertFalse(accept_onebot(self.life,raw)[1])

    async def test_pure_voice_unknown_delivery_reuses_frozen_asset_after_restart(self):
        async def transport(action,params):
            self.sent.append(params)
            raise TimeoutError('platform accepted maybe; ACK lost')
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        plan = dict(parts=[dict(kind='voice',voice_id='test',text='hello')])
        with self.assertRaises(TimeoutError):
            await expression(self.scope,'utterance',plan,[self.event.id])
        self.life.delivery.recover()
        restarted = Expressions(self.life,self.media,VoiceCatalog(self.life,self.media),transport,sleep=self.sleep)
        with self.assertRaises(SessionFailure) as error:
            await restarted(self.scope,'utterance',plan,[self.event.id])
        self.assertEqual(error.exception.code,'outcome_unknown')
        self.assertEqual(len(self.syntheses),1)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['message'][0]['type'],'record')

    async def test_new_input_during_synthesis_stops_old_send(self):
        async def late(text,voice,params,path):
            self.life.delivery.new_input(self.scope,'new',[self.event.id])
            path.write_bytes(b'ID3'+b'x'*100)
        self.voices.engine = late
        async def transport(action,params):
            self.sent.append(params)
            return dict(retcode=0,status='ok',data=dict(message_id=1))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        with self.assertRaises(SessionFailure):
            await expression(self.scope,'old',dict(parts=[dict(kind='voice',voice_id='test',text='old')]),[self.event.id])
        self.assertFalse(self.sent)

    async def test_typing_delays_precede_each_dispatch_and_retry_does_not_repeat(self):
        async def transport(action,params):
            self.sent.append((self.elapsed,params['message'][0]['data']['text']))
            return dict(retcode=0,data=dict(message_id=len(self.sent)))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        proposal = dict(text='a'*9+'\n'+'b'*24)
        result = await expression(self.scope,'paced',proposal,[self.event.id])
        self.assertTrue(result['sent_confirmed'])
        self.assertEqual(self.sent,[(2,'a'*9),(4,'b'*24)])
        await expression(self.scope,'paced',proposal,[self.event.id])
        self.assertEqual(self.elapsed,4)
        self.assertEqual(len(self.sent),2)

    async def test_long_multiline_reply_is_one_paste_even_over_old_transport_limit(self):
        async def transport(action,params):
            self.sent.append((self.elapsed,params['message'][0]['data']['text']))
            return dict(retcode=0,data=dict(message_id=len(self.sent)))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        lines = ['paragraph '+str(i)+'x'*50 for i in range(80)]
        parts = [dict(kind='text',text='\n'.join(lines[:40])),
                 dict(kind='text',text='\n'.join(lines[40:]))]
        await expression(self.scope,'paste',dict(parts=parts),[self.event.id])
        self.assertEqual(self.sent,[(4,'\n'.join(lines))])

    async def test_300_character_boundary_changes_both_layout_and_wait(self):
        async def transport(action,params):
            self.sent.append((self.elapsed,params['message'][0]['data']['text']))
            return dict(retcode=0,data=dict(message_id=len(self.sent)))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        short = 'a'*149+'\n'+'b'*149
        await expression(self.scope,'under-boundary',dict(text=short),[self.event.id])
        self.assertEqual(self.sent,[(5,'a'*149),(10,'b'*149)])
        long = 'a'*149+'\n'+'b'*150
        await expression(self.scope,'at-boundary',dict(text=long),[self.event.id])
        self.assertEqual(self.sent[-1],(14,long))

    async def test_input_during_typing_interrupts_within_one_second_and_holds_no_lease(self):
        async def sleep(seconds):
            await self.sleep(seconds)
            row = self.life.repo._one('SELECT status,token FROM deliveries')
            self.assertEqual(row['status'],'pending')
            self.assertFalse(row['token'])
            self.life.delivery.new_input(self.scope,'interrupt',[self.event.id])
        async def transport(action,params):
            self.sent.append(params)
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=sleep)
        with self.assertRaises(SessionFailure) as error:
            await expression(self.scope,'typing',dict(text='x'*90),[self.event.id])
        self.assertEqual(error.exception.code,'input_pending')
        self.assertEqual(self.elapsed,1)
        self.assertFalse(self.sent)
        self.assertEqual(self.life.repo._one('SELECT status FROM deliveries')['status'],'superseded')

    async def test_cancellation_while_typing_does_not_create_unknown_delivery(self):
        started = asyncio.Event()
        async def sleep(seconds):
            started.set()
            await asyncio.Event().wait()
        async def transport(action,params):
            self.sent.append(params)
            return dict(retcode=0,data=dict(message_id=1))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=sleep)
        proposal = dict(text='hello')
        task = asyncio.create_task(expression(self.scope,'cancel-typing',proposal,[self.event.id]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.sent)
        self.assertEqual(self.life.repo._one('SELECT status FROM deliveries')['status'],'pending')
        expression.sleep = self.sleep
        await expression(self.scope,'cancel-typing',proposal,[self.event.id])
        self.assertEqual(len(self.sent),1)

    async def test_concurrent_expressions_do_not_interleave_bubbles_in_same_conversation(self):
        async def transport(action,params):
            self.sent.append(params['message'][0]['data']['text'])
            await asyncio.sleep(0)
            return dict(retcode=0,data=dict(message_id=len(self.sent)))
        expression = Expressions(self.life,self.media,self.voices,transport,sleep=self.sleep)
        await asyncio.gather(expression(self.scope,'a',dict(text='one\ntwo'),[self.event.id]),
                             expression(self.scope,'b',dict(text='three\nfour'),[self.event.id]))
        self.assertEqual(self.sent,['one','two','three','four'])

    async def test_foreign_media_and_unverified_global_default_rejected(self):
        asset = self.media.register(self.scope,self.event.id,'image','image',b'fake','image/png')
        other = self.life.scope('qq:private:2','2')
        with self.assertRaises(SessionFailure):
            self.media.require(other,asset['id'])
        with self.assertRaises(SessionFailure):
            self.voices.set_default(self.scope,'test',True,True)
        await self.voices.synthesize(self.scope,self.event.id,'sample','hello','test')
        with self.assertRaises(SessionFailure):
            self.voices.set_default(other,'test',True,False)
        self.voices.set_default(self.scope,'test',True,True)
        self.assertEqual(self.voices.default(other),'test')

    async def test_failed_download_is_visible_and_cq_encoding_preserves_order(self):
        from life.media import onebot_segments
        parts = onebot_segments('before[CQ:image,file=a&#44;b]after[CQ:record,file=missing]')
        self.assertEqual([p['type'] for p in parts],['text','image','text','record'])
        self.assertEqual(parts[1]['data']['file'],'a,b')
        event,_ = accept_onebot(self.life,dict(user_id=1,message_type='private',message_id='failed-media',
                                             self_id=99,message=parts))
        self.media.capture(event)
        aid = event.payload['parts'][1]['asset_id']
        with self.assertRaises(SessionFailure):
            await self.media.resolve(self.scope,aid)
        row = self.media.require(self.scope,aid,False)
        self.assertEqual(row['status'],'failed')
        self.assertIn('error',row['metadata'])


if __name__=='__main__':
    unittest.main()
