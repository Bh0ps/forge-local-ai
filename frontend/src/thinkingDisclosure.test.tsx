import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageView } from './components';
import { ThinkingDisclosure } from './thinkingDisclosure';

function geometry(element: HTMLElement, height = 600, visible = 120) {
  Object.defineProperty(element, 'scrollHeight', { configurable: true, value: height });
  Object.defineProperty(element, 'clientHeight', { configurable: true, value: visible });
}

function view(text: string, streaming = true, content = '') {
  return <MessageView message={{ id: 'thought', role: 'assistant', content, thinking: text }} streaming={streaming} />;
}

afterEach(() => vi.unstubAllGlobals());

describe('reasoning follows the reader', () => {
  it('opens live reasoning at the latest text and keeps following appended text', () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(600);
    const rendered = render(view('Checking the synthetic fixture.'));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    expect(region.closest('details')?.open).toBe(true);
    expect(region.scrollTop).toBe(600);
    geometry(region, 900);
    rendered.rerender(view('Checking the synthetic fixture.\nReading its next section.'));
    expect(region.scrollTop).toBe(900);
    expect(screen.queryByRole('button', { name: 'Jump to latest reasoning' })).toBeNull();
  });

  it('preserves an earlier reading position as text arrives and lets the reader resume following', async () => {
    const rendered = render(view('First thought.'));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    region.scrollTop = 90;
    fireEvent.scroll(region);
    geometry(region, 900);
    rendered.rerender(view('First thought.\nSecond thought.'));
    expect(region.scrollTop).toBe(90);
    await userEvent.click(screen.getByRole('button', { name: 'Jump to latest reasoning' }));
    expect(region.scrollTop).toBe(900);
    expect(document.activeElement).toBe(region);
    expect(screen.queryByRole('button', { name: 'Jump to latest reasoning' })).toBeNull();
    geometry(region, 1000);
    rendered.rerender(view('First thought.\nSecond thought.\nFinal check.'));
    expect(region.scrollTop).toBe(1000);
  });

  it('resumes following when the reader scrolls back to the bottom', () => {
    const rendered = render(view('First thought.'));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    region.scrollTop = 0;
    fireEvent.scroll(region);
    expect(screen.getByRole('button', { name: 'Jump to latest reasoning' })).toBeTruthy();
    region.scrollTop = 480;
    fireEvent.scroll(region);
    expect(screen.queryByRole('button', { name: 'Jump to latest reasoning' })).toBeNull();
    geometry(region, 800);
    rendered.rerender(view('First thought.\nAnother thought.'));
    expect(region.scrollTop).toBe(800);
  });

  it('does not let the initial deferred native toggle override a reader who already scrolled up', async () => {
    render(view('First thought.'));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    region.scrollTop = 30;
    fireEvent.scroll(region);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(region.scrollTop).toBe(30);
    expect(screen.getByRole('button', { name: 'Jump to latest reasoning' })).toBeTruthy();
  });

  it('keeps a live disclosure manually collapsed until it is explicitly expanded again', async () => {
    const rendered = render(view('First thought.'));
    const details = document.querySelector('details')!;
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    await userEvent.click(screen.getByText('Thinking'));
    await waitFor(() => expect(details.open).toBe(false));
    rendered.rerender(view('First thought.\nNew thought.'));
    expect(details.open).toBe(false);
    region.scrollTop = 0;
    geometry(region, 800);
    await userEvent.click(screen.getByText('Thinking'));
    await waitFor(() => expect(details.open).toBe(true));
    expect(region.scrollTop).toBe(800);
  });

  it('keeps a paused reading position and open disclosure when the answer starts and finishes', () => {
    const rendered = render(view('First thought.'));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    region.scrollTop = 70;
    fireEvent.scroll(region);
    rendered.rerender(view('First thought.\nFinal thought.', true, 'The answer is ready.'));
    expect(screen.getByText('Model reasoning')).toBeTruthy();
    expect(region.closest('details')?.classList.contains('thinking-active')).toBe(false);
    expect(region.scrollTop).toBe(70);
    rendered.rerender(view('First thought.\nFinal thought.', false, 'The answer is ready.'));
    expect(region.closest('details')?.open).toBe(true);
    expect(region.scrollTop).toBe(70);
    expect(screen.getByRole('button', { name: 'Jump to latest reasoning' })).toBeTruthy();
  });

  it('opens saved reasoning from the beginning and keeps it separate from a different message', async () => {
    const rendered = render(view('A saved thought.', false, 'Saved answer.'));
    const details = document.querySelector('details')!;
    expect(details.open).toBe(false);
    await userEvent.click(screen.getByText('Model reasoning'));
    await waitFor(() => expect(details.open).toBe(true));
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    expect(region.scrollTop).toBe(0);
    region.scrollTop = 120;
    fireEvent.scroll(region);
    rendered.rerender(<MessageView message={{ id: 'different', role: 'assistant', content: 'Other answer.', thinking: 'Other saved thought.' }} />);
    expect(document.querySelector('details')?.open).toBe(false);
    await userEvent.click(screen.getByText('Model reasoning'));
    expect(screen.getByRole('region', { name: 'Model reasoning' }).scrollTop).toBe(0);
  });

  it('resets a replaced live thought without carrying the previous round’s scroll pause', async () => {
    const rendered = render(<ThinkingDisclosure text="First round." streaming />);
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    region.scrollTop = 80;
    fireEvent.scroll(region);
    await userEvent.click(screen.getByText('Thinking'));
    await waitFor(() => expect(region.closest('details')?.open).toBe(false));
    geometry(region, 700);
    rendered.rerender(<ThinkingDisclosure text="Next round." streaming />);
    await waitFor(() => expect(region.closest('details')?.open).toBe(true));
    expect(region.scrollTop).toBe(700);
    expect(screen.queryByRole('button', { name: 'Jump to latest reasoning' })).toBeNull();
  });

  it('follows resize only while following and disconnects observers on unmount, including StrictMode', () => {
    const observers: { callback: ResizeObserverCallback; disconnect: ReturnType<typeof vi.fn> }[] = [];
    class Observer {
      disconnect = vi.fn();
      observe = vi.fn();
      unobserve = vi.fn();
      constructor(callback: ResizeObserverCallback) { observers.push({ callback, disconnect: this.disconnect }); }
    }
    vi.stubGlobal('ResizeObserver', Observer);
    const rendered = render(<StrictMode>{view('A live thought.')}</StrictMode>);
    const region = screen.getByRole('region', { name: 'Model reasoning' });
    geometry(region);
    act(() => observers.at(-1)!.callback([], {} as ResizeObserver));
    expect(region.scrollTop).toBe(600);
    region.scrollTop = 30;
    fireEvent.scroll(region);
    geometry(region, 800);
    act(() => observers.at(-1)!.callback([], {} as ResizeObserver));
    expect(region.scrollTop).toBe(30);
    rendered.unmount();
    expect(observers.every(observer => observer.disconnect.mock.calls.length === 1)).toBe(true);
  });

  it('respects the reasoning display setting without exposing a hidden reasoning region', () => {
    render(<MessageView message={{ role: 'assistant', content: 'Synthetic answer.', thinking: 'Hidden synthetic reasoning.' }} streaming showThinking={false} />);
    expect(screen.queryByText('Hidden synthetic reasoning.')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Model reasoning' })).toBeNull();
  });
});
