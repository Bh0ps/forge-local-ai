import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { TaskContextPanel } from './taskContextPanel';
import type { LibrarySkill } from './pluginsPage';

const cpp: LibrarySkill = {id: 'native-cpp', library_id: 'saved-cpp', name: 'Windows C++', description: 'Use CMake presets and verify native Windows builds.', enabled: true, automatic: true, bundle: 'build', category: 'Coding', tags: ['cpp', 'cmake']};
const dotnet: LibrarySkill = {id: 'windows-dotnet', name: 'Windows .NET', description: 'Build and test a .NET solution.', enabled: true, automatic: true, bundle: 'build', category: 'Coding'};
function bridge(skills: LibrarySkill[]) {
  const call = vi.fn(async (action: string) => {
    if (action !== 'skills') throw new Error(`Unexpected eager resource request: ${action}`);
    return {skills};
  });
  window.pywebview = {api: {call}};
  return call;
}
function panel(overrides: Partial<React.ComponentProps<typeof TaskContextPanel>> = {}) {
  const onSkills = vi.fn(), onNotes = vi.fn();
  return {onSkills, onNotes, ...render(<TaskContextPanel projectId="project" skills={[]} notes={[]} spaces={[]} onSkills={onSkills} onNotes={onNotes} {...overrides}/>)};
}

