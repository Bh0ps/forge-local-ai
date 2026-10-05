import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronRight, MessageCircleQuestion } from 'lucide-react';
import { api, errorText } from './api';
import type { QuestionAnswer, UserQuestion } from './types';
import './workflowControls.css';

export function QuestionCard({ request, onAnswered }: { request: UserQuestion; onAnswered: () => void }) {
  const [answers, setAnswers] = useState<Record<string, QuestionAnswer>>({});
  const [other, setOther] = useState<Record<string, boolean>>({});
  const [collapsed, setCollapsed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const valid = request.items.length > 0 && request.items.every(item => other[item.id] ? Boolean(answers[item.id]?.text?.trim()) : Boolean(answers[item.id]?.option));
  async function submit() {
    if (!valid || busy) return;
    setBusy(true); setError('');
    try {
      const payload = Object.fromEntries(request.items.map(item => [item.id, other[item.id] ? { text: answers[item.id].text!.trim() } : { option: answers[item.id].option }]));
      await api('answer_question', { question_id: request.id, answers: payload });
      onAnswered();
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  return <section className="question-card" aria-label="Agent questions">
    <button className="question-heading" type="button" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed}>
      <MessageCircleQuestion size={16} /><strong>Forge needs your input</strong><span>{request.items.length > 1 ? `${request.items.length} questions` : 'Choose a preference'}</span>{collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
    </button>
    {!collapsed && <form onSubmit={e => { e.preventDefault(); void submit(); }}>
      {request.items.map((item, index) => <fieldset key={item.id} disabled={busy}>
        <legend>{request.items.length > 1 && <span className="question-number">{index + 1}.</span>}{item.question}</legend>
        <div className="question-options">
          {[...item.options].sort((a, b) => Number(Boolean(b.recommended)) - Number(Boolean(a.recommended))).map((option, optionIndex) => <label key={`${option.label}-${optionIndex}`} className={`question-option ${!other[item.id] && answers[item.id]?.option === option.label ? 'selected' : ''}`}>
            <input type="radio" name={`question-${request.id}-${item.id}`} checked={!other[item.id] && answers[item.id]?.option === option.label} onChange={() => { setOther(prev => ({ ...prev, [item.id]: false })); setAnswers(prev => ({ ...prev, [item.id]: { option: option.label } })); }} />
            <span><strong>{option.label.replace(/\s*\(recommended\)\s*$/i, '')}{option.recommended && <small className="question-recommended">Recommended</small>}</strong>{option.description && <small>{option.description}</small>}</span>
          </label>)}
          {item.allow_free_text !== false && <label className={`question-option ${other[item.id] ? 'selected' : ''}`}>
            <input type="radio" name={`question-${request.id}-${item.id}`} checked={Boolean(other[item.id])} onChange={() => { setOther(prev => ({ ...prev, [item.id]: true })); setAnswers(prev => ({ ...prev, [item.id]: { text: '' } })); }} /><span><strong>Other</strong><small>Write your own preference</small></span>
          </label>}
        </div>
        {other[item.id] && <textarea aria-label={`Your answer: ${item.header || item.question}`} placeholder="Tell Forge your preference…" value={answers[item.id]?.text || ''} onChange={e => setAnswers(prev => ({ ...prev, [item.id]: { text: e.target.value } }))} rows={2} maxLength={4000} />}
      </fieldset>)}
      {error && <p className="question-error" role="alert">{error}</p>}
      <footer><small>The run waits for your answers.</small><button type="submit" className="primary" disabled={!valid || busy}><Check size={13} />{busy ? 'Sending…' : 'Continue'}</button></footer>
    </form>}
  </section>;
}

export function QuestionPanel({ chatId, active, notify, onPending }: { chatId: string | null; active: boolean; notify: (text: string) => void; onPending?: (count: number) => void }) {
  const [requests, setRequests] = useState<UserQuestion[]>([]);
  const currentChat = useRef(chatId); currentChat.current = chatId;
  const revision = useRef(0);
  const reload = useRef<() => Promise<void>>(async () => {});
  const pendingListener = useRef(onPending); pendingListener.current = onPending;
  useEffect(() => { pendingListener.current?.(requests.length); }, [requests]);
  useEffect(() => {
    let disposed = false; let fetching = false;
    setRequests(previous => previous.filter(request => request.chat_id === chatId));
    if (!chatId) return;
    const load = async () => {
      if (disposed || fetching) return;
      fetching = true;
      const requestRevision = revision.current;
      try {
        const result = await api<{ questions: UserQuestion[] }>('pending_questions', { chat_id: chatId });
        if (!disposed && requestRevision === revision.current) setRequests((result.questions || []).filter(request => request.chat_id === chatId && request.status === 'pending'));
      } catch { /* Transient reconnects retain the pending question and any selections. */ }
      finally { fetching = false; }
    };
    reload.current = load;
    void load();
    const timer = active ? setInterval(() => void load(), 1500) : undefined;
    return () => { disposed = true; if (timer) clearInterval(timer); };
  }, [chatId, active]);
  return <div className="question-stack">{requests.map(request => <QuestionCard key={request.id} request={request} onAnswered={() => {
    if (currentChat.current !== request.chat_id) return;
    revision.current++;
    setRequests(prev => prev.filter(item => item.id !== request.id));
    notify('Answers sent. Forge will continue.');
    void reload.current();
  }} />)}</div>;
}
