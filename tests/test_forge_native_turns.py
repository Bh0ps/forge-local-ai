"""Native pipe terminals and durable answer recovery, without model processes."""
import io
import threading
from types import SimpleNamespace

import pytest

from forge_native import NativeFailure, NativeRuntime
from test_forge_core import Engine, call, finished, service
from test_forge_native import gguf


def pipe(tmp_path, monkeypatch, chunks, *, thinking=False, finish="stop"):
    runtime=NativeRuntime(tmp_path/"pipe")
    # Distinct ordinary token pieces deliberately spell a control literal too.
    tokens=["<|im_end|>","<|endoftext|>","<think>","</think>","<tool_call>","</tool_call>",*chunks]
    tokenizer=SimpleNamespace(tokens=tokens,token_types=[3,3,4,4,4,4]+[1]*len(chunks),
        special_ids={"tokenizer.ggml.eos_token_id":0},special_tokens={"<|im_end|>":0,"<|endoftext|>":1},
        encode=lambda *a,**k:[6],token_bytes=lambda identity:tokens[identity].encode())
    worker={"tokenizer":tokenizer,"template":SimpleNamespace(render=lambda *a,**k:"prompt"),
        "process":SimpleNamespace(stdin=io.StringIO())}
    lines=iter([*("T "+str(identity) for identity in range(6,len(tokens))),"T 0",f"DONE {len(chunks)+1} 1 0 1 {finish}"])
    monkeypatch.setattr(runtime,"_line",lambda *a:next(lines))
    request={"model":"fixture","context":8192,"tokens":1024,"thinking":thinking,
        "messages":[{"role":"user","content":"Synthetic request"}],
        "tools":[{"type":"function","function":{"name":"list_files","parameters":{"type":"object","properties":{}}}}]}
    return runtime,worker,request


@pytest.mark.parametrize("thinking",[False,True])
def test_terminal_ids_never_reach_text_or_tool_parser(tmp_path,monkeypatch,thinking):
    chunks=(["Reasoned safely.","</think>"] if thinking else [])+[
        "Visible progress.","<tool_call><function=list_files></function></tool_call>"]
    runtime,worker,request=pipe(tmp_path,monkeypatch,chunks,thinking=thinking)
    packets=list(runtime._strata_generate(worker,request,threading.Event()))
    assert "".join(p.get("message",{}).get("content","") for p in packets)=="Visible progress."
    assert "".join(p.get("message",{}).get("thinking","") for p in packets)==("Reasoned safely." if thinking else "")
    calls=[c for p in packets for c in p.get("message",{}).get("tool_calls",[])]
    assert len(calls)==1 and calls[0]["function"]=={"name":"list_files","arguments":{}}
    assert packets[-1]["done_reason"]=="stop" and packets[-1]["eval_count"]==len(chunks)+1


def test_ordinary_tokens_spelling_terminal_literal_are_retained(tmp_path,monkeypatch):
    runtime,worker,request=pipe(tmp_path,monkeypatch,["Literal ","<|","im_end","|>"," remains."])
    packets=list(runtime._strata_generate(worker,request,threading.Event()))
    assert "".join(p.get("message",{}).get("content","") for p in packets)=="Literal <|im_end|> remains."


def test_metadata_eos_is_used_without_literal_aliases(tmp_path,monkeypatch):
    runtime,worker,request=pipe(tmp_path,monkeypatch,["Correct answer."])
    worker["tokenizer"].special_tokens={}
    packets=list(runtime._strata_generate(worker,request,threading.Event()))
    assert "".join(p.get("message",{}).get("content","") for p in packets)=="Correct answer."


def test_endoftext_control_alias_is_terminal_without_eos_metadata(tmp_path,monkeypatch):
    runtime,worker,request=pipe(tmp_path,monkeypatch,["Correct answer."])
    worker['tokenizer'].special_ids={}
    lines=iter(['T 6','T 1','DONE 2 1 0 1 stop'])
    monkeypatch.setattr(runtime,'_line',lambda *a:next(lines))
    packets=list(runtime._strata_generate(worker,request,threading.Event()))
    assert ''.join(p.get('message',{}).get('content','') for p in packets)=='Correct answer.'


