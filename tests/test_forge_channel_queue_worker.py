"""Real queue/admission boundaries with synthetic providers and transports only."""
import json
from types import SimpleNamespace

import pytest

import forge_channel_queue as derived
import forge_channels
from test_forge_channel_inbound_fairness import queue,config,busy,row,restart
from test_forge_channels import env,configured,update


def insert(queue,channel,number,recipient='42',text='Synthetic request'):
    payload={'update_id':number,'message':{'chat':{'id':recipient},'from':{'id':recipient},'text':text}}
    assert queue.manager._record_inbound(channel['id'],str(number),payload)


@pytest.mark.parametrize('mutation',['recipient','lane','delete'])
def test_cache_mutation_cannot_hide_older_original_head(queue,mutation):
    channel=config(queue)
    insert(queue,channel,1)
    insert(queue,channel,2)
    with queue.store._connection(transaction='write') as db:
        original=db.execute('SELECT channel_id,external_id,queue_seq,created_at,generation,lane,recipient,data_hash FROM channel_inbound_queue WHERE external_id=?',('1',)).fetchone()
        queue.manager.inbound_queue.advance(db,tuple(original))
        if mutation=='delete':db.execute("DELETE FROM channel_inbound_queue WHERE external_id='1'")
        elif mutation=='recipient':db.execute("UPDATE channel_inbound_queue SET recipient='999' WHERE external_id='1'")
        else:db.execute("UPDATE channel_inbound_queue SET lane='control' WHERE external_id='1'")
        assert db.execute("SELECT ready FROM channel_queue_generations WHERE scope=1").fetchone()[0]==0
    queue.manager.tick(poll=False)
    assert [data['source_key'] for data in queue.started]==['channel:fixture-channel:1']
    assert row(queue,channel,2)['status']=='pending'


def test_actual_json_work_is_shared_by_maintenance_poll_classification_and_dispatch(queue,monkeypatch):
    channel=config(queue)
    busy(queue,channel)
    for number in range(50):
        payload={'message':{'chat':{'id':'42'},'from':{'id':'42'},'text':'Synthetic legacy '+str(number)}}
        with queue.store._connection(transaction='write') as db:
            db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)',(channel['id'],str(number),'pending',json.dumps(payload),str(number),'synthetic'))
    loads=json.loads
    decoded=[]
    def count(body):
        result=loads(body)
        if isinstance(result,dict) and 'message' in result:decoded.append(body)
        return result
    monkeypatch.setattr(derived,'json',SimpleNamespace(loads=count))
    monkeypatch.setattr(forge_channels,'json',SimpleNamespace(loads=count))
    monkeypatch.setattr(queue.manager,'_refresh_command_menu',lambda cfg:cfg)
    def receive(cfg):
        for number in range(50,180):insert(queue,cfg,number)
    monkeypatch.setattr(queue.manager,'_poll_telegram',receive)
    queue.manager.tick(poll=True)
    work=queue.manager.inbound_queue.last_work
    assert len(decoded)==work['payload_work']<=200
    assert work['payload_classifications']==150 and work['dispatch_calls']<=50
    with queue.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='unclassified'").fetchone()[0]==30
    decoded.clear()
    queue.manager.tick(poll=False)
    assert len(decoded)<=queue.manager.inbound_queue.last_work['payload_work']<=200
    with queue.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='unclassified'").fetchone()[0]==0
    assert not queue.started


def test_dispatch_limit_and_original_identity_hold_when_payload_allowance_is_available(queue):
    channel=config(queue,recipients=tuple(str(number) for number in range(100,157)))
    for number in range(57):insert(queue,channel,number,str(100+number))
    queue.manager.tick(poll=False)
    assert len(queue.started)==50
    assert queue.manager.inbound_queue.last_work['payload_work']<=200
    assert queue.manager.inbound_queue.last_work['dispatch_calls']==50
    queue.manager.tick(poll=False)
    assert len(queue.started)==57 and len({data['source_key'] for data in queue.started})==57


def test_control_crash_after_pending_claim_stays_unknown_and_never_replays(queue,monkeypatch):
    channel=config(queue)
    busy(queue,channel)
    insert(queue,channel,1,text='/pause')
    calls=[]
    def interrupted(*args):
        calls.append(True)
        raise RuntimeError('Synthetic interruption after claim')
    monkeypatch.setattr(queue.manager,'_command',interrupted)
    queue.manager.tick(poll=False)
    assert row(queue,channel,1)['status']=='outcome_unknown' and calls==[True]
    restart(queue).tick(poll=False)
    assert row(queue,channel,1)['status']=='outcome_unknown' and calls==[True] and not queue.cancelled


