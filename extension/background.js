// Dia Live Bridge — command channel.
// The extension long-polls the local bridge for commands and executes them against a
// live Dia tab through chrome.debugger (CDP 1.3). No launch flag, no restart.
//
// Commands: status, list_tabs, attach, detach, eval, cdp, navigate, snapshot,
//           click, click_text, fill, network, body, console, shot

const BRIDGE = 'http://127.0.0.1:9777';

let attachedTabId = null;
let lastDialog = null;
const net = new Map();       // requestId -> record
const consoleLog = [];       // {level, text, ts}
const MAX_LOG = 500;

function post(path, payload) {
  return fetch(BRIDGE + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  }).catch(() => null);
}

// ---------- CDP send ----------
async function send(method, params = {}) {
  if (attachedTabId == null) throw new Error('not attached to a tab');
  return chrome.debugger.sendCommand({ tabId: attachedTabId }, method, params);
}

async function attach(tabId) {
  if (attachedTabId === tabId) return { tabId, already: true };
  if (attachedTabId != null) {
    try { await chrome.debugger.detach({ tabId: attachedTabId }); } catch (e) {}
    attachedTabId = null;
  }
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) throw new Error('no such tab: ' + tabId);
  if (!/^(https?|file):/i.test(tab.url || '')) throw new Error('cannot attach to ' + (tab.url || 'unknown').slice(0, 40));
  await chrome.debugger.attach({ tabId }, '1.3');
  attachedTabId = tabId;
  await send('Runtime.enable');
  await send('Network.enable', { maxTotalBufferSize: 100000000, maxResourceBufferSize: 20000000 });
  await send('Page.enable');
  await send('Log.enable');
  await send('DOM.enable');
  return { tabId, url: tab.url, title: tab.title };
}

async function detach() {
  if (attachedTabId != null) {
    try { await chrome.debugger.detach({ tabId: attachedTabId }); } catch (e) {}
    attachedTabId = null;
  }
  return { detached: true };
}

// ---------- event capture ----------
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId !== attachedTabId) return;
  try {
    if (method === 'Network.requestWillBeSent') {
      const r = net.get(params.requestId) || { requestId: params.requestId };
      r.url = params.request.url;
      r.method = params.request.method;
      r.type = params.type;
      r.reqHeaders = params.request.headers;
      net.set(params.requestId, r);
    } else if (method === 'Network.responseReceived') {
      const r = net.get(params.requestId) || { requestId: params.requestId };
      r.status = params.response.status;
      r.mimeType = params.response.mimeType;
      if (!r.url) r.url = params.response.url;
      net.set(params.requestId, r);
    } else if (method === 'Network.loadingFinished') {
      const r = net.get(params.requestId);
      if (r) { r.finished = true; r.encodedBytes = params.encodedDataLength; }
    } else if (method === 'Network.loadingFailed') {
      const r = net.get(params.requestId);
      if (r) { r.failed = params.errorText; }
    } else if (method === 'Runtime.consoleAPICalled') {
      const text = (params.args || [])
        .map((a) => (a.value !== undefined ? String(a.value) : (a.description || a.type)))
        .join(' ');
      push({ level: params.type, text, ts: Date.now() });
    } else if (method === 'Log.entryAdded') {
      push({ level: params.entry.level || 'log', text: params.entry.text, url: params.entry.url, ts: Date.now() });
    } else if (method === 'Page.javascriptDialogOpening') {
      lastDialog = {
        type: params.type,
        message: params.message,
        defaultPrompt: params.defaultPrompt || '',
        url: params.url,
      };
    }
  } catch (e) {}
});

function push(entry) {
  consoleLog.push(entry);
  if (consoleLog.length > MAX_LOG) consoleLog.splice(0, consoleLog.length - MAX_LOG);
}

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId === attachedTabId) {
    attachedTabId = null;
    post('/report', { stage: 'detached', reason });
  }
});

// ---------- helpers ----------
async function evaluate(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text || 'evaluate failed');
  }
  return r.result?.value;
}

async function summarizeA11y() {
  // Compact, agent-friendly element map: role + name + backendNodeId, no layout noise.
  const { nodes } = await send('Accessibility.getFullAXTree');
  return nodes
    .filter((n) => !n.ignored && n.role && n.name && n.name.value && n.backendDOMNodeId)
    .map((n) => ({ role: n.role.value, name: String(n.name.value).slice(0, 120), backendNodeId: n.backendDOMNodeId }))
    .slice(0, 400);
}

