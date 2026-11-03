/**
 * background.js —— RPC 层 + 跨帧清单缓存。
 *
 * 两件事：
 *   1. 和本机软件通信（ping / import / task）。跨域请求必须放在这里，
 *      因为 service worker 有 host_permissions，不受页面 CORS 和混合内容限制。
 *   2. 缓存各子框架抓到的清单（stash / gather）。有些 PACS 把影像界面塞在
 *      iframe 里，数据在子帧、按钮在顶层帧，得有人居中转一手。
 */

const APP_ID = 'zhigu-ct3d';
const HEADERS = {
  'Content-Type': 'application/json',
  'X-Zhigu-Bridge': '1'
};
// 800ms 对"service worker 刚被唤醒"太紧：SW 冷启动时第一次 fetch 常要
// 1.5~3s，超时后 pingPort 返回 null → 扩展判定"软件没运行"，于是又去拉协议
// 唤起，表现就是「点了按钮，软件被拉起，但没数据」。
const PING_TIMEOUT = 3000;
const POST_TIMEOUT = 15000;
const STASH_TTL = 5 * 60 * 1000;

// tabId -> { frameId -> { at, data } }
const STASH = new Map();

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise(function (_, reject) {
      setTimeout(function () { reject(new Error('timeout ' + ms + 'ms')); }, ms);
    })
  ]);
}

/**
 * 探测一个端口。返回 { port, info } 或 { port, error }。
 *
 * 一定要把 error 带出去：之前失败一律返回 null，导致「探不到」和
 * 「探到了但 app 不匹配」在界面上长得一模一样，只能靠猜。
 */
