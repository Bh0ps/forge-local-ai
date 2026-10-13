import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import App, { thinkingReadOwners } from '../App';
import { MessageView } from '../components';
import { DEFAULT_SETTINGS, type Message, type Run, type RunEvent, type SavedChat } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import type { ThinkingReadIntent } from '../thinkingDisclosure';

const thoughts = Array.from({length:70}, (_, index) => `Synthetic reasoning line ${index}.`).join('\n');
const answer = 'Synthetic answer.';
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(2000);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(200);
});
function earlier(element: HTMLElement, top = 210) { element.scrollTop = top; fireEvent.scroll(element); }
function disclosure(id?: number) {
  return document.querySelector<HTMLDetailsElement>(id === undefined ? '.thinking-disclosure' : `[data-message-id="${id}"] .thinking-disclosure`)!;
}
function region(details = disclosure()) { return details.querySelector<HTMLElement>('.thinking-scroll')!; }
function streamingDisclosure() { return document.querySelector('.stream-caret')?.closest('.message')?.querySelector<HTMLDetailsElement>('.thinking-disclosure'); }
async function explicitOpen(details = disclosure()) {
  if (details.open) { await userEvent.click(details.querySelector('summary')!); expect(details.open).toBe(false); }
  await userEvent.click(details.querySelector('summary')!); expect(details.open).toBe(true);
}
async function workspace(options: { requestId?: number; history?: Message[]; holdCompletedHistory?: boolean } = {}) {
  const run: Run = {id:'promotion-run',chat_id:'promotion-chat',request_message_id:options.requestId ?? 1,status:'running',mode:'chat',model:'fixture',request:'Synthetic request.'};
  const initial: SavedChat = {id:run.chat_id!,title:'Synthetic promotion chat',project_id:null,model:'fixture',messages:[...(options.history ?? []),{id:1,role:'user',content:'Synthetic request.'}]};
  let saved = initial, events: RunEvent[] = [], finished = false;
  let releaseHistory!: (chat: SavedChat) => void;
  const completedHistory = new Promise<SavedChat>(resolve => { releaseHistory = resolve; });
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'bootstrap') return {settings:{...DEFAULT_SETTINGS,model:'fixture',thinking:true},projects:[],chats:[initial],runs:[run],capabilities:{}};
    if (action === 'get_chat') return finished && options.holdCompletedHistory ? completedHistory : saved;
    if (action === 'poll') return {events:events.filter(event => event.seq! > Number(data.after || 0)),next_cursor:events.at(-1)?.seq ?? 0,status:finished?'completed':'running',finished,chat_id:run.chat_id};
    if (action === 'setup_status') return {first_run:false};
    if (action === 'ui_layout_get') return DEFAULT_LAYOUT;
    if (action === 'ui_layout_save') return data.layout;
    if (action === 'draft_get') return {revision:0,content:{},scope_available:true};
    if (action === 'draft_save') return {revision:Number(data.expected_revision)+1,content:data.content,scope_available:true};
    if (action === 'models') return {models:[{name:'fixture'}]};
    if (action === 'pending_questions') return {questions:[]};
    if (action === 'chats') return {chats:[saved]};
    if (action === 'runs') return {runs:[{...run,status:finished?'completed':'running'}]};
    if (['projects','schedules','spaces','plans'].includes(action)) return {[action]:[]};
    throw new Error('Unexpected synthetic action');
  });
  window.pywebview = {api:{call}};
  render(<App/>); await act(async () => window.dispatchEvent(new Event('pywebviewready')));
  await screen.findByText('Synthetic request.');
  function append(batch: Omit<RunEvent,'seq'>[]) {
    for (const event of batch) events.push({...event,seq:events.length+1} as RunEvent);
  }
  function finish(messages: Message[] = [{id:2,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:run.id}]) {
    saved = {...initial,messages:[...initial.messages,...messages]}; finished = true; append([{type:'done'}]);
  }
  append([{type:'thinking',text:thoughts},{type:'token',text:answer}]);
  await waitFor(() => expect(streamingDisclosure()).toBeTruthy());
  return {
    run, initial, append, call, finish, releaseHistory: () => releaseHistory(saved),
    async complete(messages: Message[] = [{id:2,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:run.id}]) {
      finish(messages);
      await waitFor(() => expect(document.querySelector('[data-message-id="2"]')).toBeTruthy());
    },
  };
}

