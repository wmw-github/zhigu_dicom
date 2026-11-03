/**
 * background.js —— 下载调度层 + 跨帧清单缓存。
 *
 * 与「知骨影像 · 网页取图」插件的**根本区别**：
 *   那个插件把「URL 清单」发给本机知骨软件，由软件自己下载；
 *   这个插件不依赖任何本机软件 —— 后台自己并发下载，用 chrome.downloads
 *   把每一张图直接落盘到系统「下载」目录，按「日期_患者/序列」分文件夹。
 *
 * 为什么不 fetch 完直接发大 JSON 给 content 存：
 *   一个 CT 300~500 张 × 200KB 全塞内存会爆，必须一张一下载一张一落盘，
 *   并发 4 路（与知骨软件端一致的口径），进度逐张推给页面按钮显示。
 */

// ------------------------------------------------------------------ 跨帧清单

const STASH_TTL = 5 * 60 * 1000;
// tabId -> { frameId -> { at, data } }
const STASH = new Map();

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
// 静默失联。tabs 事件是纯优化，拿不到就算了，绝不能拖垮注册。
try {
  chrome.tabs.onRemoved.addListener(function (tabId) { STASH.delete(tabId); });
  chrome.tabs.onUpdated.addListener(function (tabId, info) {
    if (info && info.status === 'loading') STASH.delete(tabId);   // 导航换页，旧数据作废
  });
} catch (e) {
  console.warn('[zgsave] tabs 事件订阅失败（不影响取图）:', e && e.message);
}

// ------------------------------------------------------------------ Cookie（含 HttpOnly）

