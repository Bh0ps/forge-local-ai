"""Additive cosmetic preference, preserving the legacy layout row and scope."""
import json

import pytest

from forge_store import ForgeStore
from forge_workspace_ui import DEFAULT_LAYOUT, WorkspaceUI


@pytest.fixture
def ui(tmp_path):
    return WorkspaceUI(ForgeStore(tmp_path / 'synthetic-workspace'))


def records(ui):
    with ui.store._connection(transaction='read') as db:
        return [tuple(row) for row in db.execute("SELECT kind,id,data FROM entities WHERE kind IN ('ui_layout','ui_layout_views') ORDER BY kind,id")]


def test_desktop_choice_survives_restart_without_extending_legacy_layout(ui):
    saved = ui.layout({'client_id': 'desktop', 'layout': {'sidebar_width': 310,
        'right_panel_width': 440, 'right_panel_tab': 'browser', 'browser_mode': 'desktop'}}, save=True)
    assert saved['browser_mode'] == 'desktop'
    rows = records(ui)
    legacy = json.loads(next(row[2] for row in rows if row[0] == 'ui_layout'))['layout']
    assert set(legacy) == set(DEFAULT_LAYOUT)
    assert legacy['sidebar_width'] == 310 and legacy['right_panel_width'] == 440
    assert legacy['right_panel_tab'] == 'browser'
    assert WorkspaceUI(ForgeStore(ui.store.home)).layout({'client_id': 'desktop'}) == saved


def test_older_partial_save_keeps_the_separate_mode(ui):
    ui.layout({'client_id': 'desktop', 'layout': {'browser_mode': 'desktop', 'sidebar_width': 310}}, save=True)
    saved = ui.layout({'client_id': 'desktop', 'layout': {'right_panel_width': 470}}, save=True)
    assert saved['browser_mode'] == 'desktop' and saved['sidebar_width'] == 310
    assert saved['right_panel_width'] == 470


def test_browser_client_preferences_never_replace_native_or_other_client(ui):
    ui.layout({'client_id': 'desktop', 'layout': {'browser_mode': 'desktop'}}, save=True)
    ui.layout({'client_id': 'browser-a', 'layout': {'browser_mode': 'desktop', 'sidebar_open': False}}, save=True)
    ui.layout({'client_id': 'browser-a', 'layout': {'browser_mode': 'browser'}}, save=True)
    assert ui.layout({'client_id': 'desktop'})['browser_mode'] == 'desktop'
    assert ui.layout({'client_id': 'browser-a'})['browser_mode'] == 'browser'
    assert ui.layout({'client_id': 'browser-b'}) == {**DEFAULT_LAYOUT, 'browser_mode': 'browser'}


@pytest.mark.parametrize('value', [None, False, 1, 'other', [], {}])
def test_invalid_mode_cannot_partially_write_either_record(ui, value):
    ui.layout({'client_id': 'desktop', 'layout': {'browser_mode': 'desktop', 'sidebar_width': 310}}, save=True)
    before = records(ui)
    with pytest.raises(ValueError):
        ui.layout({'client_id': 'desktop', 'layout': {'browser_mode': value, 'sidebar_width': 330}}, save=True)
    assert records(ui) == before


@pytest.mark.parametrize('layout', [{'browser_mode': 'desktop', 'sidebar_width': True},
    {'browser_mode': 'desktop', 'unknown': True}, {'browser_mode': 'desktop', 'collapsed_project_ids': [None]}])
def test_invalid_legacy_delta_cannot_save_the_new_preference(ui, layout):
    before = records(ui)
    with pytest.raises(ValueError):
        ui.layout({'client_id': 'desktop', 'layout': layout}, save=True)
    assert records(ui) == before


def test_legacy_rows_default_to_browser_without_rewriting_saved_preferences(ui):
    ui.store.save_entity('ui_layout', {'id': 'desktop', 'layout': {**DEFAULT_LAYOUT,
        'sidebar_width': 330, 'right_panel_tab': 'files'}})
    before = records(ui)
    result = ui.layout({'client_id': 'desktop'})
    assert result['browser_mode'] == 'browser' and result['sidebar_width'] == 330
    assert result['right_panel_tab'] == 'files' and records(ui) == before


@pytest.mark.parametrize('saved', [{'id': 'desktop', 'browser_mode': 'unsupported'},
    {'id': 'other-client', 'browser_mode': 'desktop'}, ['desktop']])
def test_corrupt_view_record_does_not_reset_legacy_widths(ui, saved):
    ui.layout({'client_id': 'desktop', 'layout': {'sidebar_width': 330, 'right_panel_tab': 'files'}}, save=True)
    with ui.store._connection(transaction='write') as db:
        db.execute("INSERT INTO entities(kind,id,data) VALUES('ui_layout_views','desktop',?)", (json.dumps(saved),))
    result = ui.layout({'client_id': 'desktop'})
    assert result['browser_mode'] == 'browser' and result['sidebar_width'] == 330
    assert result['right_panel_tab'] == 'files'
