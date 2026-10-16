import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { applyEvents } from '../App';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { DEFAULT_SETTINGS } from '../types';

function live(): Parameters<typeof applyEvents>[0] {
  return {run: {id: 'run', chat_id: 'chat', status: 'running'}, text: 'Answer', thinking: '', parts: [], tools: [], cursor: 4, status: 'Generating', finished: false};
}

describe('quiet cursor polling', () => {
  it('retains live object identity for empty polls and already consumed replay events', () => {
    const current = live();
    expect(applyEvents(current, [], {next_cursor: 4, status: 'running', finished: false})).toBe(current);
    expect(applyEvents(current, [{seq: 4, type: 'token', text: 'duplicate'}], {next_cursor: 4})).toBe(current);
  });

  it('still admits new output, coordinator state and recovery information', () => {
    const current = live();
    expect(applyEvents(current, [{seq: 5, type: 'token', text: ' continued'}], {next_cursor: 5}).text).toBe('Answer continued');
    const paused = applyEvents(current, [], {status: 'paused', recovery: 'needs_action', finished: true});
    expect(paused).not.toBe(current);
    expect(paused.run.status).toBe('paused');
    expect(paused.run.recovery).toBe('needs_action');
    expect(paused.finished).toBe(true);
    expect(applyEvents(current, [{type: 'status', text: 'Waiting for model'}], {}).status).toBe('Waiting for model');
  });

  it('does not drop progress or planner updates on otherwise empty polls', () => {
    const current = live();
    const progress = {activity: 'Checking the project'};
    const next = applyEvents(current, [], {progress_summary: progress});
    expect(next).not.toBe(current);
    expect(next.run.progress_summary).toBe(progress);
  });

  it('retains identity for real poll responses with fresh but unchanged progress and planner snapshots', () => {
    const current = live();
    current.run.progress_summary = {activity: 'Checking the project', verified_task_ids: ['first'], available_actions: ['pause']};
    current.run.planner_assignment = {state: 'consumed', run_id: 'helper'};
    const response = JSON.parse(JSON.stringify({events: [], next_cursor: 4, status: 'running', finished: false,
      progress_summary: current.run.progress_summary, planner_assignment: current.run.planner_assignment}));
    expect(applyEvents(current, response.events, response)).toBe(current);
    response.progress_summary.verified_task_ids.push('second');
    expect(applyEvents(current, response.events, response)).not.toBe(current);
    response.progress_summary = current.run.progress_summary;
    response.planner_assignment.state = 'unavailable';
    expect(applyEvents(current, response.events, response)).not.toBe(current);
  });
});

afterEach(() => {vi.useRealTimers();});

it('polls less often while the native window is hidden and replays immediately on return', async () => {
  vi.useFakeTimers();
  const run = {id: 'run', chat_id: 'chat', status: 'running', request_message_id: 1};
  const chat = {id: 'chat', title: 'Fixture', model: 'fixture', project_id: null, messages: [{id: 1, role: 'user', content: 'Check the fixture'}]};
  const responses: Record<string, unknown> = {
    bootstrap: {settings: {...DEFAULT_SETTINGS, model: 'fixture'}, projects: [], chats: [chat], runs: [run], capabilities: {}},
    get_chat: chat, models: {models: [{name: 'fixture'}]}, projects: {projects: []}, chats: {chats: [chat]},
    runs: {runs: [run]}, schedules: {schedules: []}, spaces: {spaces: []}, pending_questions: {questions: []},
    setup_status: {first_run: false}, draft_get: {revision: 0, content: {}}, ui_layout_get: DEFAULT_LAYOUT,
    host_status: {visible: true, visibility_seq: 1}, poll: {events: [], next_cursor: 0, status: 'running', finished: false},
  };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'ui_layout_save') return data.layout;
    if (action === 'draft_save') return {revision: 1, content: data.content};
    if (!(action in responses)) throw new Error(`Unexpected fixture action: ${action}`);
    return responses[action];
  });
  window.pywebview = {api: {call}};
  render(<App/>);
  await act(async () => {window.dispatchEvent(new Event('pywebviewready')); await vi.advanceTimersByTimeAsync(900);});
  await act(async () => {await vi.advanceTimersByTimeAsync(450);});
  const count = () => call.mock.calls.filter(([action]) => action === 'poll').length;
  expect(count()).toBeGreaterThan(0);
  await act(async () => {fireEvent(window, new CustomEvent('forge:visibility', {detail: {visible: false, seq: 2}}));});
  const hidden = count();
  await act(async () => {await vi.advanceTimersByTimeAsync(2400);});
  expect(count()).toBe(hidden);
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  expect(count()).toBe(hidden + 1);
  await act(async () => {fireEvent(window, new CustomEvent('forge:visibility', {detail: {visible: true, seq: 3}}));});
  expect(count()).toBe(hidden + 2);
  expect(call.mock.calls.some(([action]) => ['cancel', 'pause', 'run_control'].includes(action))).toBe(false);
});
