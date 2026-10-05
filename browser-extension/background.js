/* No host permissions: clicking the action grants access to that selected tab. */
const connected = new Map();
let port = null;
let polling = null;
let results = [];
let lastError = "";

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) return;
  if (connected.has(tab.id)) {
    connected.delete(tab.id);
    await chrome.action.setBadgeText({tabId: tab.id, text: ""});
  } else {
    connected.set(tab.id, {id: tab.id, title: tab.title || "", url: tab.url || ""});
    await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: "#D76D36"});
    await chrome.action.setBadgeText({tabId: tab.id, text: "ON"});
    ensurePort();
  }
  sendPoll();
});
chrome.tabs.onRemoved.addListener(id => connected.delete(id));
chrome.tabs.onUpdated.addListener((id, change, tab) => {
  if (!connected.has(id)) return;
  // activeTab expires after a change of origin: require an explicit new click.
  const previous = connected.get(id);
  if (change.url && new URL(change.url).origin !== new URL(previous.url).origin) {
    connected.delete(id);
    chrome.action.setBadgeText({tabId: id, text: ""});
  } else connected.set(id, {id, title: tab.title || previous.title, url: tab.url || previous.url});
});

function ensurePort() {
  if (port) return;
  port = chrome.runtime.connectNative("org.forge.browser");
  port.onMessage.addListener(async message => {
    if (!message.ok) {
      lastError = message.error || "Forge is unavailable";
      for (const id of connected.keys()) chrome.action.setBadgeText({tabId:id, text:"!"});
      return;
    }
    lastError = "";
    for (const command of message.commands || []) {
      try { results.push({id: command.id, result: await perform(command)}); }
      catch (error) { results.push({id: command.id, result: {ok:false,error:String(error.message || error)}}); }
    }
  });
  port.onDisconnect.addListener(() => {
    lastError = chrome.runtime.lastError?.message || "Bridge disconnected";
    port = null;
    if (polling) clearInterval(polling);
    polling = null;
    for (const id of connected.keys()) chrome.action.setBadgeText({tabId:id, text:"!"});
  });
  polling = setInterval(sendPoll, 1000);
}
function sendPoll() {
  if (!port) return;
  port.postMessage({tabs:[...connected.values()],results});
  results = [];
}
async function perform({tab_id, operation, arguments: args}) {
  if (!connected.has(tab_id)) throw Error("Tab is not connected");
  const tab = await chrome.tabs.get(tab_id);
  if (!/^https?:/.test(tab.url || "")) throw Error("Unsupported page");
  if (operation === "navigate") {
    const url = new URL(args.url);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw Error("Invalid URL");
    // An origin change drops activeTab access; connect again to inspect it.
    await chrome.tabs.update(tab_id, {url:url.href});
    return {ok:true,url:url.href,message:"Navigation requested; reconnect after an origin change"};
  }
  if (operation === "screenshot") {
    if (!tab.active) throw Error("Select the connected tab before capturing its viewport");
    const data_url = await chrome.tabs.captureVisibleTab(tab.windowId, {format:"png"});
    return {ok:true,data_url,url:tab.url};
  }
  const response = await chrome.scripting.executeScript({target:{tabId:tab_id},
    func: pageOperation, args:[operation,args]});
  return response[0]?.result || {ok:false,error:"Page is unavailable"};
}
function pageOperation(operation,args) {
  const inspect = () => {
    const id = crypto.randomUUID();
    const targets = [...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
      .filter(el=>el.getBoundingClientRect().width && el.getBoundingClientRect().height).slice(0,200)
      .map((el,i)=>{const target=id+'-'+i;el.setAttribute('data-forge-target',target);return {
        selector:'[data-forge-target="'+target+'"]',tag:el.tagName.toLowerCase(),
        label:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
        signature:[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.textContent.slice(0,200)]};});
    window.__forgeSnapshot={id,url:location.href,targets};
    return {ok:true,snapshot_id:id,url:location.href,title:document.title,text:document.body.innerText.slice(0,24000),
      targets:targets.map(({signature,...target})=>target)};
  };
  if(operation==='inspect') return inspect();
  const snap=window.__forgeSnapshot;
  if(!snap || snap.id!==args.snapshot_id || snap.url!==location.href) throw Error('Stale snapshot; inspect again');
  const target=snap.targets.find(item=>item.selector===args.selector);
  if(!target) throw Error('Select a target from the current inspection');
  const elements=document.querySelectorAll(args.selector);
  if(elements.length!==1) throw Error('Target changed; inspect again');
  const el=elements[0];
  const signature=[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.textContent.slice(0,200)];
  if(JSON.stringify(signature)!==JSON.stringify(target.signature)) throw Error('Target changed; inspect again');
  window.__forgeSnapshot=null;
  if(operation==='click') el.click();
  else if(operation==='type') {
    if(!['INPUT','TEXTAREA'].includes(el.tagName)) throw Error('Target is not a text field');
    const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(el,String(args.text || ''));
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
  } else throw Error('Unsupported operation');
  return inspect();
}
