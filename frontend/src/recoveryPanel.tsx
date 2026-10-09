import { useEffect, useRef, useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { api, errorText } from './api';
import { ErrorNotice, Field, Modal } from './components';
interface UnknownAction { id: string; name: string; arguments: string | Record<string, unknown>; status: string; }
export interface RecoveryInspection { run_id: string; state: 'loading' | 'ready' | 'error'; unknown_count: number; }
export function RecoveryPanel({ runId, recovery, notify, onInspectionChange, compact = false, expanded = false }: { runId: string; recovery?: string; notify: (message: string) => void; onInspectionChange?: (inspection: RecoveryInspection) => void; compact?: boolean; expanded?: boolean }) {
  const [data, setData] = useState<{run_id: string; items: UnknownAction[]; loading: boolean; error: string}>({run_id: runId, items: [], loading: true, error: ''});
  const [selection, setSelection] = useState<{run_id: string; action: UnknownAction} | null>(null);
  const [evidence, setEvidence] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const currentRun = useRef(runId), lifecycle = useRef(0), request = useRef(0), callback = useRef(onInspectionChange);
  currentRun.current = runId; callback.current = onInspectionChange;
  const current = (id: string, epoch: number) => currentRun.current === id && lifecycle.current === epoch;
  async function refresh(id: string, epoch: number) {
    if (!current(id, epoch)) return;
    const ticket = ++request.current;
    setData(previous => ({run_id: id, items: previous.run_id === id ? previous.items : [], loading: true, error: ''}));
    callback.current?.({run_id: id, state: 'loading', unknown_count: 0});
    try {
      const result = await api<{actions: UnknownAction[]}>('unknown_actions', {run_id: id});
      if (!Array.isArray(result.actions)) throw new Error('Interrupted actions could not be inspected. Retry before resuming.');
      if (!current(id, epoch) || ticket !== request.current) return;
      setData({run_id: id, items: result.actions, loading: false, error: ''});
      callback.current?.({run_id: id, state: 'ready', unknown_count: result.actions.length});
    } catch (failure) {
      if (!current(id, epoch) || ticket !== request.current) return;
      setData(previous => ({...previous, loading: false, error: errorText(failure)}));
      callback.current?.({run_id: id, state: 'error', unknown_count: 0});
    }
  }
  useEffect(() => {
    const epoch = ++lifecycle.current;
    setSelection(null); setEvidence(''); setError(''); setBusy(false);
    void refresh(runId, epoch);
    return () => { if (lifecycle.current === epoch) lifecycle.current++; };
  }, [runId]);
  const action = selection?.run_id === runId ? selection.action : null;
  async function resolve(outcome: string) {
    if (!action) return;
    const id = runId, epoch = lifecycle.current, selected = action;
    setBusy(true);
    try {
      await api('resolve_action', {invocation_id: selected.id, outcome, evidence});
      if (!current(id, epoch)) return;
      setSelection(null);
      await refresh(id, epoch);
      if (current(id, epoch)) notify('Inspection recorded. Resume when you are ready.');
    } catch (failure) { if (current(id, epoch)) setError(errorText(failure)); }
    finally { if (current(id, epoch)) setBusy(false); }
  }
  const visible = data.run_id === runId ? data : {items: [], loading: true, error: ''};
  return <>{recovery && (!compact || expanded) && <p className="recovery-note">{recovery}</p>}{visible.loading && !compact && <small role="status">Checking interrupted actions…</small>}{visible.items.map(item => <button className="recovery-action" aria-label={`Inspect ${item.name}`} title={`Inspect ${item.name}`} key={item.id} onClick={() => { setSelection({run_id: runId, action: item}); setEvidence(''); setError(''); }}><ShieldAlert size={13} />{compact && !expanded ? <span className="sr-only">Inspect {item.name}</span> : <span>Inspect {item.name}</span>}</button>)}{visible.error && <ErrorNotice error={visible.error} retry={() => void refresh(runId, lifecycle.current)} />}{action && <Modal title="Inspect an interrupted action" close={() => setSelection(null)} wide><p className="muted">Forge cannot verify whether this action finished. Inspect the affected files, app or command output before recording its outcome. This action will not be repeated automatically.</p><strong>{action.name}</strong><pre className="inspection-arguments">{typeof action.arguments === 'string' ? action.arguments : JSON.stringify(action.arguments, null, 2)}</pre><Field label="Inspection evidence"><textarea autoFocus rows={4} value={evidence} onChange={e => setEvidence(e.target.value)} placeholder="What did you inspect, and what confirms the outcome?" /></Field>{error && <ErrorNotice error={error} />}<footer><button className="secondary" disabled={!evidence.trim() || busy} onClick={() => void resolve('not_executed')}>Verified not executed</button><button className="primary" disabled={!evidence.trim() || busy} onClick={() => void resolve('completed')}>Verified completed</button></footer></Modal>}</>;
}