it('control: an unchanged disclosure retains explicit opening and paused reading when streaming ends', async () => {
  const message = {id:2,role:'assistant',content:answer,thinking:thoughts};
  const view = render(<MessageView message={message} streaming/>);
  const details = disclosure(); await explicitOpen(details); const reading = region(details); earlier(reading);
  view.rerender(<MessageView message={message}/>);
  expect(disclosure()).toBe(details); expect(details.open).toBe(true); expect(reading.scrollTop).toBe(210);
});

it('actual App completion retains explicit opening, paused position, and the exact focused reasoning reader', async () => {
  const task = await workspace(); const live = disclosure(); await explicitOpen(live);
  const reading = region(live); earlier(reading); reading.focus();
  task.append([{type:'thinking',text:'\nMore synthetic reasoning.'}]);
  await waitFor(() => expect(reading.textContent).toContain('More synthetic reasoning.'));
  expect(reading.scrollTop).toBe(210);
  await task.complete([{id:2,role:'assistant',content:answer,thinking:thoughts+'\nMore synthetic reasoning.',interaction_run_id:task.run.id}]);
  const saved = disclosure(2); expect(saved).not.toBe(live); expect(saved.open).toBe(true);
  expect(region(saved).scrollTop).toBe(210); expect(document.activeElement).toBe(region(saved));
  await userEvent.click(screen.getByRole('button',{name:'Jump to latest reasoning'}));
  expect(region(saved).scrollTop).toBe(2000); expect(document.activeElement).toBe(region(saved));
});

it('completion preserves explicit follow while a different focused control keeps focus', async () => {
  const task = await workspace(); await explicitOpen(); earlier(region());
  await userEvent.click(screen.getByRole('button',{name:'Jump to latest reasoning'}));
  const composer = screen.getByRole('textbox',{name:'Message Forge'}); composer.focus();
  await task.complete();
  expect(disclosure(2).open).toBe(true); expect(region(disclosure(2)).scrollTop).toBe(2000);
  expect(document.activeElement).toBe(composer); expect(screen.queryByRole('button',{name:'Jump to latest reasoning'})).toBeNull();
});

it.each([
  {action:'close',activation:'click'}, {action:'reopen',activation:'click'},
  {action:'close',activation:'Enter'}, {action:'reopen',activation:' '},
])('records explicit $activation $action before the queued native toggle event can be lost at promotion', async ({action,activation}) => {
  const task = await workspace(); const live = disclosure(); await explicitOpen(live); earlier(region(live));
  if (action === 'reopen') { await userEvent.click(live.querySelector('summary')!); expect(live.open).toBe(false); }
  // Model a native toggle event that has not reached React before this node is retired.
  live.addEventListener('toggle', event => event.stopImmediatePropagation(), {capture:true});
  if (activation === 'click') fireEvent.click(live.querySelector('summary')!);
  else {
    fireEvent.keyDown(live.querySelector('summary')!,{key:activation});
    // jsdom omits the native summary keyboard default; its toggle remains queued.
    live.open = !live.open;
  }
  await task.complete();
  expect(disclosure(2).open).toBe(action === 'reopen');
  if (action === 'reopen') expect(region(disclosure(2)).scrollTop).toBe(2000);
});

it('unread live reasoning remains closed by default in the saved transcript', async () => {
  const task = await workspace(); expect(disclosure().open).toBe(true); expect(region().scrollTop).toBe(2000);
  fireEvent.scroll(region()); fireEvent.scroll(region());
  await task.complete(); expect(disclosure(2).open).toBe(false);
});

it('an explicitly focused reader at the latest position retains its exact region on completion', async () => {
  const task = await workspace(); region().focus(); await task.complete();
  expect(disclosure(2).open).toBe(true); expect(document.activeElement).toBe(region(disclosure(2)));
});

it('retains the owned keyboard reader across a real asynchronous saved-history gap without storing its DOM', async () => {
  const task = await workspace({holdCompletedHistory:true}); const reading = region(); earlier(reading); reading.focus();
  const completion = task.complete();
  await waitFor(() => expect(document.querySelector('.thinking-disclosure')).toBeNull());
  expect(reading.isConnected).toBe(false); expect(document.activeElement).toBe(document.body);
  await act(async () => task.releaseHistory()); await completion;
  expect(disclosure(2).open).toBe(true); expect(region(disclosure(2)).scrollTop).toBe(210);
  expect(document.activeElement).toBe(region(disclosure(2)));
});

