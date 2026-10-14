"""Tauri guidance is bounded routing and lazy data, never tool authority."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import jsonschema
import pytest

import forge_library
from forge_skill_catalog import CONTENT_FIELDS, LIBRARY_VERSION, STARTER_SKILLS, WORKFLOWS, markdown
from prompt_compiler import compile_skill_guidance, static_context_allowances
from scripts.generate_starter_skills import rendered_assets
from test_forge_windows_workflows import hub


BY_ID = {item['id']:item for item in WORKFLOWS}
GUIDE = BY_ID['desktop-executable']
TAURI = next(case for case in GUIDE['examples'] if 'Tauri' in case['title'])


@pytest.mark.parametrize('query,phase', [
    ('Build this Tauri app','implement'), ('Build a Tauri Windows EXE','implement'),
    ('Package this Tauri app for Windows','implement'), ('Fix a Tauri build error','implement'),
    ('Verify the Tauri Windows installer','verify'), ('Discover the Tauri build prerequisites','discover'),
])
def test_build_requests_select_existing_desktop_recipe_without_tool_grants(hub, query, phase):
    result=hub.route_preview({'query':query,'phase':phase,'available_tools':[]})
    assert 'desktop-executable' in {item['library_id'] for item in result['selected']}
    assert len(result['selected'])<=3
    assert all(not item.get('requires_tools') for item in result['selected'])
    assert len(hub.discover_skills())==28


@pytest.mark.parametrize('query', [
    'Build a plain Rust CLI','Build a React website','Read src/tauri.rs',
    'Research Tauri architecture','Build a web app without Tauri','Plan this Tauri app',
    'Read a Cargo.toml','Build a Cargo library',
])
def test_plain_rust_web_readonly_and_negated_requests_do_not_select_tauri_packaging(hub, query):
    result=hub.route_preview({'query':query,'phase':'implement','available_tools':[]})
    assert 'desktop-executable' not in {item['library_id'] for item in result['selected']}
    assert 'tauri' not in forge_library.task_context(query)['intents']


@pytest.mark.parametrize('query,identity', [('Build a C# WPF app','windows-dotnet'),
    ('Build native C++ with CMake','windows-native-cpp')])
def test_specific_existing_windows_guidance_retains_precedence(hub, query, identity):
    result=hub.route_preview({'query':query,'phase':'implement','available_tools':[]})
    assert result['selected'][0]['library_id']==identity
    assert 'desktop-executable' not in {item['library_id'] for item in result['selected']}


@pytest.mark.parametrize('preference', [{'enabled':False}, {'automatic':False}])
def test_tauri_word_does_not_override_disabled_or_nonautomatic_owner(hub, preference):
    skill=next(item for item in hub.discover_skills() if item['library_id']=='desktop-executable')
    hub.dispatch('skill_toggle', {'id':skill['id'],**preference})
    selected=hub.route_preview({'query':'Build this Tauri app','phase':'implement'})['selected']
    assert skill['id'] not in {item['id'] for item in selected}
    explicit=hub.route_preview({'query':'Hello','explicit':[skill['id']]})['selected']
    assert (skill['id'] in {item['id'] for item in explicit}) is preference.get('enabled',True)


def test_exact_canonical_metadata_keeps_long_tauri_example_lazy(hub):
    assert LIBRARY_VERSION=='2.4.1' and GUIDE['version']=='2.2.1' and len(WORKFLOWS)==28
    assert BY_ID['windows-dotnet']['version']==BY_ID['windows-native-cpp']['version']=='2.2.0'
    assert all(not CONTENT_FIELDS.intersection(row) for row in STARTER_SKILLS)
    skill=next(item for item in hub.discover_skills() if item['library_id']=='desktop-executable')
    primary=hub.read_skill(skill['id'])
    assert primary['text']==markdown(GUIDE) and not primary['truncated']
    assert TAURI['scenario'] not in primary['text']
    support=hub.read_skill_resource(skill['id'],'references/examples.md')
    assert support['ok'] and TAURI['scenario'] in support['text']
    assert 'https://v2.tauri.app/reference/cli/' in support['text']
    folder=forge_library.ASSET_ROOT/'skills'/'desktop-executable'
    for name,expected in rendered_assets(GUIDE).items():
        assert (folder/name).read_bytes()==expected.encode('utf-8')


def test_real_schema_calls_use_managed_build_sessions_and_no_setup_side_effects():
    from forge_runs import WORKFLOW
    from project_tools import ProjectTools
    schemas={row['function']['name']:row['function']['parameters'] for row in ProjectTools.schemas()+WORKFLOW}
    assert len(TAURI['calls'])==6
    for call in TAURI['calls']:
        jsonschema.validate(call['arguments'],schemas[call['tool']])
    assert [call['tool'] for call in TAURI['calls']]==[
        'read_files','run_command','run_command','command_start','command_wait','command_read']
    start=TAURI['calls'][3]['arguments']
    assert start['argv'][1:]==['tauri','build'] and start['max_seconds']==600
    assert '<' in start['argv'][0] and '<' in start['cwd']  # Resolve real repo outputs before execution.
    assert set(GUIDE['recommended_tools'])<=schemas.keys()


def test_complete_primary_and_supports_keep_existing_8k_budget_and_metadata_bound():
    selected=[{'skill':{'id':str(index).zfill(20),'name':item['name']},'text':markdown(item)}
        for index,item in enumerate([GUIDE,BY_ID['project-discovery'],BY_ID['testing']])]
    budget=static_context_allowances({'context':8192,'tokens':2048})['skills_bytes']
    compiled,active=compile_skill_guidance(selected,budget)
    assert selected[0]['text'] in compiled and active==[item['skill']['id'] for item in selected]
    assert len(compiled.encode('utf-8'))<=budget and len(markdown(GUIDE).split())<300
    query='Build Tauri ' + 'x'*24000 + ' unrelated suffix'
    assert len(forge_library.select_skills([{**row,'enabled':True,'automatic':True,'builtin':True}
        for row in STARTER_SKILLS],query=query,context={'phase':'implement'}))<=3


@pytest.mark.parametrize('ownership', ['pristine','edited_primary','edited_resource','deleted_primary','deleted_resource'])
def test_content_only_28_skill_upgrade_preserves_owned_edits_deletions_and_flags(tmp_path, monkeypatch, ownership):
    current_assets=forge_library.ASSET_ROOT
    old_assets=tmp_path/'prior-bundle';shutil.copytree(current_assets,old_assets)
    old_folder=old_assets/'skills'/'desktop-executable'
    # A synthetic pristine prior version isolates actual reconciliation behavior
    # without pretending this text is a shipped old release artifact.
    (old_folder/'SKILL.md').write_text('---\nname: desktop-executable\nlicense: MIT\n---\n\nPrevious package guide.\n',encoding='utf-8')
    (old_folder/'references'/'examples.md').write_text('Previous lazy package example.\n',encoding='utf-8')
    old_rows=deepcopy(STARTER_SKILLS)
    for row in old_rows:
        if row['id']=='desktop-executable':row['version']='2.2.0';row['intents'].remove('tauri')
    root=tmp_path/'profile';config={}
    monkeypatch.setattr(forge_library,'ASSET_ROOT',old_assets)
    monkeypatch.setattr(forge_library,'STARTER_SKILLS',old_rows)
    monkeypatch.setattr(forge_library,'LIBRARY_VERSION','2.4.0')
    assert forge_library.reconcile_starters(root,config)==(True,[])
    folder=root/'skills'/'forge-starter'/'desktop-executable'
    primary=folder/'SKILL.md';resource=folder/'references'/'examples.md'
    identity=forge_library.skill_identity(primary)
    config['skills'][identity]={'enabled':False,'automatic':False}
    if ownership=='edited_primary':primary.write_text(primary.read_text()+'Local build policy.\n',encoding='utf-8')
    elif ownership=='edited_resource':resource.write_text('My private build examples.\n',encoding='utf-8')
    elif ownership=='deleted_primary':primary.unlink()
    elif ownership=='deleted_resource':resource.unlink()
    retained_primary=primary.read_bytes() if primary.exists() else None
    retained_resource=resource.read_bytes() if resource.exists() else None
    config=json.loads(json.dumps(config))
    monkeypatch.setattr(forge_library,'ASSET_ROOT',current_assets)
    monkeypatch.setattr(forge_library,'STARTER_SKILLS',STARTER_SKILLS)
    monkeypatch.setattr(forge_library,'LIBRARY_VERSION',LIBRARY_VERSION)
    assert forge_library.reconcile_starters(root,config)==(True,[])
    assert len(config['library']['files'])==28 and config['library']['version']=='2.4.1'
    assert config['skills'][identity]=={'enabled':False,'automatic':False}
    assert forge_library.skill_identity(primary)==identity
    if ownership in ('edited_primary','deleted_primary'):
        assert (primary.read_bytes() if primary.exists() else None)==retained_primary
    else:assert primary.read_bytes()==(current_assets/'skills'/'desktop-executable'/'SKILL.md').read_bytes()
    if ownership in ('edited_resource','deleted_resource','deleted_primary'):
        assert (resource.read_bytes() if resource.exists() else None)==retained_resource
    else:assert resource.read_bytes()==(current_assets/'skills'/'desktop-executable'/'references'/'examples.md').read_bytes()
    assert forge_library.reconcile_starters(root,config)==(False,[])