/**
 * 读某个页面来源的 Cookie，拼成可以直接当请求头用的字符串。
 *
 * 为什么必须在后台读：内网 PACS 的取图接口（Wado/GetBlock）依赖会话 Cookie，
 * 而会话 Cookie 基本都带 HttpOnly —— 页面里的 `document.cookie` 读不到。
 * 只用 document.cookie 去下载，会拿到一页登录 HTML（还常常是 200），
 * 表现就是「一张都没存下来」。
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

// ------------------------------------------------------------------ 文件名清洗

/** Windows 文件名非法字符 + 首尾点空格。 */
function safeName(s, fallback) {
  let t = String(s == null ? '' : s).trim();
  if (!t) return fallback;
  t = t.replace(/[\\/:*?"<>|]/g, '_').replace(/[\x00-\x1f]/g, '');
  t = t.replace(/\s+/g, ' ').trim();
  t = t.replace(/[.\s]+$/, '');           // Windows 不允许文件名以点/空格结尾
  if (!t) return fallback;
  return t.slice(0, 60);
}

/** 从 URL 里取末段当文件名，去掉 query。 */
function nameFromUrl(u, idx) {
  let last = '';
  try {
    const segs = new URL(u).pathname.split('/').filter(Boolean);
    if (segs.length) last = decodeURIComponent(segs[segs.length - 1]);
  } catch (e) { /* ignore */ }
  if (!last || !/^[0-9a-zA-Z._-]{1,80}$/.test(last)) {
    last = 'img_' + String(idx + 1).padStart(4, '0');
  }
  return last;
}

// ------------------------------------------------------------------ 下载任务

const JOBS = new Map();      // tabId -> job（同一页可反复点，新任务顶掉旧状态显示）
const MAX_CONC = 4;          // 与知骨软件端一致的并发数：更快会把内网老服务器打挂
let jobSeq = 0;

function guessExt(type, it) {
  if (/dcm|dicom/i.test(type || '')) return '.dcm';
  if (/jpe?g/i.test(type || '')) return '.jpg';
  if (/png/i.test(type || '')) return '.png';
  if (/bmp/i.test(type || '')) return '.bmp';
  return /\.dcm/i.test(String(it && it.name || '')) ? '' : '.dcm';
}

function pushProgress(job) {
  if (job.tabId == null) return;
  // 定向发给发起另存的那个标签页：多开几个 PACS 页面时互不串进度。
  try {
    chrome.tabs.sendMessage(job.tabId, {
      cmd: 'progress',
      done: job.done, failed: job.failed, total: job.total,
      state: job.state
    }, function () { void chrome.runtime.lastError; /* 页面已关，忽略 */ });
  } catch (e) { /* ignore */ }
}

/**
 * 内容预检：HEAD 一次看 content-type，挡掉「SPA 对拼错路径返回 200+HTML」。
 * MV3 的 Service Worker 没有 URL.createObjectURL（规范禁止），所以整条
 * 「fetch→blob→download」链路在 SW 里根本走不通（v1.0.2 的
 * "URL.createObjectURL is not a function" 就是这个原因），只能把原始
 * https 地址直接交给 chrome.downloads，让浏览器下载引擎去拉。
 * HEAD 被拒（403/405）或 HEAD 本身失败时不拦，把判断交给下载结果。
 */
async function typeGuard(url) {
  try {
    const resp = await fetch(url, { method: 'HEAD', cache: 'no-store' });
    if (!resp.ok) return { ok: true, type: '' };
    const type = (resp.headers.get('content-type') || '').toLowerCase();
    if (/text\/html|application\/json/i.test(type)) {
      return { ok: false, type: type.split(';')[0] || 'html' };
    }
    return { ok: true, type: type };
  } catch (e) {
    return { ok: true, type: '' };
  }
}

/** 把原始 URL 直接交给浏览器下载引擎落盘。 */
function downloadDirect(url, finalPath) {
  return new Promise(function (resolve) {
    let doneFlag = false;
    try {
      chrome.downloads.download({
        url: url,
        filename: finalPath,          // 相对「下载」目录，可含子目录
        conflictAction: 'uniquify',   // 重名自动加 (1)，不覆盖旧数据
        saveAs: false
      }, function (id) {
        const err = chrome.runtime.lastError;
        if (err || !id) {
          if (!doneFlag) { doneFlag = true; resolve({ ok: false, error: (err && err.message) || 'download 返回空 id' }); }
          return;
        }
        if (!doneFlag) { doneFlag = true; resolve({ ok: true, id: id }); }
      });
    } catch (e) {
      if (!doneFlag) { doneFlag = true; resolve({ ok: false, error: String((e && e.message) || e) }); }
    }
  });
}

/**
 * 等一次 download 真正落盘。onChanged 监听 + 500ms 轮询双保险
 * （MV3 SW 休眠时事件可能丢），超时按失败报。
 */
function waitDownload(id, timeoutMs) {
  return new Promise(function (resolve) {
    let settled = false;
    let timer = null;
    const finish = function (r) {
      if (settled) return;
      settled = true;
      try { chrome.downloads.onChanged.removeListener(onCh); } catch (e) { /* ignore */ }
      if (timer) clearInterval(timer);
      clearTimeout(quit);
      resolve(r);
    };
    const onCh = function (delta) {
      if (!delta || delta.id !== id || !delta.state) return;
      if (delta.state.current === 'complete') finish({ ok: true });
      else if (delta.state.current === 'interrupted') {
        const why = delta.error && delta.error.current ? delta.error.current : 'interrupted';
        finish({ ok: false, error: why });
      }
    };
    try { chrome.downloads.onChanged.addListener(onCh); } catch (e) { /* ignore */ }
    timer = setInterval(function () {
      try {
        chrome.downloads.search({ id: id }, function (items) {
          const it = items && items[0];
          if (!it) return;
          if (it.state === 'complete') finish({ ok: true });
          else if (it.state === 'interrupted') finish({ ok: false, error: it.error || 'interrupted' });
        });
      } catch (e) { /* ignore */ }
    }, 500);
    const quit = setTimeout(function () {
      finish({ ok: false, error: '超时（' + Math.round(timeoutMs / 1000) + ' 秒未完成）' });
    }, timeoutMs);
  });
}

/**
 * 跑一个下载任务：4 路并发消费队列，逐张 fetch + 落盘，
 * 每张完成后向页面推进度。所有失败都记进 job，绝不向上抛。
 */
async function runJob(job) {
  let cursor = 0;
  const workers = [];

  async function one() {
    while (cursor < job.images.length) {
      if (job.cancel) return;
      const idx = cursor++;
      const it = job.images[idx];
      if (!it || !it.url) { job.failed++; continue; }
      let fname = it.name || nameFromUrl(it.url, idx);
      fname = String(fname).split(/[\\/]/).pop() || nameFromUrl(it.url, idx);
      const rel = job.folder + '/' + it.sub + '/' +
                  safeName(fname, 'img_' + String(idx + 1).padStart(4, '0'));
      let did = false;
      let failReason = '';
      // 主地址 + 备选地址一起排成候选队列，逐个试：HEAD 预检 → 直接下载 → 等落盘。
      const cands = [it.url].concat(Array.isArray(it.alt) ? it.alt : []);
      for (let c = 0; c < cands.length && !did; c++) {
        const cu = cands[c];
        if (!cu) continue;
        try {
          const g = await typeGuard(cu);
          if (!g.ok) {
            failReason = '内容异常（' + g.type + '，不是影像文件）';
            continue;
          }
          // 下载引擎不看 content-type，没扩展名时按预检结果补一个
          let rel2 = rel;
          if (!/\.(dcm|jpe?g|png|bmp)$/i.test(rel2)) rel2 += guessExt(g.type, it);
          const dl = await downloadDirect(cu, rel2);
          if (!dl.ok) { failReason = '下载发起失败 ' + dl.error; continue; }
          const w = await waitDownload(dl.id, 120000);
          if (w.ok) {
            did = true;
          } else {
            failReason = '下载中断 ' + w.error;
          }
        } catch (e) {
          failReason = '异常 ' + String((e && e.message) || e);
        }
      }
      if (!did) {
        job.failed++;
        if (failReason) job.errors.push(failReason + ' ' + String(it.url).slice(0, 120));
      }
      if (did) job.done++;
      pushProgress(job);
      await sleep(0);              // 让出事件循环，避免饿死消息通道
    }
  }

  for (let i = 0; i < MAX_CONC; i++) workers.push(one());
  await Promise.all(workers);
  job.state = job.cancel ? 'cancelled' : 'done';
  pushProgress(job);
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

/** 发起一次另存任务。content 把合并好的清单发来，这里直接开跑。 */
async function startDownload(tabId, payload) {
  // 逐 origin 读 Cookie：影像和页面不一定同一台主机，混着拼是错的。
  const origins = [];
  (payload.series || []).forEach(function (s) {
    (s.images || []).forEach(function (im) {
      if (!im || !im.url || origins.length >= 6) return;
      try {
        const o = new URL(im.url).origin;
        if (origins.indexOf(o) < 0) origins.push(o);
      } catch (e) { /* ignore */ }
    });
  });
  void origins;   // 下载引擎自动带会话 Cookie，不再手动读（保留收集逻辑便于日后扩展）

  const stamp = new Date();
  const pad = function (n) { return String(n).padStart(2, '0'); };
  const dateDir = stamp.getFullYear() + pad(stamp.getMonth() + 1) + pad(stamp.getDate()) +
                  '_' + pad(stamp.getHours()) + pad(stamp.getMinutes());
  const patient = safeName(payload.patient || '', '未命名患者');
  const root = '知骨另存/' + dateDir + '_' + patient;

  const items = [];
  let sIdx = 0;
  (payload.series || []).forEach(function (s) {
    if (!s || !Array.isArray(s.images) || !s.images.length) return;
    sIdx += 1;
    const desc = safeName(s.desc || '', '');
    const mod = safeName(s.modality || '', '');
    let sub = ((mod ? mod + '_' : '') + (desc ? desc + '_' : '') +
               String(sIdx).padStart(2, '0'));
    sub = safeName(sub, 'series_' + sIdx);
    s.images.forEach(function (im) {
      if (!im || !im.url) return;
      // 注意：chrome.downloads 不接受自定义头，也带不了手动 Cookie——
      // 但下载引擎会自动附上该 origin 的会话 Cookie（签名地址本身自鉴权）。
      items.push({
        url: im.url,
        alt: im.alt || [],
        name: im.name || '',
        sub: sub
      });
    });
  });

  if (!items.length) {
    return { ok: false, error: '清单里没有可下载的地址' };
  }

  const job = {
    id: ++jobSeq, tabId: tabId, state: 'running',
    folder: root, images: items,
    total: items.length, done: 0, failed: 0, errors: [],
    cancel: false, startedAt: Date.now()
  };
  JOBS.set(tabId, job);
  runJob(job);                     // 不 await：任务后台跑，进度走消息推
  return { ok: true, taskId: job.id, total: job.total, folder: root };
}

// ------------------------------------------------------------------ 消息路由

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  const cmd = msg && msg.cmd;
  const tabId = sender && sender.tab ? sender.tab.id : null;
  const frameId = sender && sender.frameId != null ? sender.frameId : 0;

  (async function () {
    try {
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
        if (tabId == null) { sendResponse({ ok: false }); return; }
        try {
          chrome.tabs.sendMessage(tabId, { cmd: 'frameReady' }, { frameId: 0 }, function () {
            void chrome.runtime.lastError;
          });
        } catch (e) { /* 顶层帧没有 content script 时忽略 */ }
        sendResponse({ ok: true });
        return;
      }
      if (cmd === 'save') {
        const r = await startDownload(tabId, msg.payload || {});
        sendResponse(r);
        return;
      }
      if (cmd === 'jobInfo') {
        // 诊断面板用：本标签页最近一次任务的下载明细——选了哪个地址、
        // 每条失败原因（403 / 空响应 / 写盘失败…）。没有任务就如实说。
        const job = tabId != null ? JOBS.get(tabId) : null;
        if (!job) { sendResponse({ ok: true, job: null }); return; }
        sendResponse({
          ok: true,
          job: {
            state: job.state,
            total: job.total,
            done: job.done,
            failed: job.failed,
            folder: job.folder,
            errors: (job.errors || []).slice(0, 8),
            // 首选地址（截前 3 条完整展示，能看出签名有没有被带上）
            urls: job.images.slice(0, 3).map(function (it) { return it.url; })
          }
        });
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
  console.warn('[zgsave] action 订阅失败（不影响页面按钮）:', e && e.message);
}
