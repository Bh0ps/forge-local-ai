/* Only user-selected tabs are retained, for this browser session. */
const connected = new Map();
let port = null, polling = null, reconnectTimer = null, reconnectAttempt = 0;
let results = [], hostReady = false, lastError = null, commandWork = Promise.resolve();
const failure = error => ({ok:false,error,not_executed:true});
const pageUrl = value => {
  try { const url = new URL(value); return /^https?:$/.test(url.protocol) && !url.username && !url.password ? url : null; }
  catch { return null; }
};
const selection = tab => ({id:tab.id,title:(tab.title || "").slice(0,300),url:tab.url.slice(0,2000)});
const saveSelections = () => chrome.storage.session.set({connectedTabs:[...connected.values()]}).catch(() => {});

async function badge(id) {
  const selected = connected.has(id), text = !selected ? "" : hostReady ? "ON" : lastError ? "!" : "…";
  try {
    await chrome.action.setBadgeBackgroundColor({tabId:id,color:hostReady ? "#D76D36" : lastError ? "#B74141" : "#8C6A32"});
    await chrome.action.setBadgeText({tabId:id,text});
    await chrome.action.setTitle({tabId:id,title:!selected ? "Connect this tab to Forge" : hostReady ? "This tab is connected to Forge" : "Forge connection needs attention"});
  } catch { /* The user may have closed this tab. */ }
}
function refreshBadges() { for (const id of connected.keys()) void badge(id); }
function connectionError(detail) {
  const missing = /not found|not registered|forbidden|access.*denied/i.test(detail);
  return {code:missing ? "host_setup" : "disconnected",
    message:missing ? "Register this extension's native host in Forge Settings → Browser, then reconnect."
      : "Forge disconnected. Open Forge and enable Browser bridge, then reconnect.", detail};
}
function stopPort() {
  const old = port; port = null; hostReady = false; results = [];
  if (polling) clearInterval(polling); polling = null;
  if (reconnectTimer) clearTimeout(reconnectTimer); reconnectTimer = null;
  if (old) { try { old.disconnect(); } catch {} }
}
function scheduleReconnect() {
  if (!connected.size || reconnectTimer || reconnectAttempt >= 6) return;
  const delay = Math.min(1000 * 2 ** reconnectAttempt++, 30000);
  reconnectTimer = setTimeout(() => { reconnectTimer = null; ensurePort(); sendPoll(); }, delay);
}
function lostPort(current, detail) {
  if (port !== current) return;
  const retry = hostReady || reconnectAttempt > 0;
  stopPort(); lastError = connectionError(detail); refreshBadges();
  if (retry && lastError.code !== "host_setup") scheduleReconnect();
}
function ensurePort() {
  if (port || !connected.size) return;
  try {
    const current = chrome.runtime.connectNative("org.forge.browser");
    port = current; hostReady = false; refreshBadges();
    current.onMessage.addListener(message => {
      if (port !== current) return;
      if (!message.ok) {
        hostReady = false;
        lastError = {code:"forge_unavailable",message:"Open Forge and enable Browser bridge in Settings → Browser.",detail:String(message.error || "Forge is unavailable")};
        refreshBadges(); return;
      }
      hostReady = true; lastError = null; reconnectAttempt = 0; refreshBadges();
      // A host session can never replay a previous session's dispatched actions.
      commandWork = commandWork.then(async () => {
        for (const command of (Array.isArray(message.commands) ? message.commands.slice(0,100) : [])) {
          if (port !== current) return;
          let result;
          try { result = await perform(command,current); }
          catch (error) { result = {ok:false,error:String(error.message || error)}; }
          if (port === current) results.push({id:command.id,result});
        }
        if (port === current && (message.commands || []).length) sendPoll();
      });
      return commandWork;
    });
    current.onDisconnect.addListener(() => lostPort(current, chrome.runtime.lastError?.message || "Native host disconnected"));
    polling = setInterval(sendPoll,1000);
  } catch (error) {
    lastError = connectionError(String(error.message || error)); refreshBadges();
  }
}
function sendPoll() {
  if (!port) return;
  const current = port;
  try { current.postMessage({tabs:[...connected.values()],results}); results = []; }
  catch (error) { lostPort(current,String(error.message || error)); }
}
async function disconnectTab(id) {
  const revoked = connected.delete(id);
  if (revoked) {
    // Rotate the host namespace so a later selection cannot adopt commands or
    // snapshots issued before this tab's access was revoked. Revoke before any
    // await, including session storage and badge updates.
    sendPoll();
    stopPort(); lastError = null; reconnectAttempt = 0; ensurePort(); sendPoll();
  }
  await saveSelections(); await badge(id);
}
const ready = (async () => {
  try {
    const saved = await chrome.storage.session.get({connectedTabs:[]});
    for (const item of saved.connectedTabs.slice(0,100)) {
      if (!Number.isInteger(item.id) || !pageUrl(item.url)) continue;
      try {
        const tab = await chrome.tabs.get(item.id);
        if (pageUrl(tab.url)?.origin === pageUrl(item.url).origin) connected.set(tab.id,selection(tab));
      } catch { /* Closed tabs and revoked activeTab grants are not restored. */ }
    }
    await saveSelections(); ensurePort(); sendPoll();
  } catch { /* A fresh browser session has no selected tabs. */ }
})();

