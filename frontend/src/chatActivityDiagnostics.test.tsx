import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { activityFailureLabel, ChatActivity, transcriptActivity } from './chatActivity';
import type { ActivityGroup } from './types';

const failure = {provider_id:'openrouter',category:'continuation_unavailable',continuation_reason:'context_limit' as const,requested_context:131072,model_context_limit:65536};
const group:ActivityGroup = {run_id:'synthetic',status:'paused',tool_count:0,helper_count:0,children:[],failure};

describe('Routed continuation explanation',()=>{
  it('shows the actual context mismatch over an older generic saved recovery message',()=>{
    render(<ChatActivity group={{...group,summary:'The selected model could not continue the tool exchange.'}} helper/>);
    expect(screen.getByText('This route supports 65,536 tokens; the helper requests 131,072. No tool from this response ran.')).toBeTruthy();
    expect(screen.queryByText('The selected model could not continue the tool exchange.')).toBeNull();
    expect(screen.queryByRole('button',{name:/Change context/})).toBeNull();
  });
  it('does not display forged or non-numeric context limits',()=>{
    const label=activityFailureLabel({...failure,model_context_limit:NaN});
    expect(label).not.toContain('NaN');
    expect(label).toContain('could not continue');
  });
  it('distinguishes an unadvertised free model from missing capabilities',()=>{
    expect(activityFailureLabel({...failure,continuation_reason:'model_unavailable'})).toContain('exact free model');
    expect(activityFailureLabel({...failure,continuation_reason:'capabilities'})).toContain('capabilities');
    expect(activityFailureLabel({category:'refusal'})).not.toContain('context');
  });
  it('shows a newly paused live helper before history is fetched again',()=>{
    const groups=transcriptActivity({id:'chat',title:'Synthetic',model:'fixture',project_id:null,messages:[],activity_groups:[{...group,run_id:'parent',status:'running',failure:undefined}]},[
      {run:{id:'child',parent_id:'parent',chat_id:'chat',status:'paused',failure},tools:[],status:'Paused',finished:true}]);
    render(<ChatActivity group={groups[0].children[0]} helper/>);
    expect(screen.getByText(/This route supports 65,536 tokens/)).toBeTruthy();
  });
  it('clears the previous pause after a live successful completion',()=>{
    const groups=transcriptActivity({id:'chat',title:'Synthetic',model:'fixture',project_id:null,messages:[],activity_groups:[{...group,summary:'The previous attempt paused.'}]},[
      {run:{id:'synthetic',chat_id:'chat',status:'completed',failure:null,last_finish_reason:'stop'},tools:[],status:'Completed',finished:true}]);
    render(<ChatActivity group={groups[0]} helper/>);
    expect(screen.queryByText(/This route supports/)).toBeNull();
    expect(screen.queryByText('The previous attempt paused.')).toBeNull();
    expect(groups[0].status).toBe('completed');
  });
});
