"""Explicit real-CLI verification, isolated from live bot conversation state."""
import json, pathlib, subprocess, sys, time, uuid
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.codex_consult import Bridge,atomic_json

def main():
    deployed=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    folder=ROOT/"data"/"codex-consult-verification"/uuid.uuid4().hex
    project=folder/"project";project.mkdir(parents=True)
    source=pathlib.Path(deployed["project"])
    for name in ("AGENTS.md","INIT.md"):
        body=(source/name).read_text(encoding="utf-8").replace(str(source),str(project))
        (project/name).write_text(body,encoding="utf-8")
    config=folder/"config.json"
    atomic_json(config,dict(deployed,enabled=True,project=str(project),data_dir=str(folder/"bridge"),timeout_seconds=240))
    bridge=Bridge(config);print(json.dumps(bridge.doctor(),ensure_ascii=False),flush=True)
    topic=None;thread=None
    token="verify-"+uuid.uuid4().hex[:12]
    messages=["这是独立接入验收，不是 QQ 指令。请读 INIT.md，只回答 role_agent.py 的绝对路径和验收口令："+token+"。不要扫描目录或读人格。",
              "继续同一话题，只回答上一轮的验收口令。不要重新读其他聊天。",
              "INIT.md 已更新，请重新读取后，只回答文件末尾的 consultation_verification 标记。"]
    results=[]
    for index,message in enumerate(messages):
        if index==2:
            with (project/"INIT.md").open("a",encoding="utf-8") as stream:stream.write("\nconsultation_verification: init-reloaded-"+token+"\n")
        binding=dict(operation_id="verify-"+str(index),dsh_session_id="verification",generation=1,
            scope_json="{}",intent_ref=None,intent_revision=None,forget_epoch=0,origin="bot_self",source_event_ids=[])
        proposal=dict(message=message)
        if topic:proposal["topic_id"]=topic
        admitted=bridge.prepare("verification",proposal,binding);topic=admitted["topic_id"]
        bridge.bind_job(admitted["request_id"],"verification","verify-job-"+str(index))
        process=subprocess.Popen(admitted["argv"],cwd=admitted["cwd"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        process.stdin.write((json.dumps(dict(action="start",request_id=admitted["request_id"],job_id="verify-job-"+str(index)))+"\n").encode());process.stdin.flush()
        try:
            process.wait(timeout=280)
            output=process.stdout.read().decode("utf-8");diagnostic=process.stderr.read().decode("utf-8")
            if process.returncode:raise RuntimeError((output,diagnostic))
        finally:
            process.stdin.close();process.stdout.close();process.stderr.close()
        result=bridge.read(admitted["request_id"])
        print(json.dumps(dict(round=index+1,request_id=admitted["request_id"],status=result["status"],thread_id=result["thread_id"],answer=result["answer"],reason=result["reason"]),ensure_ascii=False),flush=True)
        if result["status"]!="completed":raise RuntimeError("consultation did not complete; inspect "+str(folder))
        if thread and thread!=result["thread_id"]:raise RuntimeError("thread changed")
        thread=result["thread_id"]
        if index==0 and "D:\\projects\\qq-deepseek\\life\\role_agent.py" not in result["answer"]:raise RuntimeError("navigation answer missing")
        if index==1 and token not in result["answer"]:raise RuntimeError("continuation lost token")
        if index==2 and "init-reloaded-"+token not in result["answer"]:raise RuntimeError("INIT reload not observed")
        bridge.adopted(admitted["request_id"],result["version"]);results.append(result)
    bridge.close_topic(topic,"verification")
    atomic_json(folder/"verification.json",dict(passed=True,rounds=len(results),thread_id=thread))
    print("VERIFIED "+str(folder),flush=True)
if __name__=="__main__":main()