def test_user_defined_literal_is_not_a_terminal_control(tmp_path,monkeypatch):
    runtime,worker,request=pipe(tmp_path,monkeypatch,["Literal "])
    worker['tokenizer'].token_types[1]=4
    lines=iter(['T 6','T 1','T 0','DONE 3 1 0 1 stop'])
    monkeypatch.setattr(runtime,'_line',lambda *a:next(lines))
    packets=list(runtime._strata_generate(worker,request,threading.Event()))
    assert ''.join(p.get('message',{}).get('content','') for p in packets)=='Literal <|endoftext|>'


@pytest.mark.parametrize("ending",["T 6","DONE 2 1 0 1 length"])
def test_terminal_must_drain_to_matching_successful_done(tmp_path,monkeypatch,ending):
    runtime,worker,request=pipe(tmp_path,monkeypatch,["Answer."])
    lines=iter(["T 6","T 0",ending])
    monkeypatch.setattr(runtime,"_line",lambda *a:next(lines))
    with pytest.raises(NativeFailure,match="after its terminal|inconsistent terminal") as error:
        list(runtime._strata_generate(worker,request,threading.Event()))
    assert error.value.category=="protocol"


def native_service(tmp_path,monkeypatch,rounds):
    engine=Engine(rounds);svc=service(tmp_path,engine)
    svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    runtime=svc.providers.native()
    record=runtime.catalog.add([gguf(tmp_path/"fixture.gguf")],identifier="fixture")
    record.update(engine="strata",capabilities=["completion","tools"],context=8192)
    runtime.catalog.data["models"]=[record]
    monkeypatch.setattr(runtime,"generate",engine.stream_agent)
    svc.store.update_settings({"provider_id":"forge-native","model":"fixture","thinking":True,
        "permission_profile":"full_access","memory_suggestions":False})
    return svc,engine


def test_empty_native_final_gets_one_answer_only_correction_without_action_replay(tmp_path,monkeypatch):
    svc,engine=native_service(tmp_path,monkeypatch,[
        {"tool_calls":[call("write_file",{"path":"once.txt","content":"Saved once.","expected_sha256":"missing"})]},
        {"thinking":"Completed reasoning without a visible answer."},{"content":"Saved once.txt successfully."}])
    try:
        root=tmp_path/"project";root.mkdir();project=svc.create_project({"name":"Fixture","path":str(root)})
        run=svc.jobs.start({"text":"Write once.txt and report what happened.","project_id":project["id"]})
        assert finished(svc,run)["status"]=="completed"
        assert (root/"once.txt").read_text()=="Saved once." and len(engine.requests)==3
        assert engine.requests[0]["thinking"] is True and engine.requests[1]["thinking"] is True
        assert engine.requests[2]["thinking"] is False and engine.requests[2]["tools"]==[]
        assert svc.store.get_settings()["thinking"] is True
        assert svc.store.run(run["id"])["native_empty_result_corrections"]==1
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=?",(run["id"],)).fetchone()[0]==1
        assert any(m["role"]=="tool" for m in engine.requests[2]["messages"])
    finally:svc.shutdown()


def test_empty_correction_pauses_and_stays_bounded_after_restart(tmp_path,monkeypatch):
    svc,engine=native_service(tmp_path,monkeypatch,[{},{}])
    run=svc.jobs.start({"text":"Give a visible answer."})
    assert finished(svc,run)["status"]=="paused"
    assert len(engine.requests)==2 and svc.store.run(run["id"])["failure"]["category"]=="empty_result"
    svc.shutdown()
    restored,second=native_service(tmp_path,monkeypatch,[{}])
    try:
        resumed=restored.jobs.resume(run["id"])
        assert finished(restored,resumed)["status"]=="paused"
        assert len(second.requests)==1 and second.requests[0]["tools"]==[]
        assert restored.store.run(run["id"])["native_empty_result_corrections"]==1
        assert not any(e["type"]=="complete" for e in restored.store.events(run["id"]))
    finally:restored.shutdown()


