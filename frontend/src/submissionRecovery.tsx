import { useState } from 'react';
import { api, errorText } from './api';
export interface SubmissionScope { action: string; client_submission_id: string; project_id?: string | null; chat_id?: string | null; id?: string; run_id?: string; }
export function SubmissionRecovery({submission}: {submission:SubmissionScope}) {
  const [result,setResult]=useState<{state?:string;run_id?:string;admission_error?:string}|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  async function inspect(){setBusy(true);setError('');try{setResult(await api('submission_get',{...submission}));}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  return <div className="submission-recovery"><button className="text-button" disabled={busy} onClick={()=>void inspect()}>Inspect submission status</button>{error && <p role="alert">{error}</p>}{result && <div role="status"><strong>Submission: {result.state || 'unavailable'}</strong>{result.run_id && <small>Recorded run {result.run_id}. Retrying this unchanged request reconnects to its saved outcome.</small>}{result.state==='unknown' && <small>The coordinator has no confirmed outcome. Keep this draft and inspect the originating chat or action before creating another request.</small>}{result.admission_error && <small>{result.admission_error}</small>}</div>}</div>;
}
