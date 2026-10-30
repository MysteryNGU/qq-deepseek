from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from life.character_defaults import materialize,relocate_supporting_files
from life.settings import encode_toml


class CharacterDefaultsTests(unittest.TestCase):
    def test_reusable_features_are_materialized_and_private_state_is_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';stage=root/'stage';final=root/'final';source.mkdir()
            asset=source/'voice.wav';asset.write_bytes(b'fixed synthetic reference')
            item=dict(path=str(asset),sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
            manifest=dict(default_voice_id='voice',default_voice_revision=1,catalog_scope='old|private',
                production_catalog_imported=True,presets=[dict(id='voice',revision=1,reference=item,sample=item)])
            (source/'voices.json').write_text(json.dumps(manifest),encoding='utf-8')
            (source/'consult.json').write_text(json.dumps(dict(project=str(source),data_dir=str(source/'old-requests'))))
            (source/'life-dynamics.json').write_text(json.dumps(dict(drives=dict(relatedness=dict(base_growth_per_hour=.07)))))
            raw=dict(owner=1,known_people={'private':33},friends=[33],allow_groups=[44],continuity_enabled=True,
                same_person_group_recall=True,profile_review_enabled=True,
                local_media=dict(enabled=True,providers=[dict(provider_id='speech')],preset_manifest='voices.json'),
                local_embedding=dict(enabled=True,manifest_digest='a'*64,model_path='models',port=8231),
                embedding=dict(enabled=True,endpoint='http://127.0.0.1:8231/v1/embeddings',model_id='model',
                    model_revision='r1',provider_revision='p1',dimension=512,allow_external_text=True,
                    price_version='local',input_nano_per_token=0),consult_config='consult.json')
            (source/'config.toml').write_text(encode_toml(raw),encoding='utf-8')
            cfg=materialize(source,stage,'firefly',embedding_port=8233)
            self.assertTrue(cfg['continuity_enabled'] and cfg['profile_review_enabled'])
            self.assertEqual(cfg['known_people'],{'private':33});self.assertEqual(cfg['friends'],[33])
            self.assertEqual(cfg['allow_groups'],[44]);self.assertEqual(cfg['life_agent'],'firefly')
            self.assertEqual(cfg['local_embedding']['port'],8233)
            self.assertEqual(cfg['embedding']['endpoint'],'http://127.0.0.1:8233/v1/embeddings')
            self.assertEqual(cfg['local_embedding']['model_path'],str(source/'models'))
            relocate_supporting_files(stage,final,cfg)
            voices=json.loads((stage/cfg['local_media']['preset_manifest']).read_text('utf-8'))
            self.assertNotIn('catalog_scope',voices);self.assertNotIn('production_catalog_imported',voices)
            self.assertTrue(Path(voices['presets'][0]['reference']['path']).is_relative_to(final))
            consult=json.loads((stage/cfg['consult_config']).read_text('utf-8'))
            self.assertEqual(consult['data_dir'],str(final/'data/codex-consult'))
            self.assertFalse((stage/'data/life.db').exists())
            self.assertFalse((stage/'data/codex-consult/state.json').exists())


if __name__=='__main__':unittest.main()
