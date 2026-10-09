"""Small journal-backed verification cadence for new guided local goals."""
import json

from prompt_compiler import verification_outcome,static_context_allowances
from forge_workflow import verification_sources
from tool_routing import select_schemas


FILE_MUTATIONS=frozenset(('write_file','edit_file','apply_patch','patch_files','restore_file','move_file','delete_file'))
BAD_OUTCOMES=('error','not_executed','outcome_unknown','cancelled','timed_out','partial','stale','unavailable')


def registered_check(schema):
    contract=schema.get('verification_contract')
    return (isinstance(contract,dict) and isinstance(contract.get('id'),str) and bool(contract['id']) and
        (bool(contract.get('expected_checks')) or contract.get('whole_result') is True or
         contract.get('result_adapter')=='builder_gate'))


class VerificationCadence:
    def __init__(self,manager):
        self.manager=manager;self.store=manager.store

    def enabled(self,run):
        return (self.manager.workflow.enabled(run) and not run.get('cloud_scope') and not run.get('readonly')
            and run.get('settings',{}).get('provider_id')=='ollama')

    def refresh(self,run,schemas):
        if not self.enabled(run):return None
        checks={schema['function']['name']:schema for schema in schemas if registered_check(schema) and
            self.manager.registry.capability(schema) in ('read','journal') and
            self.manager.registry.permission(run,schema,{})!='deny'}
        budget=static_context_allowances(run['settings'])['tools_bytes']
        # A configured checker with a schema too large for this context is not
        # actually loadable. Do not impose a gate that cannot offer its tool.
        checks={name:schema for name,schema in checks.items() if name in {item['function']['name']
            for item in select_schemas(schemas,run['request'][:4000],budget,[name]+run.get('loaded_tools',[]),phase='verify')}}
        with self.manager.lock:
            current=self.store.run(run['id']);old=current.get('verification_cadence') or {}
            goal=self.store.goal(run['goal_id'])
            with self.store._connection() as db:
                steer=db.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE chat_id=? AND json_extract(metadata,'$.interaction_run_id')=? AND json_extract(metadata,'$.steer_id') IS NOT NULL",
                    (run['chat_id'],run['id'])).fetchone()[0]
                epoch={'scope_revision':goal.get('scope_revision',1),'steer_message_id':steer}
                if old and old.get('epoch')!=epoch:
                    # Only accepted scope changes and actually applied steering
                    # reset this allowance. Checkboxes, answers and Resume do not.
                    last=db.execute('SELECT COALESCE(MAX(rowid),0) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]
                    count=0
                else:last=old.get('last_journal_row',0);count=old.get('mutations_since_check',0)
                names=sorted(FILE_MUTATIONS|set(checks))
                mutation_names=','.join('?' for _ in FILE_MUTATIONS)
                # Mutations retain their full immutable journal elsewhere. Only
                # outcome flags are needed here, so large diffs are never parsed.
                projection="CASE WHEN name IN ("+mutation_names+") THEN json_object('is_result',json_type(result,'$.result'),'ok',json_extract(result,'$.result.ok'),"+\
                    ','.join("'"+key+"',COALESCE(json_extract(result,'$.result."+key+"'),json_extract(result,'$."+key+"'))" for key in BAD_OUTCOMES)+") ELSE result END"
                rows=db.execute('SELECT rowid AS journal_row,name,arguments,'+projection+' AS outcome FROM invocations WHERE run_id=? AND rowid>? AND status=\'completed\' AND name IN ('+
                    ','.join('?' for _ in names)+') ORDER BY rowid LIMIT 4097',(*sorted(FILE_MUTATIONS),run['id'],last,*names)).fetchall()
            if len(rows)>4096:raise ValueError('Verification cadence journal exceeds its recovery bound. Inspect the saved goal history before continuing.')
            for row in rows:
                last=row['journal_row']
                try:wrapped=json.loads(row['outcome'] or '{}')
                except (ValueError,TypeError):continue
                if row['name'] in FILE_MUTATIONS:
                    if wrapped.get('is_result')=='object' and wrapped.get('ok') not in (False,0) and not any(wrapped.get(key) for key in BAD_OUTCOMES):
                        count=min(count+1,1_000_000)
                    continue
                schema=checks[row['name']];contract=schema['verification_contract']
                result=wrapped.get('result') if isinstance(wrapped,dict) else None
                if not isinstance(result,dict) or result.get('ok') is False or any(result.get(key) for key in BAD_OUTCOMES) or result.get('available') is False:continue
                try:arguments=json.loads(row['arguments']);outcome=verification_outcome(row['name'],arguments,result,contract)
                except (ValueError,TypeError):continue
                if not outcome or outcome.get('schema_version')!=2 or not outcome.get('available') or not outcome.get('current'):continue
                if outcome.get('passed') and not outcome.get('complete'):continue
                # A genuine failed checker (including its missing-file report)
                # starts a repair allowance, without establishing task completion.
                snapshots=wrapped.get('verification_context') or {}
                after=snapshots.get('after')
                if after is None:
                    from project_tools import ProjectTools
                    read_schema=next(item for item in ProjectTools.schemas() if item['function']['name']=='read_file')
                    after=verification_sources(self.store,run,contract,lambda path:self.manager.registry.permission(run,read_schema,{'path':path})=='allow')
                before=snapshots.get('before')
                if after and (not after.get('available') or before and (not before.get('available') or before.get('fingerprint')!=after.get('fingerprint'))):continue
                count=0
            previous=old.get('check_tool')
            check=previous if previous in checks else next(iter(checks),None)
            state={'schema_version':1,'epoch':epoch,'last_journal_row':last,'mutations_since_check':count,
                'check_tool':check,'contract_id':checks[check]['verification_contract']['id'] if check else None,
                'required':bool(check and count>=3)}
            if state!=old:self.store.update_run(run['id'],verification_cadence=state)
            return state

    @staticmethod
    def context(state):
        if not state or not state.get('required'):return None
        return ('Verification cadence: three file mutations committed since the last real registered check. '
            'Run '+state['check_tool']+' now. Additional file mutations are unexecuted until a real check returns; '
            'a failed check permits targeted repairs. Commands, reads and discovery remain available. '
            'Checkboxes, final answers and tool loading cannot substitute for verification.')
