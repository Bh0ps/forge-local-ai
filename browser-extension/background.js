/* activeTab only: a toolbar click explicitly connects one selected tab. */
const connected = new Map();
let port = null, polling = null, results = [], lastError = "";
const failure = error => ({ok:false,error,not_executed:true});

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) return;
  if (connected.has(tab.id) && port) {
    connected.delete(tab.id);
    await chrome.action.setBadgeText({tabId:tab.id,text:""});
  } else {
    connected.set(tab.id,{id:tab.id,title:tab.title || "",url:tab.url || ""});
    await chrome.action.setBadgeBackgroundColor({tabId:tab.id,color:"#D76D36"});
    await chrome.action.setBadgeText({tabId:tab.id,text:"ON"});
    ensurePort();
  }
  sendPoll();
});
chrome.tabs.onRemoved.addListener(id => {connected.delete(id);sendPoll();});
chrome.tabs.onUpdated.addListener((id,change,tab) => {
  if (!connected.has(id)) return;
  const previous = connected.get(id);
  try {
    if (change.url && new URL(change.url).origin !== new URL(previous.url).origin) {
      connected.delete(id);chrome.action.setBadgeText({tabId:id,text:""});
    } else connected.set(id,{id,title:tab.title || previous.title,url:tab.url || previous.url});
  } catch {connected.delete(id);}
  sendPoll();
});

function ensurePort() {
  if (port) return;
  port = chrome.runtime.connectNative("org.forge.browser");
  port.onMessage.addListener(async message => {
    if (!message.ok) {
      lastError = message.error || "Forge is unavailable";
      for (const id of connected.keys()) chrome.action.setBadgeText({tabId:id,text:"!"});
      return;
    }
    lastError = "";
    for (const command of message.commands || []) {
      try {results.push({id:command.id,result:await perform(command)});}
      catch (error) {results.push({id:command.id,result:{ok:false,error:String(error.message || error)}});}
    }
    if ((message.commands || []).length) sendPoll();
  });
  port.onDisconnect.addListener(() => {
    lastError = chrome.runtime.lastError?.message || "Bridge disconnected";
    port = null;
    if (polling) clearInterval(polling);
    polling = null;results=[];
    for (const id of connected.keys()) chrome.action.setBadgeText({tabId:id,text:"!"});
  });
  polling = setInterval(sendPoll,1000);
}
function sendPoll() {
  if (!port) return;
  try {port.postMessage({tabs:[...connected.values()],results});results=[];}
  catch {lastError="Bridge disconnected";}
}
async function perform({tab_id,operation,arguments:args}) {
  if (!connected.has(tab_id)) return failure("Tab is not connected");
  let tab;
  try {tab=await chrome.tabs.get(tab_id);} catch {return failure("Connected tab was closed");}
  if (!/^https?:/.test(tab.url || "")) return failure("Unsupported page");
  if (new URL(tab.url).origin !== new URL(connected.get(tab_id).url).origin) return failure("Origin changed. Connect this tab again");
  if (operation === "navigate") {
    let url;
    try {url=new URL(args.url);} catch {return failure("Invalid navigation URL");}
    if (!/^https?:$/.test(url.protocol) || url.username || url.password) return failure("Invalid navigation URL");
    await chrome.tabs.update(tab_id,{url:url.href});
    return {ok:true,url:url.href,message:"Navigation requested; reconnect after an origin change"};
  }
  if (operation === "screenshot") {
    if (!tab.active) return failure("Select the connected tab before capturing its viewport");
    const data_url=await chrome.tabs.captureVisibleTab(tab.windowId,{format:"png"});
    const after=await chrome.tabs.get(tab_id);
    if (!after.active || after.url !== tab.url) return failure("Tab changed while capturing. Inspect again");
    if (data_url.length > 3500000) return failure("Screenshot exceeds the extension message limit. Use Forge's built-in Browser panel");
    return {ok:true,data_url,url:tab.url};
  }
  if (!["inspect","click","type"].includes(operation)) return failure("Unsupported browser operation");
  const response=await chrome.scripting.executeScript({target:{tabId:tab_id},world:"ISOLATED",func:pageOperation,args:[operation,args]});
  return response[0]?.result || failure("Page is unavailable");
}
function pageOperation(operation,args) {
  const fail=error=>({ok:false,error,not_executed:true});
  const signature=el=>[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,el.type!=='password'&&['INPUT','TEXTAREA'].includes(el.tagName)?el.value:null];
  if (operation==='inspect') {
    const id=crypto.randomUUID();
    const targets=[...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
      .filter(el=>{const r=el.getBoundingClientRect();return r.width&&r.height&&getComputedStyle(el).visibility!=='hidden';}).slice(0,200)
      .map((el,i)=>{const key=id+'-'+i;el.setAttribute('data-forge-target',key);return {
        selector:'[data-forge-target="'+key+'"]',tag:el.tagName.toLowerCase(),type:el.getAttribute('type'),
        label:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
        disabled:el.disabled===true,password:el.type==='password',signature:signature(el)};});
    window.__forgeSnapshot={id,url:location.href,time:Date.now(),targets};
    return {ok:true,snapshot_id:id,url:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,24000),
      targets:targets.map(({signature,...target})=>target)};
  }
  const snap=window.__forgeSnapshot;
  if(!snap||snap.id!==args.snapshot_id||snap.url!==location.href||Date.now()-snap.time>30000)return fail('Stale snapshot; inspect again');
  const target=snap.targets.find(item=>item.selector===args.selector);
  if(!target)return fail('Select a target from the current inspection');
  const elements=document.querySelectorAll(args.selector);
  if(elements.length!==1)return fail('Target changed; inspect again');
  const el=elements[0],r=el.getBoundingClientRect();
  if(JSON.stringify(signature(el))!==JSON.stringify(target.signature)||!el.isConnected||!r.width||!r.height||getComputedStyle(el).visibility==='hidden')return fail('Target changed; inspect again');
  if(el.type==='password'||el.type==='file')return fail('Password and file fields require direct user interaction');
  if(el.disabled||el.readOnly)return fail('Target is disabled or read-only');
  if(operation==='type') {
    if(!['INPUT','TEXTAREA'].includes(el.tagName)||!['text','search','email','url','tel','number',''].includes(el.type||'')||typeof args.text!=='string'||args.text.length>12000)return fail('Select a supported text field and at most 12000 characters');
    const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;
    if(!setter)return fail('This field cannot be edited');
    window.__forgeSnapshot=null;
    setter.call(el,args.text);
    el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));
  } else if(operation==='click') {window.__forgeSnapshot=null;el.click();}
  else return fail('Unsupported browser operation');
  return {ok:true,inspect_again:true};
}