@pytest.mark.parametrize('arguments',[{'path':'bad.txt','content':'Must not run.'},'{"path":'])
def test_correction_tool_calls_are_never_executed(tmp_path,monkeypatch,arguments):
    svc,engine=native_service(tmp_path,monkeypatch,[{}, {"tool_calls":[call("write_file",arguments)]}])
    try:
        root=tmp_path/"project";root.mkdir();project=svc.create_project({"name":"Fixture","path":str(root)})
        run=svc.jobs.start({"text":"Report the result.","project_id":project["id"]})
        assert finished(svc,run)["status"]=="paused" and not (root/"bad.txt").exists()
        assert svc.store.run(run["id"])["failure"]["category"]=="protocol"
        assert len(engine.requests)==2 and not svc.store.run(run['id']).get('tool_repairs')
        assert not any(m.get('tool_calls') for m in svc.store.run_chat(run['chat_id'],0)['messages'])
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=?",(run["id"],)).fetchone()[0]==0
    finally:svc.shutdown()


def test_historical_terminal_projection_requires_strata_run_provenance(tmp_path,monkeypatch):
    svc,engine=native_service(tmp_path,monkeypatch,[{"content":"The visible answer."}])
    try:
        svc.jobs._launch=lambda run:None
        old=svc.jobs.start({"text":"Old fixture."});old_id=old["id"];chat_id=old["chat_id"]
        svc.store.save_assistant_turn(old_id,"<|im_end|>",interaction_run_id=old_id,
            thinking="Saved reasoning.",tool_calls=[call("list_files",{})])
        svc.store.add_message(chat_id,"tool","[]",tool_name="list_files")
        svc.store.add_message(chat_id,"user","<|im_end|>")
        svc.store.add_message(chat_id,"assistant","<|im_end|>")
        svc.store.update_run(old_id,status="completed",response_provider="Strata")
        current=svc.jobs.start({"text":"Continue from the evidence.","chat_id":chat_id})
        run=svc.store.run(current["id"])
        messages=svc.jobs._context(run,[])
        owned=next(m for m in messages if m.get("tool_calls"))
        assert owned["content"]=="" and owned["thinking"]=="Saved reasoning."
        assert {"role":"user","content":"<|im_end|>"} in messages
        assert {"role":"assistant","content":"<|im_end|>"} in messages
        saved=svc.store.run_chat(chat_id,0)["messages"]
        assert next(m for m in saved if m.get("tool_calls"))["content"]=="<|im_end|>"
    finally:svc.shutdown()


def test_native_correction_counter_and_message_commit_atomically(tmp_path,monkeypatch):
    svc,engine=native_service(tmp_path,monkeypatch,[])
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({"text":"Synthetic request."});identifier=accepted["id"]
        with svc.store._connection(transaction="write") as db:
            db.execute("CREATE TRIGGER reject_fixture_correction BEFORE INSERT ON messages WHEN NEW.role='user' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        with pytest.raises(ValueError,match="fixture rollback"):
            svc.store.request_native_final_answer(identifier)
        assert not svc.store.run(identifier).get("native_empty_result_corrections")
        with svc.store._connection(transaction="write") as db:db.execute("DROP TRIGGER reject_fixture_correction")
        assert svc.store.request_native_final_answer(identifier)
        assert not svc.store.request_native_final_answer(identifier)
        messages=svc.store.run_chat(accepted["chat_id"],0)["messages"]
        assert sum(m["role"]=="user" and "last turn ended" in m["content"] for m in messages)==1
    finally:svc.shutdown()
