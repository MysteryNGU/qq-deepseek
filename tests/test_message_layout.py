import unittest

from life.message_layout import bubble_delay,content_blocks,expression_parts,transport_parts


class MessageLayoutTests(unittest.TestCase):
    def test_length_changes_typing_time_and_speed_is_adjustable(self):
        self.assertEqual([bubble_delay('字'*n) for n in (1,10,30,60)],[2,2,3,5])
        self.assertEqual(bubble_delay('字'*60,cps=6),3)
        self.assertEqual(bubble_delay('字'*4000,'artifact'),4)
        self.assertEqual(bubble_delay('字'*299),5)
        self.assertEqual(bubble_delay('字'*600,'artifact',cps=100),4)

    def test_paste_boundary_is_300_and_all_production_defaults_agree(self):
        from life.expression import Expressions
        from life.role_config import Config,load_config
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import inspect
        self.assertEqual(Config().paste_threshold,300)
        self.assertEqual(inspect.signature(Expressions).parameters['paste_threshold'].default,300)
        self.assertEqual(inspect.signature(expression_parts).parameters['paste_threshold'].default,300)
        with TemporaryDirectory() as directory:
            path = Path(directory)/'config.toml'
            path.write_text('owner = 1\n',encoding='utf-8')
            self.assertEqual(load_config(path).paste_threshold,300)
        for size,kind in ((180,'chat'),(299,'chat'),(300,'artifact'),(301,'artifact')):
            text = 'a'*(size//2)+'\n'+'b'*(size-size//2-1)
            bubbles = content_blocks(text)
            self.assertEqual(bubbles[0].kind,kind)
            self.assertEqual(len(bubbles),1 if kind=='artifact' else 2)
            if kind=='artifact':
                self.assertEqual(bubbles[0].text,text)

    def test_short_chat_splits_on_newlines_without_a_fixed_message_count(self):
        text = '\n'.join('line '+str(i) for i in range(12))
        self.assertEqual([b.text for b in content_blocks(text)],text.splitlines())

    def test_single_mode_keeps_only_its_own_short_bubble_whole(self):
        parts = [dict(kind='text',text='first\nsecond',mode='single'),
                 dict(kind='text',text='third',mode='single')]
        self.assertEqual(expression_parts(parts),parts)
        bubbles = content_blocks(parts[0]['text'],'single')
        self.assertEqual([(b.text,b.kind) for b in bubbles],[('first\nsecond','chat')])

    def test_single_parts_still_obey_the_whole_reply_paste_threshold(self):
        parts = [dict(kind='text',text='a'*149,mode='single'),
                 dict(kind='text',text='b'*150,mode='single')]
        self.assertEqual(expression_parts(parts),
                         [dict(kind='text',text='a'*149+'\n'+'b'*150,mode='artifact')])

    def test_long_and_structured_text_preserves_whole_body_including_newlines(self):
        for text in ('\n'.join(['long paragraph'*10]*10),
                     'example:\n```python\nx = 1\n```\nafterwards',
                     'shopping:\n- apple\n- pear\nnext week'):
            with self.subTest(text=text[:10]):
                bubbles = content_blocks(text)
                self.assertEqual(len(bubbles),1)
                self.assertEqual(bubbles[0].text,text)
                self.assertEqual(bubbles[0].kind,'artifact')
                self.assertEqual(transport_parts(bubbles[0]),bubbles)

    def test_combining_text_preserves_media_order(self):
        image = dict(kind='image',asset_id='asset')
        parts = [dict(kind='text',text='a'*150),dict(kind='text',text='b'*149),image,
                 dict(kind='text',text='short'),dict(kind='text',text='reply')]
        result = expression_parts(parts)
        self.assertEqual(result[0]['text'],'a'*150+'\n'+'b'*149)
        self.assertEqual(result[1],image)
        self.assertEqual(result[2:],parts[3:])