async function statusFor(id) {
  let tab = null;
  try { tab = await chrome.tabs.get(id); } catch {}
  if (connected.has(id) && pageUrl(tab?.url)?.origin !== pageUrl(connected.get(id).url)?.origin) await disconnectTab(id);
  return {ok:true,selected:connected.has(id),connected:connected.has(id) && hostReady,
    eligible:!!pageUrl(tab?.url),bridge:hostReady ? "connected" : reconnectTimer ? "reconnecting" : port ? "connecting" : "disconnected",
    error:lastError,title:tab?.title || "This tab",origin:pageUrl(tab?.url)?.origin || "",extension_id:chrome.runtime.id};
}
chrome.runtime.onMessage.addListener((request,sender,reply) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return false;
  (async () => {
    await ready;
    const id = request?.tab_id;
    if (!Number.isInteger(id) || id < 0) return failure("Select a browser tab first");
    if (request.action === "connect") {
      const tab = await chrome.tabs.get(id);
      if (!pageUrl(tab.url)) return failure("Choose an HTTP or HTTPS page. Browser settings and protected pages cannot be connected.");
      connected.set(id,selection(tab)); lastError = null; reconnectAttempt = 0;
      await saveSelections(); ensurePort(); await badge(id); sendPoll();
    } else if (request.action === "disconnect") await disconnectTab(id);
    else if (request.action === "reconnect") {
      if (!connected.has(id)) return failure("Connect this tab before reconnecting Forge");
      stopPort(); lastError = null; reconnectAttempt = 0; ensurePort(); sendPoll();
    } else if (request.action !== "status") return failure("Unsupported extension action");
    return statusFor(id);
  })().then(reply,error => reply(failure(String(error.message || error))));
  return true;
});
chrome.tabs.onRemoved.addListener(id => { void ready.then(() => { if (connected.has(id)) return disconnectTab(id); }); });
chrome.tabs.onUpdated.addListener((id,change,tab) => { void ready.then(async () => {
  if (!connected.has(id)) return;
  const previous = connected.get(id), current = pageUrl(change.url || tab.url);
  if ((change.url || change.status === "loading") && current?.origin !== pageUrl(previous.url)?.origin) {
    await disconnectTab(id); return;
  }
  if (current) connected.set(id,selection({...tab,url:current.href,title:tab.title || previous.title}));
  await saveSelections(); sendPoll();
}); });