def test_pairing_configuration_is_rechecked_in_conditional_claim_transaction(queue,monkeypatch):
    from hashlib import sha256
    channel=config(queue,pair_hash=sha256(b'synthetic-code').hexdigest(),pair_expires=2000)
    insert(queue,channel,1,text='/pair synthetic-code')
    status=queue.manager._inbound_status
    changed=[]
    def revoke(identifier,external,state,*args,**kwargs):
        if state=='dispatching' and not changed:
            changed.append(True)
            queue.store.save_entity('channels',{**channel,'pair_hash':'0'*64})
        return status(identifier,external,state,*args,**kwargs)
    monkeypatch.setattr(queue.manager,'_inbound_status',revoke)
    queue.manager.tick(poll=False)
    assert row(queue,channel,1)['status']=='pending'
    assert queue.store.entities('channel_pairings')==[] and not queue.started


def test_pairing_revoked_after_claim_is_not_saved_by_the_pairing_transaction(queue,monkeypatch):
    from hashlib import sha256
    channel=config(queue,pair_hash=sha256(b'synthetic-code').hexdigest(),pair_expires=2000)
    insert(queue,channel,1,text='/pair synthetic-code')
    pair=queue.manager._pair_request
    def revoked(cfg,*args):
        queue.store.save_entity('channels',{**cfg,'pair_hash':'0'*64})
        return pair(cfg,*args)
    monkeypatch.setattr(queue.manager,'_pair_request',revoked)
    queue.manager.tick(poll=False)
    assert row(queue,channel,1)['status']=='rejected'
    assert queue.store.entities('channel_pairings')==[]


def test_callback_owner_revoked_after_source_claim_cannot_consume_approval(queue,monkeypatch):
    from hashlib import sha256
    from forge_store import encode
    channel=config(queue)
    held=busy(queue,channel)
    action={'tool':'read_file','arguments':{'path':'synthetic.txt'}}
    digest=sha256(encode(action).encode()).hexdigest()
    with queue.store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',('approval',held['id'],'invocation',digest,'pending',encode(action)))
        db.execute('INSERT INTO channel_callbacks VALUES(?,?,?,?,?,?,?,?,?,?)',('synthetic-token',channel['id'],'42','42',held['id'],'approval',digest,1,2000,None))
    payload={'callback_query':{'from':{'id':42},'message':{'chat':{'id':42}},'data':'forge:synthetic-token'}}
    queue.manager._record_inbound(channel['id'],'1',payload)
    callback=queue.manager._callback
    def revoke(cfg,*args):
        queue.store.save_entity('channels',{**cfg,'allowlist':[]})
        return callback(cfg,*args)
    monkeypatch.setattr(queue.manager,'_callback',revoke)
    queue.manager.tick(poll=False)
    assert row(queue,channel,1)['status']=='rejected' and queue.approved==[]
    with queue.store._connection() as db:
        assert db.execute('SELECT used_at FROM channel_callbacks').fetchone()[0] is None


def test_corrupt_metadata_blocks_poll_and_keeps_original_request_and_local_chat(queue,monkeypatch):
    channel=config(queue)
    insert(queue,channel,1)
    polled=[]
    monkeypatch.setattr(queue.manager,'_poll_telegram',lambda cfg:polled.append(True))
    monkeypatch.setattr(queue.manager,'_refresh_command_menu',lambda cfg:cfg)
    with queue.store._connection(transaction='write') as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute("UPDATE channel_queue_generations SET scan_seq=-1 WHERE scope=0")
    queue.manager.tick(poll=True)
    assert queue.store.channel_queue_reason=='metadata_unavailable' and polled==[]
    assert row(queue,channel,1)['status']=='pending' and queue.started==[]
    assert queue.store.create_chat(title='Synthetic local chat')['title']=='Synthetic local chat'


