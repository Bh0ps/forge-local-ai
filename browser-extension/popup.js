const elements = Object.fromEntries(['status','indicator','page-title','origin','message','connect','disconnect','reconnect','setup','extension-id','detail'].map(id => [id,document.getElementById(id)]));
let tabId = null, busy = false;
elements['extension-id'].value = chrome.runtime.id;
elements['extension-id'].addEventListener('click',event => event.target.select());
function show(value) {
  const error = value.error;
  elements.status.textContent = value.connected ? 'Connected to Forge' : value.selected ? value.bridge === 'reconnecting' ? 'Reconnecting to Forge…' : error ? 'Connection needs attention' : 'Connecting to Forge…' : 'This tab is not connected';
  elements.indicator.dataset.state = value.connected ? 'connected' : error ? 'error' : value.selected ? value.bridge : 'disconnected';
  elements['page-title'].textContent = value.title || 'This tab'; elements.origin.textContent = value.origin || '';
  elements.message.textContent = error?.message || (value.connected ? 'Your Forge agent can inspect and interact with this tab. Disconnect when you finish.' : value.selected ? 'Waiting for the local browser bridge. Open Forge if it is closed.' : value.eligible ? 'Connect this tab to share its page with your Forge agent.' : 'Choose an HTTP or HTTPS page. Browser settings and protected pages cannot be connected.');
  elements.connect.hidden = value.selected; elements.connect.disabled = busy || !value.eligible;
  elements.disconnect.hidden = !value.selected; elements.disconnect.disabled = busy;
  elements.reconnect.hidden = !value.selected || value.connected; elements.reconnect.disabled = busy;
  elements.detail.textContent = error?.detail || '';
  if (error?.code === 'host_setup' || error?.code === 'forge_unavailable') elements.setup.open = true;
}
async function refresh(action = 'status') {
  if (tabId === null) return;
  try {
    const value = await chrome.runtime.sendMessage({action,tab_id:tabId});
    if (!value?.ok) throw new Error(value?.error || 'The extension did not respond. Reload it in your browser Extensions page.');
    show(value);
  } catch (error) {
    elements.status.textContent = 'Connection unavailable'; elements.indicator.dataset.state = 'error';
    elements.message.textContent = error.message; elements.connect.disabled = false;
  }
}
for (const action of ['connect','disconnect','reconnect']) elements[action].addEventListener('click',async () => {
  busy = true; for (const name of ['connect','disconnect','reconnect']) elements[name].disabled = true;
  await refresh(action); busy = false; await refresh();
});
(async () => {
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  if (!Number.isInteger(tab?.id)) { elements.status.textContent = 'Select a browser tab'; return; }
  tabId = tab.id; await refresh(); setInterval(() => { if (!busy) void refresh(); },1000);
})().catch(error => { elements.status.textContent = 'Connection unavailable'; elements.message.textContent = error.message; });