async function perform({tab_id,operation,arguments:args = {}},session = port) {
  const sessionChanged = () => !session || port !== session;
  const disconnected = () => failure("The Forge connection changed before dispatch. Request a fresh action after reconnecting.");
  if (sessionChanged()) return disconnected();
  if (!connected.has(tab_id)) return failure("Tab is not connected. Use the extension's Connect this tab button.");
  let tab;
  try { tab = await chrome.tabs.get(tab_id); } catch { return failure("Connected tab was closed"); }
  if (sessionChanged()) return disconnected();
  const selected = connected.get(tab_id), url = pageUrl(tab.url);
  if (!selected) return failure("The user disconnected this tab");
  if (!url || url.origin !== pageUrl(selected.url)?.origin) return failure("Page access changed. Connect this tab again.");
  if (operation === "navigate") {
    const destination = pageUrl(args.url);
    if (!destination) return failure("Invalid navigation URL");
    if (sessionChanged()) return disconnected();
    await chrome.tabs.update(tab_id,{url:destination.href});
    return {ok:true,url:destination.href,message:"Navigation requested; reconnect after an origin change"};
  }
  if (operation === "screenshot") {
    if (!tab.active) return failure("Select the connected tab before capturing its viewport");
    if (sessionChanged()) return disconnected();
    const data_url = await chrome.tabs.captureVisibleTab(tab.windowId,{format:"png"});
    if (sessionChanged()) return failure("The Forge connection changed during capture. Request a fresh screenshot.");
    const after = await chrome.tabs.get(tab_id);
    if (sessionChanged()) return failure("The Forge connection changed during capture. Request a fresh screenshot.");
    if (!after.active || after.url !== tab.url) return failure("Tab changed while capturing. Inspect again");
    if (data_url.length > 3500000) return failure("Screenshot exceeds the extension message limit. Use Forge's built-in Browser panel");
    return {ok:true,data_url,url:tab.url};
  }
  if (!["inspect","click","type","select","key","scroll"].includes(operation)) return failure("Unsupported browser operation");
  if (sessionChanged()) return disconnected();
  const response = await chrome.scripting.executeScript({target:{tabId:tab_id},world:"ISOLATED",func:pageOperation,args:[operation,args]});
  return response[0]?.result || failure("Page is unavailable");
}
function pageOperation(operation,args) {
  const fail = error => ({ok:false,error,not_executed:true});
  const options = el => [...el.options].slice(0,200).map(o => [o.value.slice(0,300),o.text.slice(0,300),o.disabled]);
  const signature = el => [el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,
    el.tagName==='SELECT' ? [el.value,options(el)] : !['password','file'].includes(el.type) && ['INPUT','TEXTAREA'].includes(el.tagName) ? el.value : null];
  if (operation === 'inspect') {
    const id = crypto.randomUUID();
    const targets = [...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
      .filter(el => { const r = el.getBoundingClientRect(); return r.width && r.height && getComputedStyle(el).visibility !== 'hidden'; }).slice(0,200)
      .map((el,i) => {
        const key = id+'-'+i; el.setAttribute('data-forge-target',key);
        return {selector:'[data-forge-target="'+key+'"]',tag:el.tagName.toLowerCase(),type:el.getAttribute('type'),
          label:(el.getAttribute('aria-label') || [...(el.labels || [])].map(label => label.textContent).join(' ') || el.innerText || el.getAttribute('placeholder') || '').trim().slice(0,300),
          disabled:el.disabled===true,password:el.type==='password',
          ...(!['password','file'].includes(el.type) && ['INPUT','TEXTAREA','SELECT'].includes(el.tagName) ? {value:String(el.value).slice(0,300)} : {}),
          ...(el.tagName==='SELECT' ? {options:options(el).map(([value,label,disabled]) => ({value,label,disabled}))} : {}),signature:signature(el)};
      });
    window.__forgeSnapshot = {id,url:location.href,time:Date.now(),targets};
    return {ok:true,snapshot_id:id,url:location.href,title:document.title.slice(0,500),text:(document.body?.innerText || '').slice(0,24000),
      viewport:{width:innerWidth,height:innerHeight},scroll:{x:scrollX,y:scrollY},targets:targets.map(({signature,...target}) => target)};
  }
  const snap = window.__forgeSnapshot;
  if (!snap || snap.id !== args.snapshot_id || snap.url !== location.href || Date.now()-snap.time > 30000) return fail('Stale snapshot; inspect again');
  if (operation === 'scroll') {
    const x = args.x ?? 0, y = args.y ?? 0;
    if (!Number.isInteger(x) || !Number.isInteger(y) || Math.max(Math.abs(x),Math.abs(y)) > 1500) return fail('Scroll by integer offsets of at most 1500 CSS pixels');
    window.__forgeSnapshot = null; window.scrollBy(x,y); return {ok:true,inspect_again:true};
  }
  const target = snap.targets.find(item => item.selector === args.selector);
  if (!target) return fail('Select a target from the current inspection');
  const elements = document.querySelectorAll(args.selector);
  if (elements.length !== 1) return fail('Target changed; inspect again');
  const el = elements[0], r = el.getBoundingClientRect();
  if (JSON.stringify(signature(el)) !== JSON.stringify(target.signature) || !el.isConnected || !r.width || !r.height || getComputedStyle(el).visibility === 'hidden') return fail('Target changed; inspect again');
  if (el.type === 'password' || el.type === 'file') return fail('Password and file fields require direct user interaction');
  if (el.disabled || el.readOnly) return fail('Target is disabled or read-only');
  if (operation === 'type') {
    if (!['INPUT','TEXTAREA'].includes(el.tagName) || !['text','search','email','url','tel','number',''].includes(el.type || '') || typeof args.text !== 'string' || args.text.length > 12000) return fail('Select a supported text field and at most 12000 characters');
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto,'value')?.set;
    if (!setter) return fail('This field cannot be edited');
    window.__forgeSnapshot = null; setter.call(el,args.text);
    el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
  } else if (operation === 'select') {
    if (el.tagName !== 'SELECT' || el.options.length > 200 || typeof args.value !== 'string') return fail('Choose a dropdown option returned by inspection');
    if (![...el.options].some(option => option.value === args.value && !option.disabled)) return fail('Option changed or is disabled; inspect again');
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')?.set;
    if (!setter) return fail('Dropdown cannot be edited');
    window.__forgeSnapshot = null; setter.call(el,args.value);
    el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
  } else if (operation === 'key') {
    if (!['Enter','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' ','Backspace','Delete','Home','End'].includes(args.key)) return fail('Unsupported browser key');
    const button = el.tagName === 'BUTTON' || el.tagName === 'INPUT' && ['button','submit','reset','image'].includes(el.type);
    const checkable = el.tagName === 'INPUT' && ['checkbox','radio'].includes(el.type);
    const textInput = el.tagName === 'INPUT' && ['text','search','email','url','tel','number',''].includes(el.type || '');
    const textEntry = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' && ['text','search','url','tel',''].includes(el.type || '');
    if (args.key === 'Enter' && !button && !textInput && el.tagName !== 'TEXTAREA' && el.tagName !== 'A') return fail('Enter default action is unsupported for this target. Use browser_click or another supported action.');
    if (args.key === ' ' && !button && !checkable && !textEntry) return fail('Space default action is unsupported for this target. Use browser_click or browser_type.');
    window.__forgeSnapshot = null; el.focus();
    const proceed = el.dispatchEvent(new KeyboardEvent('keydown',{key:args.key,bubbles:true,cancelable:true}));
    if (proceed && args.key === 'Enter') {
      if (button || el.tagName === 'A') el.click();
      else if (el.tagName === 'TEXTAREA') {
        el.setRangeText('\n',el.selectionStart,el.selectionEnd,'end');
        el.dispatchEvent(new Event('input',{bubbles:true}));
      } else if (textInput && el.form) {
        const submitter = [...document.querySelectorAll('button,input')].find(control => control.form === el.form && ['submit','image'].includes(control.type));
        if (submitter) submitter.click();
        else {
          const blockers = [...(el.form.elements || [])].filter(control => control.tagName === 'INPUT' && ['text','search','email','url','tel','number','password','date','month','week','time','datetime-local'].includes(control.type));
          if (blockers.length <= 1) HTMLFormElement.prototype.requestSubmit.call(el.form);
        }
      }
    } else if (proceed && args.key === ' ' && textEntry) {
      el.setRangeText(' ',el.selectionStart,el.selectionEnd,'end');
      el.dispatchEvent(new Event('input',{bubbles:true}));
    }
    el.dispatchEvent(new KeyboardEvent('keyup',{key:args.key,bubbles:true}));
    if (proceed && args.key === ' ' && (button || checkable)) el.click();
  } else if (operation === 'click') { window.__forgeSnapshot = null; el.click(); }
  else return fail('Unsupported browser operation');
  return {ok:true,inspect_again:true};
}
