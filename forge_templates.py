"""Small runnable offline Builder starters. Dependency installation is explicit."""
import html
import json


STYLE = '''*{box-sizing:border-box}body{margin:0;font:16px system-ui;color:#162f29;background:#eef4ef}main{max-width:920px;margin:64px auto;padding:32px}header small{color:#46705f;text-transform:uppercase;letter-spacing:.12em}h1{font-size:clamp(32px,6vw,60px);margin:12px 0}section{background:white;padding:24px;border-radius:18px;margin-top:24px;box-shadow:0 12px 32px #1836290d}form{display:flex;gap:12px;flex-wrap:wrap}input{font:inherit;padding:12px;flex:1;min-width:180px;border:1px solid #b7cdbd;border-radius:8px}button{font:inherit;background:#245944;color:white;padding:12px 18px;border:0;border-radius:8px;cursor:pointer}li{padding:12px 0;display:flex;align-items:center;justify-content:space-between;gap:16px}button:focus-visible,input:focus-visible{outline:3px solid #d18c30;outline-offset:3px}.error{color:#a12727}@media(max-width:600px){main{margin:24px auto;padding:20px}section{padding:18px}}'''


def template_files(kind, title):
    title = str(title)[:120] or 'My workspace'
    escaped = html.escape(title)
    head = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escaped}</title><link rel="stylesheet" href="style.css"></head>'
    body = f'<body><main><header><small>Your new workspace</small><h1>{escaped}</h1><p>Capture ideas and turn them into useful work.</p></header><section aria-labelledby="items-title"><h2 id="items-title">Ideas</h2><form id="item-form"><label for="item-input">New idea</label><input id="item-input" required maxlength="200" placeholder="What would you like to do?"><button>Add idea</button></form><p class="error" role="alert" id="error"></p><ul id="items" aria-live="polite"></ul></section></main><script type="module" src="app.js"></script></body></html>'
    js = '''const form=document.getElementById('item-form'),input=document.getElementById('item-input'),list=document.getElementById('items'),error=document.getElementById('error');
const row=item=>{const li=document.createElement('li'),text=document.createElement('span'),button=document.createElement('button');text.textContent=item.text;button.textContent='Remove';button.setAttribute('aria-label','Remove '+item.text);button.onclick=()=>remove(item.id);li.append(text,button);return li;};
const render=()=>{list.replaceChildren(...items.map(row));};
'''
    if kind == 'static':
        js += '''let items=JSON.parse(localStorage.getItem('forge-starter-items')||'[]');
const save=()=>{localStorage.setItem('forge-starter-items',JSON.stringify(items));render();};
const remove=id=>{items=items.filter(item=>item.id!==id);save();};
form.onsubmit=event=>{event.preventDefault();const text=input.value.trim();if(!text)return;items.push({id:crypto.randomUUID(),text});input.value='';save();};render();
'''
        return {'index.html': head + body, 'style.css': STYLE, 'app.js': js,
                'README.md': '# ' + title + '\n\nStart the static Builder preview. Ideas are stored in this browser only.\n'}
    if kind == 'fastapi':
        js += '''let items=[];
const request=async(url,options={})=>{const response=await fetch(url,options);if(!response.ok)throw new Error('Request failed ('+response.status+')');return response.status===204?null:response.json();};
const load=async()=>{try{items=await request('/api/items');render();error.textContent='';}catch(e){error.textContent=e.message;}};
const remove=async id=>{try{await request('/api/items/'+id,{method:'DELETE'});await load();}catch(e){error.textContent=e.message;}};
form.onsubmit=async event=>{event.preventDefault();const text=input.value.trim();if(!text)return;try{await request('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});input.value='';await load();}catch(e){error.textContent=e.message;}};load();
'''
        python = '''from contextlib import contextmanager
from pathlib import Path
import sqlite3
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Local ideas workspace")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
class Idea(BaseModel):
    text: str = Field(min_length=1, max_length=200)

@contextmanager
def database():
    connection = sqlite3.connect(ROOT / 'data.sqlite3')
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, text TEXT NOT NULL)')
        yield connection
        connection.commit()
    finally:
        connection.close()

@app.get('/', response_class=HTMLResponse)
def index():
    return (ROOT / 'index.html').read_text(encoding='utf-8')

@app.get('/style.css')
def style():
    return FileResponse(ROOT / 'style.css', media_type='text/css')

@app.get('/app.js')
def script():
    return FileResponse(ROOT / 'app.js', media_type='text/javascript')

@app.get('/api/items')
def items():
    with database() as db:
        return [dict(row) for row in db.execute('SELECT id,text FROM items ORDER BY id')]

@app.post('/api/items', status_code=201)
def add(idea: Idea):
    text = idea.text.strip()
    if not text:
        raise HTTPException(422, 'Enter an idea.')
    with database() as db:
        cursor = db.execute('INSERT INTO items(text) VALUES(?)', (text,))
        return {'id': cursor.lastrowid, 'text': text}

@app.delete('/api/items/{identifier}', status_code=204)
def remove(identifier: int):
    with database() as db:
        if not db.execute('DELETE FROM items WHERE id=?', (identifier,)).rowcount:
            raise HTTPException(404, 'Idea not found.')
    return Response(status_code=204)
'''
        return {'index.html': head + body, 'style.css': STYLE, 'app.js': js, 'app.py': python,
                'requirements.txt': 'fastapi==0.141.1\nuvicorn==0.52.4\n', '.gitignore': '.venv/\n__pycache__/\ndata.sqlite3\n',
                'README.md': '# ' + title + '\n\nCreate a Python virtual environment, install requirements.txt explicitly, then start the FastAPI Builder preview. Data stays in data.sqlite3.\n'}
    if kind == 'vite':
        source = '''import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
interface Idea {id: string; text: string;}
function App(){const [items,setItems]=useState<Idea[]>([]),[text,setText]=useState('');return <main><header><small>Your new workspace</small><h1>__TITLE__</h1><p>Capture ideas and turn them into useful work.</p></header><section><h2>Ideas</h2><form onSubmit={event=>{event.preventDefault();if(!text.trim())return;setItems([...items,{id:crypto.randomUUID(),text:text.trim()}]);setText('');}}><label htmlFor="idea">New idea</label><input id="idea" required maxLength={200} value={text} onChange={event=>setText(event.target.value)}/><button>Add idea</button></form><ul aria-live="polite">{items.map(item=><li key={item.id}><span>{item.text}</span><button aria-label={'Remove '+item.text} onClick={()=>setItems(items.filter(other=>other.id!==item.id))}>Remove</button></li>)}</ul></section></main>;}
const root=document.getElementById('root');if(!root)throw new Error('App root is missing.');createRoot(root).render(<App/>);
'''.replace('__TITLE__', '{' + json.dumps(title) + '}')
        return {'index.html': head.replace('<link rel="stylesheet" href="style.css">', '') + '<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>',
                'src/main.tsx': source, 'src/style.css': STYLE,
                'src/vite-env.d.ts': '/// <reference types="vite/client" />\n',
                'tsconfig.json': json.dumps({'compilerOptions': {'target': 'ES2022', 'lib': ['ES2022', 'DOM', 'DOM.Iterable'],
                    'module': 'ESNext', 'moduleResolution': 'Bundler', 'jsx': 'react-jsx', 'strict': True,
                    'noEmit': True, 'skipLibCheck': True, 'allowSyntheticDefaultImports': True}, 'include': ['src']}, indent=2),
                'package.json': json.dumps({'name': 'forge-builder-app', 'version': '1.0.0', 'private': True,
                    'type': 'module', 'scripts': {'dev': 'vite --host 127.0.0.1', 'build': 'tsc --noEmit && vite build'},
                    'dependencies': {'react': '19.3.0', 'react-dom': '19.3.0'}, 'devDependencies': {'vite': '8.3.2',
                        'typescript': '7.0.2', '@types/react': '19.3.0', '@types/react-dom': '19.3.0'}}, indent=2),
                '.gitignore': 'node_modules/\ndist/\n',
                'README.md': '# ' + title + '\n\nRun npm install explicitly, then start the Vite Builder preview. npm run build creates dist/.\n'}
    raise ValueError('Choose static, vite or fastapi template.')