async function pingPort(port) {
  try {
    const res = await withTimeout(
      fetch('http://127.0.0.1:' + port + '/ping', {
        cache: 'no-store',
        headers: { 'X-Zhigu-Bridge': '1' }
      }), PING_TIMEOUT);
    if (!res.ok) {
      return { port: port, error: 'HTTP ' + res.status };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { return { port: port, error: '响应不是 JSON' }; }
    if (!data || data.app !== APP_ID) {
      return { port: port, error: 'app 不匹配（收到 ' + (data && data.app) + '）' };
    }
    return { port: port, info: data };
  } catch (e) {
    const msg = String((e && e.message) || e);
    return { port: port, error: (msg.indexOf('timeout') === 0 ? '连接超时' : msg) };
  }
}

async function findPort(ports) {
  const list = Array.isArray(ports) && ports.length ? ports : [18923];
  // 串行扫：并发扫会让浏览器在多个端口同时打 SYN，失败端口反而变慢，
  // 而且命中率最高的是列表里第一个（软件默认绑 18923）。
  const tried = [];
  for (let i = 0; i < list.length; i++) {
    const r = await pingPort(list[i]);
    if (r.info) return { port: list[i], info: r.info, tried: tried };
    tried.push(r.port + ':' + r.error);
    // 第一个端口就"连接被拒"说明软件确实没起，再扫剩下 7 个纯属浪费时间；
    // 只有超时/HTTP 异常才值得继续往后试（可能是别的程序占了端口）。
    if (r.error && r.error.indexOf('Failed to fetch') >= 0 && i === 0) break;
  }
  return { port: 0, tried: tried };
}

// ------------------------------------------------------------------ 原生消息通道

/**
 * 为什么要这条通道：
 *   HTTP 回环（127.0.0.1:18923）在装了代理软件 / VPN（Clash、v2ray 的 TUN
 *   模式）或企业代理管理扩展的机器上会静默失败 —— TCP 根本建不起来，浏览器
 *   也不给可读错误。现象就是「软件被唤起，但一张图都没送过去」。
 *
 *   原生消息走 Chrome 与本地进程之间的 stdio 管道，完全不经网络栈，
 *   代理、VPN、防火墙、混合内容策略都碰不到它。
 *
 * 策略：**先 native，失败再 HTTP**。原生通道不可用（没注册）时立刻回退，
 * 不额外拖慢已有部署。
 */
const NATIVE_HOST = 'com.zhigu.bridge';
const NATIVE_TIMEOUT = 20000;

let nativePort = null;
let nativeSeq = 0;
const nativeWaiters = new Map();
let nativeState = 'unknown';          // unknown | ok | missing

function nativeDisconnectAll(reason) {
  nativeWaiters.forEach(function (w) {
    clearTimeout(w.timer);
    w.resolve({ ok: false, __native: true, error: reason });
  });
  nativeWaiters.clear();
  nativePort = null;
}

function ensureNative() {
  if (nativePort) return nativePort;
  if (nativeState === 'missing') return null;
  let p;
  try {
    p = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (e) {
    nativeState = 'missing';
    return null;
  }
  p.onMessage.addListener(function (m) {
    const id = m && m.__id;
    const w = (id != null) ? nativeWaiters.get(id) : null;
    if (!w) return;
    clearTimeout(w.timer);
    nativeWaiters.delete(id);
    w.resolve(m);
  });
  p.onDisconnect.addListener(function () {
    const err = chrome.runtime.lastError;
    const why = (err && err.message) ? err.message : '原生通道已断开';
    // 「host not found」= 没跑注册脚本，标记后不再重试，直接走 HTTP
    if (err && /not found|not registered|not allowed|Access to the specified native/i.test(why)) {
      nativeState = 'missing';
    }
    nativeDisconnectAll(why);
  });
  nativePort = p;
  nativeState = 'ok';
  return p;
}

/** 发一条消息给原生宿主。通道层面失败时返回带 __native 标记的对象。 */
function nativeCall(msg, timeoutMs) {
  return new Promise(function (resolve) {
    let p = null;
    try { p = ensureNative(); } catch (e) { /* 落到下面统一处理 */ }
    if (!p) {
      resolve({ ok: false, __native: true, error: '原生通道未注册（请运行 web_bridge_native_setup.py）' });
      return;
    }
    const id = ++nativeSeq;
    const timer = setTimeout(function () {
      nativeWaiters.delete(id);
      resolve({ ok: false, __native: true, error: '原生通道超时' });
    }, timeoutMs || NATIVE_TIMEOUT);
    nativeWaiters.set(id, { resolve: resolve, timer: timer });
    try {
      const out = Object.assign({ __id: id }, msg);
      p.postMessage(out);
    } catch (e) {
      clearTimeout(timer);
      nativeWaiters.delete(id);
      resolve({ ok: false, __native: true, error: 'postMessage 失败：' + ((e && e.message) || e) });
    }
  });
}

async function postImport(port, payload) {
  const res = await withTimeout(
    fetch('http://127.0.0.1:' + port + '/import', {
      method: 'POST',
      headers: HEADERS,
      body: JSON.stringify(payload || {})
    }), POST_TIMEOUT);
  let data = null;
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    return { ok: false, error: (data && data.error) || ('HTTP ' + res.status) };
  }
  return data || { ok: false, error: '空响应' };
}

async function getTask(port, id) {
  // 先试原生通道。注意：原生模式下 import 响应里也会带回真实 port，
  // 所以不能靠「port 是否为 0」来判断走哪条路 —— 之前就是这么漏的：
  // port 有值 → 走 HTTP fetch → 被代理/VPN 拦掉 → /task 一次都到不了
  // 软件端，前端就一直停在「正在导入 0/419 张…」永远不动。
  const n = await nativeCall({ cmd: 'task', id: id }, 10000);
  if (n && !n.__native) return n.task || null;

  if (!port) {
    const r = await findPort();
    port = r.port;
    if (!port) return null;
  }
  const res = await withTimeout(
    fetch('http://127.0.0.1:' + port + '/task?id=' + encodeURIComponent(id), {
      cache: 'no-store',
      headers: { 'X-Zhigu-Bridge': '1' }
    }), POST_TIMEOUT);
  if (!res.ok) return null;
  try { return await res.json(); } catch (e) { return null; }
}

// ------------------------------------------------------------------ Cookie（含 HttpOnly）

/**
 * 读某个页面来源的 Cookie，拼成可以直接当请求头用的字符串。
 *
 * 为什么必须在后台读：内网 PACS 的取图接口（Wado/GetBlock）依赖会话 Cookie，
 * 而会话 Cookie 基本都带 HttpOnly —— 页面里的 `document.cookie` 读不到。
 * 只把 document.cookie 交给软件端，下载端会拿到一页登录 HTML（还常常是 200），
 * 表现就是「一张都没下来」。
 *
 * 需要 "cookies" 权限；没授权或读失败时返回空，调用方继续用 document.cookie 那份。
 */
async function cookiesFor(urls) {
  const out = {};
  if (!chrome.cookies || !chrome.cookies.getAll) return out;
  const list = Array.isArray(urls) ? urls.slice(0, 6) : [];
  for (const u of list) {
    try {
      const got = await chrome.cookies.getAll({ url: u });
      if (!got || !got.length) continue;
      const parts = [];
      const seen = new Set();
      got.forEach(function (c) {
        if (!c || !c.name || seen.has(c.name)) return;
        seen.add(c.name);
        parts.push(c.name + '=' + (c.value == null ? '' : c.value));
      });
      if (parts.length) {
        let origin = u;
        try { origin = new URL(u).origin; } catch (e) { /* 保留原值 */ }
        out[origin] = parts.join('; ');
      }
    } catch (e) { /* 单个来源失败不影响其它 */ }
  }
  return out;
}

// ------------------------------------------------------------------ 跨帧清单

function stashPut(tabId, frameId, data) {
  if (tabId == null) return;
  let frames = STASH.get(tabId);
  if (!frames) { frames = new Map(); STASH.set(tabId, frames); }
  frames.set(frameId || 0, { at: Date.now(), data: data });
}

function stashGather(tabId) {
  const frames = STASH.get(tabId);
  if (!frames) return null;
  const now = Date.now();
  const seriesMap = new Map();
  let patient = '';
  let title = '';
  let diag = null;
  let srcUrl = '';
  let total = 0;

  frames.forEach(function (item) {
    if (now - item.at > STASH_TTL) return;
    const d = item.data;
    if (!d || !Array.isArray(d.series)) return;
    if (!patient && d.patient) patient = d.patient;
    if (!title && d.title) title = d.title;
    if (!diag && d.diag) diag = d.diag;
    if (!srcUrl && d.srcUrl) srcUrl = d.srcUrl;
    d.series.forEach(function (s) {
      if (!s || !Array.isArray(s.images) || !s.images.length) return;
      const key = s.key || ('f' + (s.desc || '') + s.images.length);
      const merged = seriesMap.get(key);
      if (merged) {
        const seen = new Set(merged.images.map(function (i) { return i.url; }));
        s.images.forEach(function (im) {
          if (im && im.url && !seen.has(im.url)) { merged.images.push(im); seen.add(im.url); }
        });
      } else {
        seriesMap.set(key, {
          key: key,
          desc: s.desc || '',
          modality: s.modality || '',
          images: s.images.slice()
        });
      }
    });
  });

  const series = Array.from(seriesMap.values());
  series.forEach(function (s) { total += s.images.length; });
  if (!series.length) return null;
  return { series: series, total: total, patient: patient, title: title,
           diag: diag, srcUrl: srcUrl };
}

// service worker 顶层任何一次抛错都会让整个 SW 注册失败，届时所有消息
// 静默失联（表现为 content 里 pickPort 永远返回 0）。tabs 事件是纯优化，
// 拿不到就算了，绝不能拖垮注册。
try {
  chrome.tabs.onRemoved.addListener(function (tabId) { STASH.delete(tabId); });
  chrome.tabs.onUpdated.addListener(function (tabId, info) {
    if (info && info.status === 'loading') STASH.delete(tabId);   // 导航换页，旧数据作废
  });
} catch (e) {
  console.warn('[zhigu] tabs 事件订阅失败（不影响取图）:', e && e.message);
}

// ------------------------------------------------------------------ 消息路由

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  const cmd = msg && msg.cmd;
  const tabId = sender && sender.tab ? sender.tab.id : null;
  const frameId = sender && sender.frameId != null ? sender.frameId : 0;

  (async function () {
    try {
      if (cmd === 'ping') {
        // 先试原生通道：它不受代理/VPN 影响，而且是本机管道，比 TCP 更稳
        const n = await nativeCall({ cmd: 'ping' }, 8000);
        if (n && !n.__native && n.ok && n.port) {
          sendResponse({ ok: true, port: n.port, tried: ['native'], info: n, via: 'native' });
          return;
        }
        const r = await findPort(msg.ports);
        sendResponse({ ok: !!r.port, port: r.port, tried: r.tried || [], info: r.info || null,
                       via: 'http', nativeError: n && n.error });
        return;
      }
      if (cmd === 'import') {
        const n = await nativeCall({ cmd: 'import', payload: msg.payload }, 25000);
        if (n && !n.__native) {
          sendResponse(n);          // 业务结果（成功或"软件未运行"）都直接透传
          return;
        }
        let port = msg.port || 0;
        let tried = [];
        if (!port) {
          const r = await findPort(msg.ports);
          port = r.port;
          tried = r.tried || [];
        }
        if (!port) {
          const why = (n && n.error) ? ('原生通道：' + n.error + '；') : '';
          sendResponse({ ok: false, error: why + '软件未运行（' + (tried.join('; ') || '无端口响应') + '）', tried: tried });
          return;
        }
        try {
          const result = await postImport(port, msg.payload);
          sendResponse(result || { ok: false, error: '空响应', tried: tried });
        } catch (e) {
          // 这里以前会直接把异常抛给 catch 分支，提示只有一句英文，
          // 现在带上端口和已试过的错误，用户一眼能看出卡在哪。
          sendResponse({ ok: false, error: 'POST 到 ' + port + ' 失败：' +
                         String((e && e.message) || e), tried: tried, port: port });
        }
        return;
      }
      if (cmd === 'task') {
        const n = await nativeCall({ cmd: 'task', id: msg.id }, 12000);
        if (n && !n.__native) {
          sendResponse(n);
          return;
        }
        const data = await getTask(msg.port, msg.id);
        sendResponse(data || {});
        return;
      }
      if (cmd === 'stash') {
        stashPut(tabId, frameId, msg.data);
        sendResponse({ ok: true });
        return;
      }
      if (cmd === 'gather') {
        sendResponse({ ok: true, data: stashGather(tabId) });
        return;
      }
      if (cmd === 'cookies') {
        const map = await cookiesFor(msg.urls);
        sendResponse({ ok: true, map: map });
        return;
      }
      if (cmd === 'frameReady') {
        // 子帧有数据了 → 叫醒顶层帧（临床系统里按钮在顶层、viewer 在 iframe）。
        // 不能靠顶层轮询：那会让每个 PACS 页面都定期打后台。
        if (tabId == null) { sendResponse({ ok: false }); return; }
        try {
          chrome.tabs.sendMessage(tabId, { cmd: 'frameReady' }, { frameId: 0 }, function () {
            void chrome.runtime.lastError;
          });
        } catch (e) { /* 顶层帧没有 content script 时忽略 */ }
        sendResponse({ ok: true });
        return;
      }
      sendResponse({ ok: false, error: 'unknown cmd' });
    } catch (err) {
      sendResponse({ ok: false, error: String((err && err.message) || err) });
    }
  })();

  return true;      // 保持通道，异步 sendResponse
});

// 点工具栏图标 = 点页面上的按钮
try {
  chrome.action.onClicked.addListener(function (tab) {
    if (!tab || !tab.id) return;
    chrome.tabs.sendMessage(tab.id, { cmd: 'click' }, function () {
      void chrome.runtime.lastError;
    });
  });
} catch (e) {
  console.warn('[zhigu] action 订阅失败（不影响页面按钮）:', e && e.message);
}
