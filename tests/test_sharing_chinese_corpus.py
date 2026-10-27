import json
from pathlib import Path
import unittest

from life.life_access import RecallError
from tests.role_fixture import Fixture


def evaluate_sharing():
    fixture=Fixture()
    results=[]
    try:
        cases=json.loads((Path(__file__).parent/'fixtures'/'sharing-chinese.json').read_text('utf-8'))
        for i,case in enumerate(cases):
            event,_=fixture.life.ingest_message(fixture.scope,case['text'])
            resource=fixture.life.catalog.resolve(event.id)
            body=fixture.life.catalog.body(resource).encode('utf-8')
            selected='推荐电影小森林。'.encode('utf-8');start=body.index(selected)
            proposal=dict(source_ref=resource['resource_id'],classification='ordinary',category='recommendation',
                uncertain=False,spans=[[start,start+len(selected)]])
            try:
                fixture.role.memory_worker.sharing.prepare(fixture.life.life_access.bind(fixture.scope,mode='internal'),
                    proposal,{resource['resource_id']:resource['source_version']})
                allowed=True
            except RecallError:
                allowed=False
            results.append(dict(case=i+1,expected=case['allow'],allowed=allowed,passed=allowed==case['allow']))
    finally:
        fixture.close()
    return results


class ChineseSharingTests(unittest.TestCase):
    def test_host_policy_on_bounded_chinese_corpus(self):
        for result in evaluate_sharing():
            with self.subTest(case=result['case']):self.assertTrue(result['passed'],result)


if __name__=='__main__':unittest.main()
