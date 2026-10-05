import { describe, expect, it } from 'vitest';
import { applyEvents } from './App';
const initial = () => ({ run: { id: 'run' }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'running', finished: false });
describe('durable activity and speed events', () => {
  it('updates a web fetch card in place before a completed turn', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'running', arguments: { url: 'https://example.com' } }, { seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'done', result: { artifact: 'saved' } }, { seq: 3, type: 'done', status: 'completed' }], { finished: true, status: 'completed' });
    expect(live.tools).toHaveLength(1); expect(live.tools[0].state).toBe('done'); expect(live.tools[0].arguments).toEqual({ url: 'https://example.com' }); expect(live.finished).toBe(true);
  });
  it('keeps different calls to the same tool distinct and deduplicates replay', () => {
    let live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'running' }, { seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }], {});
    live = applyEvents(live, [{ seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }, { seq: 3, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'done' }], {});
    expect(live.tools).toHaveLength(2); expect(live.tools.map(t => t.state)).toEqual(['done', 'running']);
  });
  it('uses backend speed rather than counting text delivered by polling', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'token', text: 'Buffered output '.repeat(1000) }, { seq: 2, type: 'stream_speed', tps: 20, estimated: true }], {});
    expect(live.speed).toBe(20); expect(live.speedEstimated).toBe(true);
    const complete = applyEvents(live, [{ seq: 3, type: 'generation', tps: 19.8, estimated: false }], {});
    expect(complete.speed).toBe(19.8); expect(complete.speedEstimated).toBe(false);
  });
  it('settles an interrupted tool without pretending its side effect completed', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'run_command', invocation_id: 'a', state: 'running' }, { seq: 2, type: 'outcome_unknown', name: 'run_command', invocation_id: 'a' }, { seq: 3, type: 'done', status: 'paused' }], { finished: true, status: 'paused' });
    expect(live.tools).toHaveLength(1); expect(live.tools[0].state).toBe('Outcome unknown');
    const interrupted = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }], { finished: true, status: 'interrupted' });
    expect(interrupted.tools[0].state).toBe('Interrupted');
  });
});
