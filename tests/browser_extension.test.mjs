import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../browser-extension/background.js',import.meta.url),'utf8');
const extensionId = 'a'.repeat(32);
const fixtureTab = {id:10,title:'Disposable form',url:'https://example.test/form',active:true,windowId:1};
const event = () => ({listeners:[],addListener(fn){this.listeners.push(fn);},async emit(...args){await Promise.all(this.listeners.map(fn=>fn(...args)));}});
const flush = () => new Promise(resolve=>setImmediate(resolve));
async function worker(saved = []) {
  const ports=[], badges=new Map(), timers=new Map(), gets=[], scripts=[];
  const tabs=new Map([[10,{...fixtureTab}],[11,{...fixtureTab,id:11,title:'Never selected'}]]);
  let timerId=0;
  const session={connectedTabs:structuredClone(saved)};
  const chrome={
    runtime:{id:extensionId,getURL:path=>`chrome-extension://${extensionId}/${path}`,onMessage:event(),lastError:null,
      connectNative(name){
        assert.equal(name,'org.forge.browser');
        const port={onMessage:event(),onDisconnect:event(),messages:[],disconnected:false,
          postMessage(value){this.messages.push(structuredClone(value));},disconnect(){this.disconnected=true;}};
        ports.push(port);return port;
      }},
    storage:{session:{async get(defaults){return {...defaults,...structuredClone(session)};},async set(value){Object.assign(session,structuredClone(value));}}},
    action:{async setBadgeText({tabId,text}){badges.set(tabId,text);},async setBadgeBackgroundColor(){},async setTitle(){}},
    tabs:{onRemoved:event(),onUpdated:event(),async get(id){gets.push(id);if(!tabs.has(id))throw Error('Closed tab');return {...tabs.get(id)};},
      async query(){throw Error('Background must not enumerate tabs');},async update(id,value){Object.assign(tabs.get(id),value);},async captureVisibleTab(){return 'data:image/png;base64,ZmFrZQ==';}},
    scripting:{async executeScript(value){scripts.push(value);return [{result:{ok:true}}];}}
  };
  const context=vm.createContext({chrome,URL,Map,Promise,setInterval:fn=>{const id=++timerId;timers.set(id,{fn,interval:true});return id;},
    clearInterval:id=>timers.delete(id),setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source,context);await vm.runInContext('ready',context);await flush();
  const request=(action,tab_id=10,sender={id:extensionId,url:chrome.runtime.getURL('popup.html')})=>new Promise(resolve=>{
    const accepted=chrome.runtime.onMessage.listeners[0]({action,tab_id},sender,resolve);
    if(!accepted)resolve(undefined);
  });
  const lost=async detail=>{chrome.runtime.lastError={message:detail};await ports.at(-1).onDisconnect.emit();chrome.runtime.lastError=null;await flush();};
  return {context,chrome,ports,badges,timers,tabs,session,gets,scripts,request,lost,
    run:expression=>vm.runInContext(expression,context)};
}

test('only explicit popup selection connects; ON requires a live Forge acknowledgement',async()=>{
  const w=await worker();assert.equal(w.ports.length,0);
  assert.equal(await w.request('connect',10,{id:extensionId,url:'https://untrusted.test/'}),undefined);
  assert.equal(w.ports.length,0);
  const selected=await w.request('connect');assert.equal(selected.selected,true);assert.equal(selected.connected,false);
  assert.equal(w.badges.get(10),'…');assert.equal(w.ports.length,1);
  assert.deepEqual(w.ports[0].messages.at(-1).tabs,[fixtureTab].map(({active,windowId,...tab})=>tab));
  assert.ok(w.gets.every(id=>id===10));assert.equal(w.badges.has(11),false);
  await w.ports[0].onMessage.emit({ok:true,commands:[]});await flush();
  assert.equal((await w.request('status')).connected,true);assert.equal(w.badges.get(10),'ON');
  await w.request('disconnect');assert.equal(w.badges.get(10),'');assert.deepEqual(w.session.connectedTabs,[]);
  assert.deepEqual(w.ports[0].messages.at(-1).tabs,[]);assert.equal(w.ports[0].disconnected,true);
});

test('missing native host has registration feedback and a manual reconnect path',async()=>{
  const w=await worker();await w.request('connect');await w.lost('Specified native messaging host not found.');
  const status=await w.request('status');assert.equal(status.selected,true);assert.equal(status.connected,false);
  assert.equal(status.error.code,'host_setup');assert.match(status.error.message,/Register/);assert.equal(w.badges.get(10),'!');
  assert.equal([...w.timers.values()].filter(timer=>!timer.interval).length,0);
  await w.request('reconnect');assert.equal(w.ports.length,2);
  await w.ports[1].onMessage.emit({ok:false,error:'Open Forge and enable the browser bridge'});await flush();
  assert.equal((await w.request('status')).error.code,'forge_unavailable');
  await w.ports[1].onMessage.emit({ok:true,commands:[]});await flush();assert.equal(w.badges.get(10),'ON');
});

test('a healthy host reconnects without replaying a dispatched action or its old result',async()=>{
  const w=await worker();await w.request('connect');await w.ports[0].onMessage.emit({ok:true,commands:[]});
  let finish;
  w.chrome.scripting.executeScript=async value=>{w.scripts.push(value);return await new Promise(resolve=>{finish=resolve;});};
  const dispatched=w.ports[0].onMessage.emit({ok:true,commands:[{id:'old-command',tab_id:10,operation:'click',arguments:{selector:'button',snapshot_id:'old'}}]});
  await flush();assert.equal(w.scripts.length,1);await w.lost('Native host exited');
  const timer=[...w.timers.values()].find(item=>!item.interval);assert.equal(timer.delay,1000);timer.fn();await flush();
  assert.equal(w.ports.length,2);finish([{result:{ok:true}}]);await dispatched;await flush();
  assert.equal(w.scripts.length,1);assert.deepEqual(w.ports[1].messages.at(-1).results,[]);
  await w.ports[1].onMessage.emit({ok:true,commands:[]});assert.equal((await w.request('status')).connected,true);
});

test('reconnecting during pending tab preflight prevents the old action from dispatching',async()=>{
  const w=await worker();await w.request('connect');await w.ports[0].onMessage.emit({ok:true,commands:[]});
  const originalGet=w.chrome.tabs.get;let release;
  w.chrome.tabs.get=async id=>{
    const tab=await originalGet(id);
    w.chrome.tabs.get=originalGet;
    return await new Promise(resolve=>{release=()=>resolve(tab);});
  };
  const oldPort=w.ports[0];
  const pending=oldPort.onMessage.emit({ok:true,commands:[{id:'old-pending-command',tab_id:10,operation:'click',arguments:{selector:'button',snapshot_id:'old'}}]});
  await flush();assert.equal(w.scripts.length,0);assert.equal(typeof release,'function');
  await w.request('reconnect');assert.equal(w.ports.length,2);assert.equal(oldPort.disconnected,true);
  release();await pending;await flush();
  assert.equal(w.scripts.length,0);
  assert.ok(w.ports.every(port=>port.messages.every(message=>message.results.length===0)));
  await w.ports[1].onMessage.emit({ok:true,commands:[]});assert.equal((await w.request('status')).connected,true);
});

test('revoking one of two tabs rotates the host and prevents reselected-tab action adoption',async()=>{
  const w=await worker();await w.request('connect',10);await w.request('connect',11);
  await w.ports[0].onMessage.emit({ok:true,commands:[]});
  const originalGet=w.chrome.tabs.get;let release;
  w.chrome.tabs.get=async id=>{
    const tab=await originalGet(id);w.chrome.tabs.get=originalGet;
    return await new Promise(resolve=>{release=()=>resolve(tab);});
  };
  const oldPort=w.ports[0];
  const pending=oldPort.onMessage.emit({ok:true,commands:[{id:'revoked-command',tab_id:10,operation:'click',arguments:{selector:'button',snapshot_id:'old'}}]});
  await flush();assert.equal(w.scripts.length,0);
  const originalSet=w.chrome.storage.session.set;let finishDisconnect;
  w.chrome.storage.session.set=async value=>{
    await originalSet(value);w.chrome.storage.session.set=originalSet;
    await new Promise(resolve=>{finishDisconnect=resolve;});
  };
  const revoking=w.request('disconnect',10);await flush();
  assert.equal(w.ports.length,2);assert.equal(oldPort.disconnected,true);
  await w.request('connect',10);release();await pending;await flush();
  assert.equal(w.scripts.length,0);assert.ok(w.ports.every(port=>port.messages.every(message=>message.results.length===0)));
  finishDisconnect();await revoking;
  assert.deepEqual(w.session.connectedTabs.map(tab=>tab.id).sort(),[10,11]);
  await w.ports[1].onMessage.emit({ok:true,commands:[]});
  assert.equal((await w.request('status',10)).connected,true);assert.equal((await w.request('status',11)).connected,true);
});

test('cross-origin navigation revokes selection; restored sessions never add unrelated tabs',async()=>{
  const w=await worker([fixtureTab,{...fixtureTab,id:99}, {...fixtureTab,id:11,url:'https://different.test/'}]);
  assert.deepEqual([...w.session.connectedTabs].map(tab=>tab.id),[10]);assert.ok(w.gets.includes(99));
  w.tabs.set(10,{...fixtureTab,url:'https://other.test/new'});
  await w.chrome.tabs.onUpdated.emit(10,{url:'https://other.test/new'},w.tabs.get(10));await flush();
  assert.equal((await w.request('status')).selected,false);assert.deepEqual(w.session.connectedTabs,[]);assert.equal(w.badges.get(10),'');
  assert.equal(w.ports[0].disconnected,true);
  w.tabs.set(10,{...fixtureTab,url:'chrome://settings/'});assert.equal((await w.request('connect')).not_executed,true);
});

test('commands cannot target an unselected or revoked tab',async()=>{
  const w=await worker();await w.request('connect');
  assert.equal((await w.run("perform({tab_id:11,operation:'inspect',arguments:{}})")).not_executed,true);
  w.tabs.set(10,{...fixtureTab,url:'https://other.test/'});
  assert.equal((await w.run("perform({tab_id:10,operation:'click',arguments:{}})")).not_executed,true);
  assert.equal(w.scripts.length,0);
});

async function page() {
  const w=await worker();
  class Input {
    constructor(type='text'){Object.assign(this,{tagName:'INPUT',type,_value:'',textContent:'',innerText:'',disabled:false,readOnly:false,isConnected:true,labels:[{textContent:'Your name'}],attributes:{},events:[],clicks:0});}
    get value(){return this._value;}set value(value){this._value=value;}
    getAttribute(name){return name==='type'?this.type:this.attributes[name]??null;}setAttribute(name,value){this.attributes[name]=value;}
    getBoundingClientRect(){return {width:100,height:20};}focus(){this.focused=true;}click(){this.clicks++;}
    dispatchEvent(event){this.events.push(event.type);return true;}
    setRangeText(text,start,end){this._value=this._value.slice(0,start)+text+this._value.slice(end);this.selectionStart=this.selectionEnd=start+text.length;}
  }
  class Select extends Input {constructor(){super('select-one');this.tagName='SELECT';this.labels=[{textContent:'Choose size'}];this.options=[{value:'small',text:'Small',disabled:false},{value:'large',text:'Large',disabled:false},{value:'blocked',text:'Blocked',disabled:true}];this.value='small';}}
  Object.defineProperty(Select.prototype,'value',{get(){return this._value;},set(value){this._value=value;}});
  const input=new Input(), dropdown=new Select(), password=new Input('password');password.value='never-return-this';
  const file=new Input('file');file.value='never-return-file-path';
  let submissions=0;input.form={requestSubmit(){submissions++;}};
  const nodes=[input,dropdown,password,file];let key=0;
  Object.assign(w.context,{window:{scrollBy(x,y){w.context.scrollX+=x;w.context.scrollY+=y;}},location:{href:fixtureTab.url},innerWidth:800,innerHeight:600,scrollX:0,scrollY:0,
    crypto:{randomUUID(){return 'snapshot-'+(++key);}},getComputedStyle:()=>({visibility:'visible'}),HTMLInputElement:Input,HTMLTextAreaElement:Input,HTMLSelectElement:Select,
    HTMLFormElement:class{requestSubmit(){return this.requestSubmit();}},
    Event:class{constructor(type){this.type=type;}},KeyboardEvent:class{constructor(type){this.type=type;}},
    document:{title:'Disposable form',body:{innerText:'Form fixture'},querySelectorAll(selector){return selector==='button,input'?[...nodes,...nodes.flatMap(node=>[...(node.form?.elements||[])].filter(control=>!nodes.includes(control)))]:selector.startsWith('a,button')?nodes:nodes.filter(node=>selector===`[data-forge-target="${node.attributes['data-forge-target']}"]`);}}});
  return {...w,input,dropdown,password,file,submissions:()=>submissions,
    inspect:()=>w.run("pageOperation('inspect',{})"),operate:(operation,args)=>{w.context.operation=operation;w.context.args=args;return w.run('pageOperation(operation,args)');}};
}

test('inspection returns associated labels and options without password/file values',async()=>{
  const p=await page(), snapshot=p.inspect();assert.equal(snapshot.targets[0].label,'Your name');
  assert.equal(snapshot.targets[1].label,'Choose size');assert.equal(snapshot.targets[1].options.length,3);
  assert.equal(snapshot.targets[2].value,undefined);assert.equal(snapshot.targets[3].value,undefined);
  assert.equal(JSON.stringify(snapshot).includes('never-return'),false);assert.deepEqual(JSON.parse(JSON.stringify(snapshot.viewport)),{width:800,height:600});
});

test('form typing, enabled selection and bounded scroll invalidate snapshots',async()=>{
  const p=await page();let snap=p.inspect();
  assert.equal(p.operate('type',{snapshot_id:snap.snapshot_id,selector:snap.targets[0].selector,text:'Fixture text'}).ok,true);assert.equal(p.input.value,'Fixture text');
  assert.equal(p.operate('click',{snapshot_id:snap.snapshot_id,selector:snap.targets[0].selector}).not_executed,true);
  snap=p.inspect();assert.equal(p.operate('select',{snapshot_id:snap.snapshot_id,selector:snap.targets[1].selector,value:'blocked'}).not_executed,true);
  assert.equal(p.operate('select',{snapshot_id:snap.snapshot_id,selector:snap.targets[1].selector,value:'large'}).ok,true);assert.equal(p.dropdown.value,'large');
  snap=p.inspect();assert.equal(p.operate('key',{snapshot_id:snap.snapshot_id,selector:snap.targets[0].selector,key:'Enter'}).ok,true);assert.equal(p.submissions(),1);
  snap=p.inspect();assert.equal(p.operate('scroll',{snapshot_id:snap.snapshot_id,y:1501}).not_executed,true);
  assert.equal(p.operate('scroll',{snapshot_id:snap.snapshot_id,y:500}).ok,true);assert.equal(p.context.scrollY,500);
});

test('changed, expired, disabled and private targets are rejected before effects',async()=>{
  const p=await page();let snap=p.inspect();p.input.value='Changed outside Forge';
  assert.equal(p.operate('click',{snapshot_id:snap.snapshot_id,selector:snap.targets[0].selector}).not_executed,true);assert.equal(p.input.clicks,0);
  snap=p.inspect();p.input.disabled=true;assert.equal(p.operate('click',{snapshot_id:snap.snapshot_id,selector:snap.targets[0].selector}).not_executed,true);
  snap=p.inspect();assert.equal(p.operate('type',{snapshot_id:snap.snapshot_id,selector:snap.targets[2].selector,text:'new'}).not_executed,true);assert.equal(p.password.value,'never-return-this');
  p.context.window.__forgeSnapshot.time-=31000;
  assert.equal(p.operate('scroll',{snapshot_id:snap.snapshot_id,y:10}).not_executed,true);assert.equal(p.context.scrollY,0);
});

test('Enter on a type=button activates that button without submitting its form',async()=>{
  const p=await page();p.input.tagName='BUTTON';p.input.type='button';p.input.labels=[];p.input.innerText='Cancel';
  const snapshot=p.inspect();
  assert.equal(p.operate('key',{snapshot_id:snapshot.snapshot_id,selector:snapshot.targets[0].selector,key:'Enter'}).ok,true);
  assert.equal(p.input.clicks,1);assert.equal(p.submissions(),0);
});

test('text input Enter clicks the default named submitter and excludes range inputs',async()=>{
  const p=await page(), submitter={tagName:'BUTTON',type:'submit',name:'intent',value:'save',disabled:false};
  let submitted=null;p.input.form={elements:[p.input,submitter],requestSubmit(button){submitted=button;}};
  submitter.form=p.input.form;submitter.click=()=>{submitted=submitter;};
  let snapshot=p.inspect();p.operate('key',{snapshot_id:snapshot.snapshot_id,selector:snapshot.targets[0].selector,key:'Enter'});
  assert.equal(submitted,submitter);
  submitted=null;p.input.type='range';p.input.focused=false;snapshot=p.inspect();
  assert.equal(p.operate('key',{snapshot_id:snapshot.snapshot_id,selector:snapshot.targets[0].selector,key:'Enter'}).not_executed,true);
  assert.equal(submitted,null);assert.equal(p.input.focused,false);
});

test('Space inserts into editable text at the current selection',async()=>{
  const p=await page();p.input.value='Initial text';p.input.selectionStart=7;p.input.selectionEnd=7;
  const snapshot=p.inspect();assert.equal(p.operate('key',{snapshot_id:snapshot.snapshot_id,selector:snapshot.targets[0].selector,key:' '}).ok,true);
  assert.equal(p.input.value,'Initial  text');assert.ok(p.input.events.includes('input'));
});

test('text input Enter never skips a disabled default submit button',async()=>{
  const p=await page();let submissions=0;
  const blocked={tagName:'BUTTON',type:'submit',disabled:true,click(){if(!this.disabled)submissions++;}};
  const alternative={tagName:'BUTTON',type:'submit',disabled:false,click(){submissions++;}};
  p.input.form={elements:[p.input,blocked,alternative],requestSubmit(){submissions++;}};
  blocked.form=alternative.form=p.input.form;
  const snapshot=p.inspect();p.operate('key',{snapshot_id:snapshot.snapshot_id,selector:snapshot.targets[0].selector,key:'Enter'});
  assert.equal(submissions,0);
});

test('manifest keeps existing scoped permissions and loads a script-only popup',()=>{
  const manifest=JSON.parse(readFileSync(new URL('../browser-extension/manifest.json',import.meta.url),'utf8'));
  assert.equal(manifest.version,'5.0.3');assert.deepEqual(manifest.permissions,['activeTab','scripting','nativeMessaging','storage']);
  assert.equal(manifest.host_permissions,undefined);assert.equal(manifest.action.default_popup,'popup.html');
  const html=readFileSync(new URL('../browser-extension/popup.html',import.meta.url),'utf8');assert.match(html,/src="popup.js"/);assert.doesNotMatch(html,/onclick=|javascript:/);
});