def test_metadata_failure_during_tick_stops_outbound_before_any_delivery(queue,monkeypatch):
    channel=config(queue)
    insert(queue,channel,1)
    queue.manager._enqueue(channel['id'],'42','command',{'chat_id':'42','text':'Synthetic reply'},'synthetic-outbound')
    delivered=[]
    monkeypatch.setattr(queue.manager,'_deliver',lambda record:delivered.append(record['id']))
    with queue.store._connection(transaction='write') as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE channel_queue_generations SET scan_seq=-1 WHERE scope=0')
    queue.manager.tick(poll=False)
    assert queue.store.channel_queue_reason=='metadata_unavailable' and delivered==[]
    with queue.store._connection() as db:
        assert db.execute('SELECT status FROM channel_outbox').fetchone()[0]=='pending'


def test_invalid_utf8_original_payload_disables_channels_without_raw_diagnostics(queue):
    channel=config(queue)
    insert(queue,channel,1)
    with queue.store._connection(transaction='write') as db:
        db.execute("UPDATE channel_inbound SET data=CAST(? AS TEXT) WHERE external_id='1'",(b'\xffsynthetic',))
    queue.manager.tick(poll=False)
    assert queue.store.channel_queue_reason=='metadata_unavailable' and queue.started==[]
    with queue.store._connection() as db:
        assert tuple(db.execute('SELECT status,CAST(data AS BLOB),error FROM channel_inbound').fetchone())==('pending',b'\xffsynthetic',None)
    assert queue.store.create_chat(title='Synthetic local chat')['title']=='Synthetic local chat'


@pytest.mark.parametrize('fence',['stop','update'])
@pytest.mark.parametrize('after_row',[1,3])
def test_maintenance_admission_race_rolls_back_and_resumes_without_disabling_channels(queue,monkeypatch,fence,after_row):
    channel=config(queue)
    with queue.store._connection(transaction='write') as db:
        for number in range(3):
            payload=json.dumps({'message':{'chat':{'id':'42'},'from':{'id':'42'},'text':'Synthetic request '+str(number)}})
            db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)',(channel['id'],str(number),'pending',payload,str(number),'synthetic'))
    index=derived._index_row
    indexed=[]
    def fence_after_write(*args):
        result=index(*args)
        indexed.append(True)
        if len(indexed)==after_row:
            if fence=='stop':queue.manager.stop_event.set()
            else:queue.service.update_manager.applying=True
        return result
    monkeypatch.setattr(derived,'_index_row',fence_after_write)
    queue.manager.tick(poll=False)
    assert queue.store.channel_queue_reason is None and queue.started==[]
    with queue.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='unclassified'").fetchone()[0]==3
        assert tuple(db.execute('SELECT cursor,ready FROM channel_queue_generations WHERE scope=1').fetchone())==(0,0)
    queue.service.update_manager.applying=False
    restart(queue).tick(poll=False)
    assert [data['source_key'] for data in queue.started]==['channel:fixture-channel:0']


def test_real_run_admission_uses_only_internal_validator_in_write_transactions(env,monkeypatch):
    channel=configured(env)
    observations=[]
    validate=derived.InboundQueue.validate_selection
    def checked(self,db,item,budget=None):
        observations.append(db.in_transaction)
        return validate(self,db,item,budget)
    monkeypatch.setattr(derived.InboundQueue,'validate_selection',checked)
    env.manager.receive_telegram(channel['id'],update(1))
    env.manager.tick(poll=False)
    assert observations==[True,True]
    runs=env.service.store.runs()
    assert len(runs)==1 and runs[0]['source_key']=='channel:'+channel['id']+':1'
    assert not {'selection','validator','channel_selection'}&runs[0].keys()
    assert env.manager.inbound_queue.last_work['payload_work']==4


@pytest.mark.parametrize('change',['revoke_channel','stop_worker'])
def test_final_run_claim_defers_without_new_messages_or_tasks_when_admission_changes(env,monkeypatch,change):
    channel=configured(env)
    env.manager.receive_telegram(channel['id'],update(1))
    accept=env.service.store.accept_request
    changed=[]
    def between(*args,**kwargs):
        if isinstance(kwargs.get('channel'),derived.ChannelAdmission) and not changed:
            changed.append(True)
            if change=='revoke_channel':env.service.store.save_entity('channels',{**env.service.store.entity('channels',channel['id']),'enabled':False})
            else:env.manager.stop_event.set()
        return accept(*args,**kwargs)
    monkeypatch.setattr(env.service.store,'accept_request',between)
    env.manager.tick(poll=False)
    assert changed==[True] and env.service.store.runs()==[]
    with env.service.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM request_keys').fetchone()[0]==0
        assert db.execute('SELECT status FROM channel_inbound').fetchone()[0]=='pending'