// ---------- command dispatch ----------
async function run(cmd) {
  switch (cmd.type) {
    case 'status':
      return { attachedTabId, network: net.size, console: consoleLog.length };

    case 'list_tabs': {
      const tabs = await chrome.tabs.query({});
      return tabs
        .filter((t) => /^(https?|file):/i.test(t.url || ''))
        .map((t) => ({ tabId: t.id, title: t.title, url: t.url, active: t.active, windowId: t.windowId }));
    }

    case 'attach': {
      let tabId = cmd.tabId;
      if (!tabId) {
        const tabs = (await chrome.tabs.query({})).filter((t) => /^(https?|file):/i.test(t.url || ''));
        tabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
        if (!tabs[0]) throw new Error('no web tab open');
        tabId = tabs[0].id;
      }
      net.clear();
      consoleLog.length = 0;
      lastDialog = null;
      return attach(tabId);
    }

    case 'create_tab': {
      const tab = await chrome.tabs.create({ url: cmd.url, active: cmd.active !== false });
      return { tabId: tab.id, url: tab.url || cmd.url, title: tab.title || '' };
    }

    case 'close_tab': {
      await chrome.tabs.remove(cmd.tabId);
      if (attachedTabId === cmd.tabId) attachedTabId = null;
      return { closed: cmd.tabId };
    }

    case 'activate_tab': {
      const tab = await chrome.tabs.get(cmd.tabId);
      await chrome.tabs.update(cmd.tabId, { active: true });
      if (tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true });
      return { tabId: cmd.tabId, url: tab.url, title: tab.title };
    }

    case 'dialog': {
      await send('Page.handleJavaScriptDialog', {
        accept: cmd.accept !== false,
        ...(cmd.promptText != null ? { promptText: cmd.promptText } : {}),
      });
      lastDialog = null;
      return { handled: true };
    }

    case 'last_dialog':
      return { dialog: lastDialog };

    case 'detach':
      return detach();

    case 'eval':
      return { value: await evaluate(cmd.expression, !!cmd.awaitPromise) };

    case 'cdp':
      return { result: await send(cmd.method, cmd.params || {}) };

    case 'navigate': {
      const r = await send('Page.navigate', { url: cmd.url });
      return { frameId: r.frameId, errorText: r.errorText || null };
    }

    case 'snapshot':
      return { elements: await summarizeA11y() };

    case 'click': {
      const sel = cmd.selector;
      const expr = `(function(){var el=document.querySelector(${JSON.stringify(sel)});if(!el)throw new Error('not found: '+${JSON.stringify(sel)});el.scrollIntoView({block:'center'});el.click();return {tag:el.tagName,text:(el.innerText||'').slice(0,80)};})()`;
      return { clicked: await evaluate(expr) };
    }

    case 'click_text': {
      const t = cmd.text;
      const expr = `(function(){var t=${JSON.stringify(t)};var all=[...document.querySelectorAll('button,[role=button],a,input[type=submit],li,span,div')];var el=all.find(e=>((e.innerText||e.value||'').trim()===t)&&e.offsetParent!==null)||all.find(e=>((e.innerText||'').trim().toLowerCase()===t.toLowerCase())&&e.offsetParent!==null);if(!el)throw new Error('no visible element with text: '+t);el.scrollIntoView({block:'center'});el.click();return {tag:el.tagName,text:(el.innerText||'').slice(0,80)};})()`;
      return { clicked: await evaluate(expr) };
    }

    case 'fill': {
      const sel = cmd.selector, val = cmd.value;
      const expr = `(function(){var el=document.querySelector(${JSON.stringify(sel)});if(!el)throw new Error('not found: '+${JSON.stringify(sel)});el.focus();var proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;var setter=Object.getOwnPropertyDescriptor(proto,'value').set;setter.call(el,${JSON.stringify(val)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return el.value;})()`;
      return { value: await evaluate(expr) };
    }

    case 'network': {
      const limit = cmd.limit || 50;
      const rows = [...net.values()]
        .filter((r) => (cmd.filter ? (r.url || '').includes(cmd.filter) : true))
        .slice(-limit)
        .map((r) => ({
          requestId: r.requestId, method: r.method, status: r.status, type: r.type,
          mimeType: r.mimeType, bytes: r.encodedBytes, failed: r.failed || null,
          url: (r.url || '').slice(0, 300),
        }));
      return { count: rows.length, requests: rows };
    }

    case 'body': {
      const r = await send('Network.getResponseBody', { requestId: cmd.requestId });
      const raw = r.base64Encoded ? atob(r.body) : r.body;
      const max = cmd.max || 4000;
      return { base64Encoded: !!r.base64Encoded, length: raw.length, body: raw.slice(0, max) };
    }

    case 'console':
      return { entries: consoleLog.slice(-(cmd.limit || 100)) };

    case 'shot': {
      const r = await send('Page.captureScreenshot', { format: cmd.format || 'png' });
      return { format: cmd.format || 'png', base64: r.data };
    }

    default:
      throw new Error('unknown command: ' + cmd.type);
  }
}

// ---------- long-poll loop ----------
// Exactly one loop may run. The alarm and the wake events below can fire at any
// time, and two concurrent loops split commands between them: one holds a long
// poll the other never sees, so calls look random and time out.
let polling = false;

async function pollLoop() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      let cmd = null;
      try {
        const res = await fetch(BRIDGE + '/cmd', { cache: 'no-store' });
        if (res.status === 200) cmd = await res.json();
        else await new Promise((r) => setTimeout(r, 500));
      } catch (e) {
        await new Promise((r) => setTimeout(r, 1000));
        continue;
      }
      if (!cmd || !cmd.id) continue;
      let out;
      try {
        out = { id: cmd.id, ok: true, result: await run(cmd) };
      } catch (e) {
        out = { id: cmd.id, ok: false, error: String(e && e.message ? e.message : e) };
      }
      await post('/result', out);
    }
  } finally {
    polling = false;
  }
}

function wake() {
  pollLoop();
}

// MV3 suspends the worker after a short idle period; these events bring it back.
chrome.alarms.create('keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'keepalive') wake(); });
chrome.tabs.onActivated.addListener(() => wake());
chrome.tabs.onUpdated.addListener(() => wake());
chrome.windows.onFocusChanged.addListener(() => wake());
chrome.runtime.onStartup.addListener(() => wake());
chrome.runtime.onInstalled.addListener(() => wake());

wake();