it.each(['open','closed','unactivated'] as const)('retains the exact %s summary focus through delayed saved history without focusing hidden reasoning', async state => {
  const task = await workspace({holdCompletedHistory:true}); const live = disclosure();
  if (state !== 'unactivated') await explicitOpen(live);
  if (state === 'closed') await userEvent.click(live.querySelector('summary')!);
  const summary = live.querySelector<HTMLElement>('summary')!; summary.focus();
  expect(document.activeElement).toBe(summary);
  const completion = task.complete(); await waitFor(() => expect(document.querySelector('.thinking-disclosure')).toBeNull());
  expect(document.activeElement).toBe(document.body);
  await act(async () => task.releaseHistory()); await completion;
  expect(disclosure(2).open).toBe(state === 'open');
  expect(document.activeElement).toBe(disclosure(2).querySelector('summary')); expect(document.activeElement).not.toBe(region(disclosure(2)));
});

const focusControls = ['current-control','control-then-body','body-key','body-click','body-wheel','navigation-event'] as const;
it.each((['region','summary'] as const).flatMap(target=>focusControls.map(action=>({target,action}))))('intervening $action cancels pending $target focus during saved-history loading', async ({target,action}) => {
  const task = await workspace({holdCompletedHistory:true}); const reading = region(); earlier(reading);
  if (target === 'summary') disclosure().querySelector<HTMLElement>('summary')!.focus(); else reading.focus();
  const completion = task.complete(); await waitFor(() => expect(document.querySelector('.thinking-disclosure')).toBeNull());
  const composer = screen.getByRole('textbox',{name:'Message Forge'});
  if (action.includes('control')) { composer.focus(); expect(document.activeElement).toBe(composer); if (action === 'control-then-body') composer.blur(); }
  else if (action === 'body-key') fireEvent.keyDown(document.body,{key:'Tab'});
  else if (action === 'body-wheel') fireEvent.wheel(document.body,{deltaY:100});
  else if (action === 'navigation-event') window.dispatchEvent(new CustomEvent('forge:navigate',{detail:{page:'chat'}}));
  else fireEvent.click(document.body);
  await act(async () => task.releaseHistory()); await completion;
  expect(disclosure(2).open).toBe(true); expect(region(disclosure(2)).scrollTop).toBe(210);
  expect(document.activeElement).toBe(action === 'current-control' ? composer : document.body);
});

it.each((['region','summary'] as const).flatMap(target=>(['hidden','inactive'] as const).map(state=>({target,state}))))('does not restore the $target into an $state app', async ({target,state}) => {
  const task = await workspace({holdCompletedHistory:true}); const reading = region(); earlier(reading);
  if (target === 'summary') disclosure().querySelector<HTMLElement>('summary')!.focus(); else reading.focus();
  const completion = task.complete(); await waitFor(() => expect(document.querySelector('.thinking-disclosure')).toBeNull());
  if (state === 'hidden') vi.spyOn(document,'hidden','get').mockReturnValue(true);
  else vi.spyOn(document,'hasFocus').mockReturnValue(false);
  await act(async () => task.releaseHistory()); await completion;
  expect(disclosure(2).open).toBe(true); expect(region(disclosure(2)).scrollTop).toBe(210); expect(document.activeElement).toBe(document.body);
});

it('real navigation cancels pending focus and the late saved reply cannot change the new conversation', async () => {
  const task = await workspace({holdCompletedHistory:true}); earlier(region()); region().focus(); task.finish();
  await waitFor(() => expect(document.querySelector('.thinking-disclosure')).toBeNull());
  await userEvent.click(screen.getByRole('button',{name:/New chat/}));
  const composer = screen.getByRole('textbox',{name:'Message Forge'}); expect(document.activeElement).toBe(composer);
  await act(async () => task.releaseHistory());
  await screen.findByText('What are we building?'); expect(document.querySelector('[data-message-id="2"]')).toBeNull(); expect(document.activeElement).toBe(composer);
  await userEvent.click(screen.getByRole('button',{name:'Synthetic promotion chat'}));
  await waitFor(() => expect(disclosure(2)).toBeTruthy()); expect(document.activeElement).not.toBe(region(disclosure(2)));
});

const uncertainIdentities = ['foreign-run','missing-run','missing-anchor','foreign-chat','out-of-order'] as const;
it.each((['region','summary'] as const).flatMap(target=>uncertainIdentities.map(kind=>({target,kind}))))('does not transfer $target intent through $kind saved identity', async ({target,kind}) => {
  const task = await workspace({requestId:kind === 'missing-anchor'?99:1});
  await explicitOpen(); earlier(region());
  if (target === 'summary') disclosure().querySelector<HTMLElement>('summary')!.focus(); else region().focus();
  const message: Message = {id:2,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:task.run.id};
  if (kind === 'foreign-run') message.interaction_run_id = 'unrelated-run';
  if (kind === 'missing-run') delete message.interaction_run_id;
  if (kind === 'foreign-chat') task.initial.id = 'unrelated-chat';
  const messages: Message[] = kind === 'out-of-order' ? [{id:3,role:'tool',content:'Synthetic tool.'},message] : [message];
  await task.complete(messages); expect(disclosure(2).open).toBe(false); expect(region(disclosure(2)).scrollTop).toBe(0); expect(document.activeElement).toBe(document.body);
});

