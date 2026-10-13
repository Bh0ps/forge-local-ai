import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Modal } from '../components';

function Harness() {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>Configure connection</button>{open && <Modal title="Connection details" close={() => setOpen(false)}><input aria-label="Endpoint" autoFocus /></Modal>}</>;
}
describe('dialog accessibility', () => {
  it('names the dialog by its visible heading and restores focus after native cancellation', async () => {
    render(<Harness />);
    const trigger = screen.getByRole('button', { name: 'Configure connection' });
    await userEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Connection details' });
    const title = screen.getByRole('heading', { name: 'Connection details' });
    expect(dialog.getAttribute('aria-labelledby')).toBe(title.id);
    // jsdom's showModal stub does not implement the browser's focus step.
    screen.getByLabelText('Endpoint').focus();
    fireEvent(dialog, new Event('cancel', { bubbles: true }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('gives concurrently mounted dialogs their own title identity', () => {
    render(<><Modal title="First connection" close={() => {}}><p>First</p></Modal><Modal title="Second connection" close={() => {}}><p>Second</p></Modal></>);
    const first = screen.getByRole('dialog', { name: 'First connection' });
    const second = screen.getByRole('dialog', { name: 'Second connection' });
    expect(first.getAttribute('aria-labelledby')).not.toBe(second.getAttribute('aria-labelledby'));
  });
});
