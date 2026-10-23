"""Real clock, production expression/delivery code, local fake transport only."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from life.agent_session import SessionFailure
from life.expression import Expressions
from life.runtime import LifeRuntime


async def main():
    with tempfile.TemporaryDirectory() as directory:
        life = LifeRuntime(Path(directory)/'life.db')
        try:
            scope = life.scope('qq:private:1','1')
            event,_ = life.ingest_message(scope,'synthetic expression acceptance')
            sent = []
            start = time.monotonic()
            async def transport(action,params):
                sent.append((time.monotonic()-start,params['message'][0]['data']['text']))
                return dict(retcode=0,data=dict(message_id=len(sent)))
            expression = Expressions(life,None,None,transport)
            await expression(scope,'typing',dict(text='a'*9+'\n'+'b'*60),[event.id])
            assert len(sent)==2 and sent[0][0]>=2 and sent[1][0]-sent[0][0]>=5,sent
            text = '\n'.join(['long text '*20]*3)
            await expression(scope,'paste',dict(text=text),[event.id])
            assert len(sent)==3 and sent[2][1]==text and sent[2][0]-sent[1][0]>=4,sent
            pending = asyncio.create_task(expression(scope,'interrupted',dict(text='c'*90),[event.id]))
            await asyncio.sleep(.2)
            interrupt = time.monotonic()
            life.delivery.new_input(scope,'new-user-input',[event.id])
            try:
                await pending
            except SessionFailure as error:
                assert error.code=='input_pending',error.code
            else:
                raise AssertionError('obsolete expression was not stopped')
            latency = time.monotonic()-interrupt
            assert len(sent)==3 and latency<2,(sent,latency)
            print(json.dumps(dict(short_first_seconds=round(sent[0][0],2),
                short_next_gap_seconds=round(sent[1][0]-sent[0][0],2),
                paste_gap_seconds=round(sent[2][0]-sent[1][0],2),paste_messages=1,
                interrupt_stop_seconds=round(latency,2),real_qq_messages=0)))
        finally:
            life.close()


if __name__=='__main__':
    asyncio.run(main())
