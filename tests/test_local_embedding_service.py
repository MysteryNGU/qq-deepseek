import hashlib
from http.server import HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

from life.local_embedding import LocalEmbeddingConfig,LocalEmbeddingProcess,handler_for,verified_manifest


class LocalEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def manifest(self):
        (self.folder/'model.safetensors').write_bytes(b'fake test weights')
        value=dict(model_id='test-model',revision='pinned',dimension=512,pooling='cls-l2',
            files={'model.safetensors':hashlib.sha256(b'fake test weights').hexdigest()})
        data=json.dumps(value).encode();(self.folder/'model-manifest.json').write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_manifest_binds_the_actual_assets(self):
        digest=self.manifest()
        self.assertEqual(verified_manifest(self.folder,digest)['revision'],'pinned')
        (self.folder/'model.safetensors').write_bytes(b'changed')
        with self.assertRaises(ValueError):verified_manifest(self.folder,digest)

    def test_manifest_cannot_point_outside_model_folder(self):
        self.manifest()
        data=json.dumps(dict(dimension=512,pooling='cls-l2',files={'../outside':'bad'})).encode()
        (self.folder/'model-manifest.json').write_bytes(data)
        with self.assertRaises(ValueError):verified_manifest(self.folder,hashlib.sha256(data).hexdigest())

    def test_local_server_rejects_public_binding_and_unbounded_threads(self):
        for raw in [dict(host='0.0.0.0'),dict(host='localhost'),dict(threads=999),dict(port=True),dict(enabled=True)]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):LocalEmbeddingConfig.parse(raw)

    def test_enabled_service_requires_same_loopback_endpoint_as_adapter(self):
        raw=dict(enabled=True,manifest_digest='a'*64)
        with self.assertRaises(ValueError):LocalEmbeddingProcess(self.folder,raw,dict(enabled=True,endpoint='https://example.invalid'))

    def server(self):
        class Model:
            def embed(self,texts):
                if any('secret-error' in t for t in texts):raise RuntimeError('secret-error')
                return [[1.0]+[0.0]*511 for t in texts],len(texts)*3
        server=HTTPServer(('127.0.0.1',0),handler_for(Model(),dict(model_id='test-model',revision='pin',dimension=512),'digest'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():server.shutdown();server.server_close();thread.join(2)
        self.addCleanup(close)
        return 'http://127.0.0.1:'+str(server.server_port)

    def post(self,base,value):
        request=Request(base+'/v1/embeddings',data=json.dumps(value).encode(),headers={'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(request,timeout=3) as reply:return json.load(reply)

    def test_loopback_response_reports_real_counts_shape_and_model(self):
        base=self.server()
        result=self.post(base,dict(model='test-model',input=['中文检索','第二条']))
        self.assertEqual(result['usage']['prompt_tokens'],6)
        self.assertEqual(len(result['data']),2)
        self.assertEqual(len(result['data'][0]['embedding']),512)
        with build_opener(ProxyHandler({})).open(base+'/health',timeout=3) as reply:health=json.load(reply)
        self.assertEqual(health['device'],'cpu');self.assertEqual(health['manifest_digest'],'digest')

    def test_invalid_model_or_input_never_runs_inference(self):
        base=self.server()
        for value in [dict(model='other',input=['text']),dict(model='test-model',input=[]),
                      dict(model='test-model',input='text'),dict(model='test-model',input=['x']*33)]:
            with self.subTest(value=value),self.assertRaises(HTTPError) as got:self.post(base,value)
            self.assertEqual(got.exception.code,400)

    def test_error_response_never_reflects_experience_text(self):
        base=self.server()
        with self.assertRaises(HTTPError) as got:self.post(base,dict(model='test-model',input=['secret-error']))
        self.assertNotIn('secret-error',got.exception.read().decode())

    def test_request_size_is_bounded(self):
        base=self.server()
        with self.assertRaises(HTTPError) as got:self.post(base,dict(model='test-model',input=['x'*262144]))
        self.assertEqual(got.exception.code,413)


if __name__=='__main__':unittest.main()