it('identical assistant turns retain separate intent through live parts and ordered saved promotion', async () => {
  const task = await workspace(); await explicitOpen(); earlier(region(),210);
  task.append([{type:'round'},{type:'thinking',text:thoughts},{type:'token',text:answer}]);
  await waitFor(() => expect(document.querySelectorAll('.thinking-disclosure')).toHaveLength(2));
  const [first, second] = [...document.querySelectorAll<HTMLDetailsElement>('.thinking-disclosure')];
  expect(first.open).toBe(true); expect(region(first).scrollTop).toBe(210);
  expect(second.open).toBe(true); expect(region(second).scrollTop).toBe(2000);
  await userEvent.click(first.querySelector('summary')!); expect(first.open).toBe(false);
  earlier(region(second),620);
  await task.complete([{id:2,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:task.run.id},{id:3,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:task.run.id}]);
  expect(disclosure(2).open).toBe(false); expect(disclosure(3).open).toBe(true); expect(region(disclosure(3)).scrollTop).toBe(620);
  expect(document.activeElement).toBe(disclosure(2).querySelector('summary'));
});

it('same-text older history never inherits the current run reader', async () => {
  const task = await workspace({history:[{id:10,role:'user',content:'Older synthetic request.'},{id:11,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:'older-run'}]});
  const live = streamingDisclosure()!;
  await explicitOpen(live); earlier(region(live)); await task.complete();
  expect(disclosure(11).open).toBe(false); expect(disclosure(2).open).toBe(true); expect(region(disclosure(2)).scrollTop).toBe(210);
});

it('bounds ephemeral owners to 256 and keeps evicted or unrelated history on its default behavior', () => {
  const reads = new Map<string,{chatId:string;intent:ThinkingReadIntent}>();
  const chat: SavedChat = {id:'bounded-chat',project_id:null,title:'Synthetic',model:'fixture',messages:[{id:1,role:'user',content:'Synthetic request.'}]};
  const live = (id: string) => ({run:{id,chat_id:chat.id,request_message_id:1},parts:[],text:answer,thinking:thoughts,tools:[],cursor:0,status:'running',finished:false});
  const original = live('original-run'), first = thinkingReadOwners(original,chat,reads,1,true).current!;
  first.current = {open:true,following:false,scrollTop:210,latestAvailable:true};
  for (let index=0; index<256; index++) thinkingReadOwners(live(`other-run-${index}`),chat,reads,1,true);
  expect(reads.size).toBe(256); expect(reads.has(first.key)).toBe(false);
  const saved = {...chat,messages:[...chat.messages,{id:2,role:'assistant',content:answer,thinking:thoughts,interaction_run_id:'original-run'}]};
  expect(thinkingReadOwners(original,saved,reads,1,false).saved.size).toBe(0);
  expect(thinkingReadOwners(original,saved,reads,2,false).saved.size).toBe(0);
  const serialized = JSON.stringify([...reads.values()]); expect(serialized).not.toContain(thoughts); expect(serialized).not.toContain(answer);
});

it('does not consume read-owner capacity for non-thinking turns or invent an owner for unread restored history', () => {
  const reads = new Map<string,{chatId:string;intent:ThinkingReadIntent}>();
  const chat: SavedChat = {id:'bounded-chat',project_id:null,title:'Synthetic',model:'fixture',messages:[{id:1,role:'user',content:'Synthetic request.'}]};
  const live = {run:{id:'bounded-run',chat_id:chat.id,request_message_id:1},parts:Array.from({length:300},()=>({role:'assistant',content:answer})),text:answer,thinking:thoughts,tools:[],cursor:0,status:'running',finished:false};
  const owners = thinkingReadOwners(live,chat,reads,1,true);
  expect(owners.parts).toHaveLength(300); expect(owners.parts.every(owner=>owner === undefined)).toBe(true); expect(reads.size).toBe(1);
  owners.current!.current = {open:true,following:false,scrollTop:210,latestAvailable:true};
  const retired = {...live,run:{...live.run,id:'unread-restored-run'}};
  expect(thinkingReadOwners(retired,chat,reads,1,false).current).toBeUndefined(); expect(reads.size).toBe(1);
});
