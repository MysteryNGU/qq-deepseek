// JSONL adapter for the original Coopanion engine. No Cortico host or agent.
import {fork} from 'node:child_process';
import {createInterface} from 'node:readline';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const runtime=resolve(process.argv[2]);
const {parseKeys}=await import(pathToFileURL(resolve(runtime,'keys.mjs')).href);
const child=fork(resolve(runtime,'engine-child.mjs'),[],{
  serialization:'advanced',windowsHide:true,stdio:['ignore','ignore','inherit','ipc'],execArgv:[],
});
const write=value=>process.stdout.write(JSON.stringify(value)+'\n');
child.on('message',message=>{
  if(message.ok && message.value?.jpeg){
    message.value.imageBase64=Buffer.from(message.value.jpeg).toString('base64');
    delete message.value.jpeg;
  }
  write(message);
});
child.on('error',error=>{process.stderr.write(error.message+'\n');process.exit(1)});
child.on('exit',code=>process.exit(code??1));
const lines=createInterface({input:process.stdin});
lines.on('line',line=>{
  let message;
  try{
    message=JSON.parse(line);
    if(message.req?.op==='key' || message.req?.op==='hold'){
      const parsed=parseKeys(message.req.keys,'en');
      if(parsed.error)throw new Error(parsed.error);
      if(message.req.op==='key')message.req.chords=parsed.chords;
      else{
        if(parsed.chords.length!==1)throw new Error('hold accepts one key chord');
        message.req.keys=parsed.chords[0];
      }
      if(message.req.op==='key')delete message.req.keys;
    }
    child.send(message);
  }catch(error){write({id:message?.id,ok:false,error:error.message})}
});
lines.on('close',()=>{if(child.connected)child.disconnect()});