describe('compact task context bundles', () => {
  it('groups C++ and .NET under the declared Coding bundle and loads only descriptions on expansion', async () => {
    const call = bridge([cpp, dotnet]);
    const {onSkills} = panel();
    const bundle = await screen.findByRole('button', {name: 'Coding skills'});
    expect(bundle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('checkbox', {name: 'Windows C++'})).toBeNull();
    bundle.focus(); await userEvent.keyboard('{Enter}');
    expect(bundle.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(bundle);
    expect(screen.getByRole('checkbox', {name: 'Windows .NET'})).toBeTruthy();
    expect(screen.queryByText(cpp.description!)).toBeNull();
    await userEvent.click(screen.getByRole('button', {name: 'About Windows C++'}));
    expect(screen.getByText(cpp.description!)).toBeTruthy();
    expect(screen.queryByText(dotnet.description!)).toBeNull();
    await userEvent.click(screen.getByRole('checkbox', {name: 'Windows C++'}));
    expect(onSkills).toHaveBeenCalledWith(['native-cpp']);
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith('skills', {project_id: 'project'});
  });

  it('searches names, descriptions, metadata and tags while revealing matching collapsed bundles', async () => {
    bridge([cpp, dotnet, {...cpp, id: 'custom', library_id: undefined, name: 'Custom release', bundle: undefined, category: 'delivery', tags: ['portable'], description: 'Publish a checked build.'}]);
    panel();
    await screen.findByRole('button', {name: 'Coding skills'});
    fireEvent.change(screen.getByRole('textbox', {name: 'Search skills and notes'}), {target: {value: 'coding cmake'}});
    expect(screen.getByRole('checkbox', {name: 'Windows C++'})).toBeTruthy();
    expect(screen.queryByRole('checkbox', {name: 'Windows .NET'})).toBeNull();
    fireEvent.change(screen.getByRole('textbox', {name: 'Search skills and notes'}), {target: {value: 'portable'}});
    expect(screen.getByRole('checkbox', {name: 'Custom release'})).toBeTruthy();
    expect(screen.queryByRole('checkbox', {name: 'Windows C++'})).toBeNull();
  });

  it('puts saved aliases first without rewriting IDs or duplicating catalog choices', async () => {
    bridge([cpp, dotnet]);
    const {onSkills} = panel({skills: ['saved-cpp', 'missing-skill']});
    const attached = await screen.findByRole('region', {name: 'Attached skills and notes'});
    await within(attached).findByRole('checkbox', {name: 'Windows C++'});
    expect(screen.getAllByRole('checkbox', {name: 'Windows C++'})).toHaveLength(1);
    expect(within(attached).getByRole('checkbox', {name: 'Unavailable skill'})).toHaveProperty('checked', true);
    expect(onSkills).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('checkbox', {name: 'Windows C++'}));
    expect(onSkills).toHaveBeenCalledWith(['missing-skill']);
    await userEvent.click(screen.getByRole('button', {name: 'Remove Unavailable skill'}));
    expect(onSkills).toHaveBeenLastCalledWith(['saved-cpp']);
  });

  it('keeps disabled selections removable and custom or imported skills discoverable', async () => {
    bridge([{...cpp, enabled: false}, {...dotnet, id: 'custom', bundle: undefined, category: undefined, name: 'Imported skill', builtin: false}]);
    const {onSkills} = panel({skills: ['saved-cpp']});
    const disabled = await screen.findByRole('checkbox', {name: 'Windows C++'});
    expect(disabled).toHaveProperty('disabled', true);
    expect(screen.getByText('Disabled in Library')).toBeTruthy();
    expect(screen.getByRole('checkbox', {name: 'Imported skill'})).toHaveProperty('disabled', false);
    await userEvent.click(screen.getByRole('button', {name: 'Remove Windows C++'}));
    expect(onSkills).toHaveBeenCalledWith([]);
  });

  it('shows canonical and library aliases once and removes all saved aliases only on explicit removal', async () => {
    bridge([cpp, dotnet]);
    const {onSkills} = panel({skills: ['saved-cpp', 'native-cpp', 'missing-skill']});
    const attached = await screen.findByRole('region', {name: 'Attached skills and notes'});
    await within(attached).findByRole('checkbox', {name: 'Windows C++'});
    expect(within(attached).getAllByRole('checkbox', {name: 'Windows C++'})).toHaveLength(1);
    expect(screen.getByText('2 attached')).toBeTruthy();
    expect(onSkills).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', {name: 'Remove Windows C++'}));
    expect(onSkills).toHaveBeenCalledWith(['missing-skill']);
  });

  it('retains missing and out-of-scope notes, enforces the unique-note limit, and hides unrelated choices', async () => {
    bridge([]);
    const {onNotes} = panel({notes: ['one', 'two', 'three', 'four', 'missing'], spaces: [
      ...['one', 'two', 'three'].map(id => ({id, name: id})),
      {id: 'four', name: 'Saved project note', project_ids: ['other']},
      {id: 'extra', name: 'Another note', description: 'Reference fixture.'},
      {id: 'foreign', name: 'Unrelated note', project_ids: ['other']},
    ]});
    expect(screen.getByRole('checkbox', {name: 'Another note'})).toHaveProperty('disabled', true);
    expect(screen.getByText('Outside this project')).toBeTruthy();
    expect(screen.queryByText('Unrelated note')).toBeNull();
    expect(screen.getByRole('checkbox', {name: 'Unavailable note'})).toHaveProperty('checked', true);
    expect(onNotes).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', {name: 'Remove Saved project note'}));
    expect(onNotes).toHaveBeenCalledWith(['one', 'two', 'three', 'missing']);
  });

  it('ignores a delayed former-project catalog without discarding selections', async () => {
    let resolveOld!: (value: unknown) => void;
    const call = vi.fn(async (_action: string, data: Record<string, unknown>) => data.project_id === 'old' ? new Promise(resolve => {resolveOld = resolve;}) : {skills: [dotnet]});
    window.pywebview = {api: {call}};
    const onSkills = vi.fn(), onNotes = vi.fn();
    const view = render(<TaskContextPanel projectId="old" skills={['saved-cpp']} notes={[]} spaces={[]} onSkills={onSkills} onNotes={onNotes}/>);
    await waitFor(() => expect(resolveOld).toBeTypeOf('function'));
    view.rerender(<TaskContextPanel projectId="new" skills={['saved-cpp']} notes={[]} spaces={[]} onSkills={onSkills} onNotes={onNotes}/>);
    await screen.findByRole('button', {name: 'Coding skills'});
    await act(async () => resolveOld({skills: [cpp]}));
    expect(screen.queryByRole('checkbox', {name: 'Windows C++'})).toBeNull();
    expect(screen.getByRole('checkbox', {name: 'Unavailable skill'})).toHaveProperty('checked', true);
    expect(onSkills).not.toHaveBeenCalled();
  });

  it('preserves keyboard focus as a checked skill moves into attached items and restores search after removal', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    bridge([cpp, dotnet]);
    function Harness() {
      const [skills, setSkills] = useState<string[]>([]);
      return <TaskContextPanel projectId="project" skills={skills} notes={[]} spaces={[]} onSkills={setSkills} onNotes={() => {}}/>;
    }
    render(<Harness/>);
    await userEvent.click(await screen.findByRole('button', {name: 'Coding skills'}));
    const checkbox = screen.getByRole('checkbox', {name: 'Windows C++'});
    checkbox.focus(); await userEvent.keyboard(' ');
    const attached = screen.getByRole('region', {name: 'Attached skills and notes'});
    expect(document.activeElement).toBe(within(attached).getByRole('checkbox', {name: 'Windows C++'}));
    await userEvent.click(screen.getByRole('button', {name: 'Remove Windows C++'}));
    expect(document.activeElement).toBe(screen.getByRole('textbox', {name: 'Search skills and notes'}));
  });

  it('does not take focus back after the user moves elsewhere during selection', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    bridge([cpp, dotnet]);
    function Harness() {
      const [skills, setSkills] = useState<string[]>([]);
      return <><button id="elsewhere">Another action</button><TaskContextPanel projectId="project" skills={skills} notes={[]} spaces={[]} onSkills={ids => {document.getElementById('elsewhere')?.focus();setSkills(ids);}} onNotes={() => {}}/></>;
    }
    render(<Harness/>);
    await userEvent.click(await screen.findByRole('button', {name: 'Coding skills'}));
    const checkbox = screen.getByRole('checkbox', {name: 'Windows C++'});
    checkbox.focus(); await userEvent.keyboard(' ');
    expect(document.activeElement).toBe(screen.getByRole('button', {name: 'Another action'}));
  });
});
