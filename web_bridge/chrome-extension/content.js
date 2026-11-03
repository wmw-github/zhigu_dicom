/**
 * content.js —— 运行在扩展隔离世界。
 *
 * 职责四件：
 *   1. 在影像页面上画一个浮动按钮（Shadow DOM 隔离，避免被 PACS 的全局 CSS 污染）
 *   2. 向主世界要影像清单（postMessage 与 inject.js 通信）
 *   3. 调后台 service worker 把清单推给本机软件，并轮询进度
 *   4. 双击按钮弹诊断面板（抓不到图时用它定位，不用开 F12）
 *
 * 只在顶层帧画按钮；子帧只上报数据，避免 iframe 里冒出好几个按钮。
 *
 * 与软件的数据约定见 web_bridge.py 顶部注释：只传"取数凭据"，不传像素。
 */
(function () {
  'use strict';
  if (window.__zhiguContentInstalled) return;
  window.__zhiguContentInstalled = true;

  var PORTS = [18923, 18924, 18925, 18926, 18927, 18928, 18929, 18930];
  var READY_TOTAL = 0;
  var BUSY = false;
  var LAST_DIAG = null;
  var IS_TOP = (window === window.top);

  // ---------------------------------------------------------------- UI

  var hostEl = null, shadow = null, btnEl = null, txEl = null, dotEl = null, panelEl = null;

  function looksLikePacs() {
    var probe = (location.host + location.pathname).toLowerCase();
    if (/dimage|webpacs|pacs|viewer|cloudimage|webdicom|dicomweb|imageviewer/.test(probe)) {
      return true;
    }
    if (/影像|PACS|放射|放射科|报告/.test(document.title || '')) return true;

    // 内网临床系统：按钮所在的**顶层页**本身长得不像影像页（标题「临床系统」，
    // 路径 /content?reportId=…），真正的 viewer 在跨端口的 iframe 里（8989）。
    // 只按 URL/标题判断的话，医生在最常用的那个页面上根本看不到按钮 ——
    // 数据其实一直在子帧里，只是没人去汇总。
    try {
      if (document.getElementById('dicomview') || document.querySelector('.cloudFilm')) {
        return true;
      }
      var fs = document.getElementsByTagName('iframe');
      for (var i = 0; i < fs.length && i < 12; i++) {
        var src = String(fs[i].src || fs[i].getAttribute('src') || '');
        if (/viewer|wado|dicom|pacs|image/i.test(src)) return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  function ensureUI() {
    if (!IS_TOP || hostEl || !document.body) return;
    hostEl = document.createElement('div');
    hostEl.setAttribute('data-zhigu', 'host');
    hostEl.style.cssText = 'position:fixed;right:16px;bottom:88px;z-index:2147483647;';
    shadow = hostEl.attachShadow({ mode: 'open' });
    shadow.innerHTML = [
      '<style>',
      ':host{all:initial}',
      '.btn{display:flex;align-items:center;gap:8px;padding:9px 16px 9px 12px;',
      'border:0;border-radius:22px;cursor:pointer;font:600 13px/1.2 "Microsoft YaHei",system-ui,sans-serif;',
      'color:#fff;background:linear-gradient(135deg,#1160d6,#0a8ec9);',
      'box-shadow:0 4px 14px rgba(10,80,160,.42);transition:transform .12s,filter .12s;',
      'letter-spacing:.3px;white-space:nowrap}',
      '.btn:hover{filter:brightness(1.1);transform:translateY(-1px)}',
      '.btn:active{transform:translateY(0);filter:brightness(.95)}',
      '.btn[disabled]{cursor:default;filter:grayscale(.35) brightness(.9)}',
      // 状态文字可能很长（错误原因、进度），限宽省略，避免按钮撑到遮住影像
      '.tx{max-width:232px;overflow:hidden;text-overflow:ellipsis}',
      '.dot{width:8px;height:8px;border-radius:50%;background:#5df2a8;flex:0 0 auto;',
      'box-shadow:0 0 6px #5df2a8}',
      '.dot.warn{background:#ffcf5c;box-shadow:0 0 6px #ffcf5c}',
      '.dot.busy{background:#8fd0ff;box-shadow:0 0 6px #8fd0ff;animation:pz 1s infinite}',
      '.dot.err{background:#ff6b6b;box-shadow:0 0 6px #ff6b6b}',
      '@keyframes pz{0%,100%{opacity:1}50%{opacity:.25}}',
      '.panel{display:none;position:absolute;right:0;bottom:46px;width:440px;max-height:60vh;',
      'overflow:auto;padding:12px 14px;border-radius:10px;background:#1b1f27;color:#dfe6f0;',
      'font:12px/1.55 Consolas,"Microsoft YaHei",monospace;box-shadow:0 8px 28px rgba(0,0,0,.45);',
      'white-space:pre-wrap;word-break:break-all}',
      '.panel.on{display:block}',
      '.panel b{color:#7ec4ff}',
      '.panel .hint{color:#8b97a8}',
      '.row{display:flex;gap:8px;margin-top:10px}',
      '.mini{padding:4px 10px;border:1px solid #3a4556;border-radius:6px;background:#242b36;',
      'color:#cfe0f5;cursor:pointer;font:12px/1.4 inherit}',
      '.mini:hover{background:#2e3745}',
      '</style>',
      '<div class="panel"></div>',
      '<button class="btn" type="button"><span class="dot"></span>',
      '<span class="tx">用知骨打开</span></button>'
    ].join('');
    document.body.appendChild(hostEl);
    btnEl = shadow.querySelector('.btn');
    txEl = shadow.querySelector('.tx');
    dotEl = shadow.querySelector('.dot');
    panelEl = shadow.querySelector('.panel');

    // 单击 = 导入；双击 = 诊断。靠 240ms 迟滞区分，不会互相串。
    var clickTimer = null;
    btnEl.addEventListener('click', function () {
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
        toggleDiag();
        return;
      }
      clickTimer = setTimeout(function () {
        clickTimer = null;
        onClick();
      }, 240);
    });
  }

  function setState(text, kind) {
    ensureUI();
    if (!txEl) return;
    txEl.textContent = text;
    dotEl.className = 'dot' + (kind ? ' ' + kind : '');
  }

  function show() {
    ensureUI();
    if (hostEl) hostEl.style.display = '';
  }

  function fail(msg, hint) {
    BUSY = false;
    if (btnEl) btnEl.disabled = false;
    // 软件端给的失败原因常有近百字（含「请翻一遍图再点」这类可操作建议）。
    // 整段塞进按钮会把按钮撑爆、把页面布局顶歪，所以按钮只放短句，
    // 全文丢进诊断面板，用户点「查看诊断」就能看全。
    var full = String(msg == null ? '导入失败' : msg);
    var brief = full.replace(/\s+/g, ' ').trim();
    if (brief.length > 16) {
      if (!hint) hint = full;
      brief = brief.slice(0, 15) + '…';
    }
    setState(brief, 'err');
    if (hint) showDiagHint(hint);
    setTimeout(function () {
      if (!BUSY) setState(READY_TOTAL ? ('用知骨打开 · ' + READY_TOTAL + ' 张') : '用知骨打开', '');
    }, 4600);
  }

  // ---------------------------------------------------------------- 与主世界通信

  function collect() {
    return new Promise(function (resolve) {
      var done = false;
      // 两份 inject.js 都会回（主世界 + 隔离世界），必须认主世界那份：
      // 隔离世界的钩子拦不到页面请求，回的是空数据，让它抢先回就把真结果盖掉了。
      // 表现是「换台电脑点按钮没反应 / 0 张」，且没有任何报错。
      var fallback = null;

      function finish(payload) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        window.removeEventListener('message', onMsg);
        resolve(payload);
      }

      var timer = setTimeout(function () {
        finish(fallback);          // 主世界没回就退而求其次，至少带回诊断
      }, 2000);

      function onMsg(ev) {
        if (ev.source !== window) return;
        var d = ev.data;
        if (!d || d.__zhigu !== 'collected') return;
        var payload = d.data || null;
        if (payload && payload.world === 'main') {
          finish(payload);
          return;
        }
        if (!fallback) fallback = payload;
      }

      window.addEventListener('message', onMsg);
      try {
        window.postMessage({ __zhigu: 'collect' }, '*');
      } catch (e) {
        clearTimeout(timer);
        resolve(null);
      }
    });
  }

  // 向主世界要一份诊断（钩子是否装上、拦了多少响应）
  function diagnose() {
    return new Promise(function (resolve) {
      var done = false;
      // 与 collect 同理：只有主世界那份的钩子是真的，隔离世界那份会把
      // 「抓到 0 条」当成正常情况报出来，把排查方向整个带偏。
      var fallback = null;

      function finish(payload) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        window.removeEventListener('message', onMsg);
        if (payload) LAST_DIAG = payload;
        resolve(LAST_DIAG || null);
      }

      var timer = setTimeout(function () {
        finish(fallback);
      }, 1200);

      function onMsg(ev) {
        if (ev.source !== window) return;
        var d = ev.data;
        if (!d || d.__zhigu !== 'diagResult') return;
        var payload = d.data || null;
        if (payload && payload.world === 'main') {
          finish(payload);
          return;
        }
        if (!fallback) fallback = payload;
      }

      window.addEventListener('message', onMsg);
      try {
        window.postMessage({ __zhigu: 'diag' }, '*');
      } catch (e) {
        clearTimeout(timer);
        resolve(null);
      }
    });
  }

  // 主世界一收到数据就通知，用来点亮按钮上的张数
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data) return;
    if (ev.data.__zhigu === 'ready') {
      READY_TOTAL = ev.data.total || 0;
      if (IS_TOP) {
        if (!BUSY) {
          show();
          setState('用知骨打开 · ' + READY_TOTAL + ' 张', '');
        }
      } else {
        // 子帧有数据：赶紧存到后台，等着顶层帧来取
        collect().then(function (d) {
          if (d && d.total) {
            bg({ cmd: 'stash', data: d });
            // 同时叫醒顶层帧：临床系统里按钮在顶层、数据在 iframe，
            // 不主动通报的话顶层永远不知道子帧已经有图了（按钮就一直
            // 停在「用知骨打开」不带张数，医生以为没准备好）。
            bg({ cmd: 'frameReady' });
          }
        });
      }
    }
  });

  // ---------------------------------------------------------------- 后台 RPC

  var LAST_BG_ERR = '';
  var LAST_PING = null;

  function bg(msg) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage(msg, function (resp) {
          if (chrome.runtime.lastError) {
            LAST_BG_ERR = chrome.runtime.lastError.message || '未知错误';
            resolve(null);
            return;
          }
          LAST_BG_ERR = '';
          resolve(resp || null);
        });
      } catch (e) {
        // 扩展被重载后旧脚本会抛 "context invalidated"，用户刷新页面即可
        LAST_BG_ERR = '扩展上下文已失效（请刷新页面）：' + ((e && e.message) || e);
        resolve(null);
      }
    });
  }

  function pickPort() {
    return bg({ cmd: 'ping', ports: PORTS }).then(function (r) {
      LAST_PING = r || null;
      return (r && r.port) || 0;
    });
  }

  /** 把最近一次探测结果翻译成人话，诊断面板直接用 */
  function linkText() {
    if (!LAST_PING) {
      return '未探测' + (LAST_BG_ERR ? '（后台未响应：' + LAST_BG_ERR + '）' : '（后台无响应）');
    }
    if (LAST_PING.port) {
      var bound = LAST_PING.info && LAST_PING.info.bound;
      var via = LAST_PING.via === 'native' ? '原生通道' : 'HTTP 回环';
      return '已连接 127.0.0.1:' + LAST_PING.port +
             '（' + via + '，' + (bound ? '主窗口就绪' : '主窗口未就绪') + '）';
    }
    var t = (LAST_PING.tried || []).join('; ');
    return '未连接' + (t ? '：' + t : '：无响应');
  }

  /**
   * 从页面上下文直接探一次 127.0.0.1，只判断「网络层通不通」。
   *
   * 用 no-cors：读不到响应内容，但 fetch 能 resolve 就说明 TCP 通了。
   * 这一条能把「软件没监听」和「扩展后台被拦」彻底分开 ——
   * 两者在按钮上都表现成"连不上"，但修法完全不同。
   */
  function probeDirect() {
    var ports = [18923, 18924];
    return new Promise(function (resolve) {
      var i = 0;
      function next() {
        if (i >= ports.length) { resolve('不可达（端口没监听，或本机代理拦截）'); return; }
        var p = ports[i++];
        var done = false;
        var timer = setTimeout(function () {
          if (done) return;
          done = true;
          next();
        }, 1800);
        fetch('http://127.0.0.1:' + p + '/ping', { mode: 'no-cors', cache: 'no-store' })
          .then(function () {
            if (done) return;
            done = true;
            clearTimeout(timer);
            resolve('可达 :' + p + '（页面直连）');
          }, function (e) {
            if (done) return;
            done = true;
            clearTimeout(timer);
            next();
          });
      }
      next();
    });
  }

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function waitPort(timeoutMs) {
    var deadline = Date.now() + timeoutMs;
    return (function loop() {
      return pickPort().then(function (p) {
        if (p) return p;
        if (Date.now() > deadline) return 0;
        return sleep(700).then(loop);
      });
    })();
  }

  // ---------------------------------------------------------------- 清单合并

  function mergeData(remote, local) {
    var out = { series: [], total: 0, patient: '', title: '', diag: null,
                cookies: '', srcUrl: '' };
    var byKey = Object.create(null);

    function push(src) {
      if (!src || !Array.isArray(src.series)) return;
      if (!out.patient && src.patient) out.patient = src.patient;
      if (!out.title && src.title) out.title = src.title;
      if (!out.diag && src.diag) out.diag = src.diag;
      // cookies 必须带下去：下载端没有它，签名 URL 之外的老部署会直接 403
      if (!out.cookies && src.cookies) out.cookies = src.cookies;
      // srcUrl 是「采集到这份数据的那个帧」的地址。临床系统把 viewer 放在
      // 跨端口 iframe 里，顶层帧地址当 Referer 是错的 —— 必须是子帧那个。
      if (!out.srcUrl && src.srcUrl) out.srcUrl = src.srcUrl;
      src.series.forEach(function (s) {
        if (!s || !Array.isArray(s.images) || !s.images.length) return;
        var key = s.key || ('d' + (s.desc || '') + s.images.length);
        var t = byKey[key];
        if (!t) {
          t = byKey[key] = { key: key, desc: s.desc || '', modality: s.modality || '', images: [], seen: Object.create(null) };
          out.series.push(t);
        }
        s.images.forEach(function (im) {
          if (!im || !im.url || t.seen[im.url]) return;
          t.seen[im.url] = 1;
          t.images.push(im);
        });
      });
    }

    push(remote);
    push(local);          // 本帧最新，补充/覆盖

    out.series.forEach(function (s) { delete s.seen; out.total += s.images.length; });
    out.series = out.series.filter(function (s) { return s.images.length > 0; });
    return out;
  }

  // ---------------------------------------------------------------- 唤起软件

  function launchApp() {
    var url = 'zhigu://import?u=' + encodeURIComponent(location.href);
    try {
      var f = document.createElement('iframe');
      f.style.display = 'none';
      f.src = url;
      (document.body || document.documentElement).appendChild(f);
      setTimeout(function () { try { f.parentNode && f.parentNode.removeChild(f); } catch (e) {} }, 5000);
    } catch (e) { /* ignore */ }
    setTimeout(function () {
      pickPort().then(function (p) {
        if (p) return;
        try { window.open(url, '_blank', 'noopener,noreferrer'); } catch (e) { /* ignore */ }
      });
    }, 1200);
  }

  // ---------------------------------------------------------------- 主流程

  /**
   * 问后台要影像所在主机的 Cookie（含 HttpOnly）。
   *
   * 为什么非要做这一步：内网 PACS 的取图接口（Wado/GetBlock）跟站点同源，
   * 靠的就是会话 Cookie；而会话 Cookie 绝大多数是 HttpOnly 的，
   * `document.cookie` 读不到 —— 只能下发给软件端的 Cookie 头是空的，
   * 下载端就会拿到一页登录 HTML（还常常是 200），表现为"全部失败"。
   * 没有 cookies 权限或读取失败时返回空对象，下载端仍会用到 document.cookie 那份。
   */
  function collectCookies(data) {
    var origins = [];
    (data.series || []).forEach(function (s) {
      (s.images || []).forEach(function (im) {
        if (!im || !im.url || origins.length >= 4) return;
        try {
          var o = new URL(im.url, location.href).origin;
          if (origins.indexOf(o) < 0) origins.push(o);
        } catch (e) { /* ignore */ }
      });
    });
    if (!origins.length) return Promise.resolve({});
    return bg({ cmd: 'cookies', urls: origins }).then(function (r) {
      return (r && r.map) || {};
    });
  }

  function buildPayload(data, cookieMap) {
    return {
      pageUrl: location.href,
      pageTitle: data.title || document.title || '',
      patient: data.patient || '',
      cookies: data.cookies || '',
      // 分主机 Cookie：内网 PACS 的影像和页面不一定同一个端口/域名
      cookieMap: cookieMap || {},
      // Referer 用采集帧的地址（临床系统里就是 8989 的 viewer），不是顶层页
      referer: data.srcUrl || location.href,
      userAgent: navigator.userAgent,
      series: (data.series || []).map(function (s) {
        return {
          key: s.key,
          desc: s.desc || '',
          modality: s.modality || '',
          images: (s.images || []).map(function (im) {
            return { url: im.url, alt: im.alt || [], name: im.name || '' };
          })
        };
      })
    };
  }

  function pollTask(port, taskId) {
    // 15 分钟：一个 CT 300~500 张、每张 200KB 上下，医院出口带宽不高时
    // 6 路并发也要好几分钟。原来的 5 分钟会在正常下载中途误报「下载超时」。
    var deadline = Date.now() + 15 * 60 * 1000;
    var lastDone = -1;
    return (function loop() {
      return bg({ cmd: 'task', port: port, id: taskId }).then(function (r) {
        var t = r && r.task;
        if (!t) {
          if (Date.now() > deadline) return { state: 'error', message: '进度查询超时' };
          return sleep(900).then(loop);
        }
        if (t.state === 'running') {
          lastDone = t.done || 0;
          setState('正在导入 ' + lastDone + '/' + (t.total || 0) + ' 张…', 'busy');
          if (Date.now() > deadline) {
            return { state: 'error', message: '下载超时（已下载 ' + lastDone + ' 张）' };
          }
          return sleep(900).then(loop);
        }
        return t;
      });
    })();
  }

  function onClick() {
    if (BUSY) return;
    BUSY = true;
    btnEl.disabled = true;
    setState('正在收集影像…', 'busy');

    var collected = null;

    collect().then(function (local) {
      collected = local;
      if (local && local.total) {
        // 顺手存一份，跨 iframe 的站点要靠这个汇总
        return bg({ cmd: 'stash', data: local });
      }
      return null;
    }).then(function () {
      return bg({ cmd: 'gather' });
    }).then(function (r) {
      var data = mergeData(r && r.data, collected);

      if (!data.series.length) {
        BUSY = false;
        btnEl.disabled = false;
        // 抓不到时把诊断打出来：是钩子没装上，还是页面压根没下发地址
        return diagnose().then(function (diag) {
          handleEmpty(diag);
        });
      }

      // 清掉软件端可能残留的"上一份清单"提示
      setState('正在连接知骨影像…', 'busy');
      return pickPort().then(function (port) {
        if (port) return port;
        setState('正在启动知骨影像…', 'busy');
        launchApp();
        return waitPort(30000).then(function (p) {
          if (!p) fail('连不上知骨影像', '本机软件：' + linkText() + ' —— 确认软件已启动后，再点一次按钮');
          return p || null;
        });
      }).then(function (port) {
        if (!port) return null;
        setState('正在导入 ' + data.total + ' 张…', 'busy');
        return collectCookies(data).then(function (cookieMap) {
          return bg({ cmd: 'import', port: port, payload: buildPayload(data, cookieMap) });
        }).then(function (rr) {
            if (!rr || !rr.ok) {
              fail((rr && rr.error) || '导入失败，请查看软件目录 web_bridge.log');
              return null;
            }
            return pollTask(port, rr.taskId).then(function (t) {
              BUSY = false;
              btnEl.disabled = false;
              if (t && t.state === 'done') {
                var extra = t.failed ? ('（' + t.failed + ' 张失败）') : '';
                setState('已打开 ' + (t.total - (t.failed || 0)) + ' 张' + extra, '');
                setTimeout(function () {
                  if (!BUSY) setState('用知骨打开 · ' + READY_TOTAL + ' 张', '');
                }, 5000);
              } else {
                fail((t && t.message) || '导入失败');
              }
            });
          });
      });
    }).catch(function (err) {
      BUSY = false;
      btnEl.disabled = false;
      fail('出错：' + (err && err.message ? err.message : err));
    });
  }

  // ---------------------------------------------------------------- 诊断面板

  function handleEmpty(diag) {
    var responses = (diag && diag.responses) || 0;
    var ghosts = (diag && diag.ghosts) || 0;
    var seriesN = (diag && diag.series) ? diag.series.length : 0;

    var tip, hint;
    if (!diag) {
      tip = '没找到影像';
      hint = '钩子没有响应，页面脚本可能比扩展更早执行。请刷新页面后立刻再点。';
    } else if (responses === 0) {
      tip = '没找到影像';
      hint = '页面数据没有经过可拦截的入口（响应 0 条）。可能是解密在 Web Worker 里做的，请看诊断里的请求记录。';
    } else if (ghosts > 0) {
      tip = '页面没提供原始 DICOM';
      hint = '已识别 ' + ghosts + ' 张图像，但这个部署只下发了 JPEG 预览，没有 DICOM 地址。属于部署形态限制。';
    } else if (seriesN === 0) {
      // 关键分叉：页面到底有没有去请求影像接口。
      // 没请求 = 用户还停在列表页；请求了 = 响应结构不认识，需要样本。
      var reqs = (diag && diag.req) ? diag.req.join(' ') : '';
      var askedSeries = /series|instanc|\/image/i.test(reqs);
      tip = '没找到影像';
      hint = askedSeries
        ? '页面请求过影像接口，但响应里没有能识别的清单。把诊断里带 series/image 的那几条记录发我，我按真实结构改解析。'
        : '页面还没去请求影像清单。请点开一个具体序列、看到图像后再点按钮 —— 只停在检查/序列列表时前端没有数据。';
    } else {
      tip = '清单为空';
      hint = '识别到 ' + seriesN + ' 个序列，但没能提取出下载地址。请看诊断页的字段样本。';
    }

    // 短暂提示 + 常驻诊断入口
    setState(tip, 'err');
    setTimeout(function () {
      if (!BUSY) setState('用知骨打开', 'warn');
    }, 4600);
    showDiagHint(hint);
  }

  function showDiagHint(hint) {
    if (!IS_TOP) return;
    ensureUI();
    if (!panelEl) return;
    panelEl.innerHTML = [
      '<b>取图未完成</b>',
      '<span class="hint">' + esc(hint) + '</span>',
      '<div class="row">',
      '<button class="mini" data-act="diag">查看诊断</button>',
      '<button class="mini" data-act="close">关闭</button>',
      '</div>'
    ].join('\n');
    panelEl.classList.add('on');
    panelEl.querySelector('[data-act="diag"]').addEventListener('click', function () {
      renderDiag();
    });
    panelEl.querySelector('[data-act="close"]').addEventListener('click', function () {
      panelEl.classList.remove('on');
    });
  }

  function toggleDiag() {
    ensureUI();
    if (!panelEl) return;
    if (panelEl.classList.contains('on')) {
      panelEl.classList.remove('on');
      return;
    }
    renderDiag();
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderDiag() {
    ensureUI();
    if (!panelEl) return;
    panelEl.classList.add('on');
    panelEl.innerHTML = '<b>诊断中…</b>';

    var info = {
      page: location.href.slice(0, 120),
      readyTotal: READY_TOTAL
    };

    // 先探一次本机软件再取钩子状态：很多"点了没反应"其实是页面→软件这一跳
    // 断了，光看钩子永远是正常的，会把人往错误方向带。
    // 同时跑一条页面直连探测，用来区分「软件没监听」和「扩展后台被拦」。
    // 再顺手 gather 一次：临床系统里数据在子帧，顶层帧自己的钩子永远是空的，
    // 只看本帧诊断会把「子帧明明有 4 张」误报成「什么都没识别到」。
    Promise.all([pickPort(), probeDirect(), diagnose(), bg({ cmd: 'gather' })]).then(function (arr) {
      var direct = arr[1];
      var diag = arr[2];
      var remoteDiag = arr[3] && arr[3].data && arr[3].data.diag;
      var lines = [];
      lines.push('<b>知骨取图 · 诊断</b>');
      lines.push('页面      ：' + esc(info.page));
      lines.push('版本      ：' + esc((diag && diag.version) || '未响应'));
      lines.push('本机软件  ：' + esc(linkText()));
      lines.push('直连探测  ：' + esc(direct));
      if (remoteDiag && remoteDiag.wado && remoteDiag.wado.images) {
        var rw = remoteDiag.wado;
        lines.push('子帧 Wado ：序列 ' + rw.series + ' 个 / ' + rw.images + ' 张' +
                   (rw.real ? ('，真实地址 ' + rw.real + ' 条') : '') +
                   (rw.jpgOnly ? ' · 仅 JPEG 预览' : ''));
        if (rw.base) lines.push('  取图基址：' + esc(rw.base));
        if (rw.study) lines.push('  检查号  ：' + esc(rw.study) + '　院区 ' + esc(rw.hosp || '?'));
      }
      if (!diag) {
        lines.push('');
        lines.push('<b>主世界钩子没有响应</b>');
        lines.push('说明 inject.js 没在页面主世界执行。可能原因：');
        lines.push('1) Chrome 版本 < 111 且页面 CSP 拦掉了 script 注入');
        lines.push('2) 扩展刚更新，页面还没刷新');
        lines.push('');
        lines.push('先刷新页面（F5）再试。仍未响应就是第 1 种，需要升级浏览器。');
      } else {
        // 钩子所在世界 —— 这一行决定下面每一个数字可不可信。
        // 隔离世界 = 钩子是死的，此时「0 条」不代表页面没数据，照着排查必然走偏。
        var de = document.documentElement;
        var hasMain = !!(de && de.getAttribute('data-zhigu-main'));
        if (diag.world === 'main') {
          lines.push('钩子世界  ：主世界（有效）');
        } else {
          lines.push('钩子世界  ：<b>隔离世界 · 钩子无效</b>' +
                     (hasMain ? '（主世界已装载，本次取的不是它）' : '（主世界未装载）'));
        }
        lines.push('已运行    ：' + diag.uptime + ' 秒');
        lines.push('页面标题  ：' + esc(diag.docTitle || '(空)'));
        if (diag.hash) lines.push('路由 hash ：' + esc(diag.hash));
        lines.push('解析响应  ：' + diag.responses + ' 条（JSON.parse 命中 ' + diag.parsed + '）');
        lines.push('序列/图像 ：' + (diag.series ? diag.series.length : 0) + ' 个 / ' + diag.total + ' 张');
        // 内网 Wado（WebDicomView）专用一行：这套部署没有"影像清单接口"，
        // 能不能取图全看 viewer 对象读没读到、基址/检查号齐不齐。
        if (diag.wado && (diag.wado.active || diag.wado.note)) {
          var w = diag.wado;
          lines.push('内网 Wado ：' + (w.active
            ? ('序列 ' + w.series + ' 个 / ' + w.images + ' 张' +
               (w.real ? ('，真实地址 ' + w.real + ' 条') : '') +
               (w.jpgOnly ? ' · 仅 JPEG 预览' : ''))
            : '未识别到 viewer 对象') +
            (w.note ? (' · ' + w.note) : ''));
          if (w.base) lines.push('  取图基址：' + esc(w.base));
          if (w.study) lines.push('  检查号  ：' + esc(w.study) + '　院区 ' + esc(w.hosp || '?'));
        }
        lines.push('无地址张数：' + diag.ghosts);
        // 这两行决定"能不能拿到原始 DICOM"：imageResult 里出现 dicom 才有
        // 原始数据，只有 jpeg 就说明这个入口只给预览图。
        if (diag.assetKeys) lines.push('影像字段  ：' + esc(diag.assetKeys));
        // 这一行是「拼出来的地址到底可不可信」的唯一判据：0 条 = 页面一张
        // 影像都没加载过，所有 URL 都是猜的（必然 404），不是软件的问题。
        // 旧版 inject（<1.4.1）没有这个字段，要单独说明，别让用户误以为
        // "一张都没加载"——那是页面没刷新时最常见的情况。
        if (typeof diag.knownCount === 'number') {
          lines.push('真实影像址：' + diag.knownCount + ' 条' +
                     (diag.knownCount ? '' : '  ← 页面还没加载过任何一张图'));
          if (diag.knownSample && diag.knownSample.length) {
            diag.knownSample.forEach(function (u) {
              lines.push('  ' + esc(u));
            });
          }
        } else {
          lines.push('真实影像址：旧版钩子未统计（请刷新页面后再看）');
        }
        if (diag.imageBase) lines.push('CDN 基址  ：' + esc(diag.imageBase));
        // 从接口数据里取到的域名 —— 有它就能拼对，没有就说明域名写在前端
        // 代码里，只能靠已加载图片反推（见下面「已加载图片」）。
        if (diag.cdnBases && diag.cdnBases.length) {
          lines.push('CDN 域名  ：' + esc(diag.cdnBases.join(' | ')));
        } else {
          lines.push('CDN 域名  ：数据里没找到');
        }
        if (diag.probe) lines.push('域名探测  ：' + esc(diag.probe));
        if (diag.baseFields) lines.push('域名字段  ：' + esc(diag.baseFields).replace(/\n/g, ' '));
        // knownCount=0 时，这里能回答「页面到底渲染过图没有」
        if (diag.imgRes && diag.imgRes.length) {
          lines.push('已加载图片：');
          diag.imgRes.forEach(function (u) { lines.push('  ' + esc(u)); });
        } else {
          lines.push('已加载图片：无（页面没发出过任何图片请求）');
        }
        if (diag.series && diag.series.length) {
          lines.push('');
          lines.push('<b>序列明细</b>');
          diag.series.slice(0, 6).forEach(function (s) {
            lines.push('· ' + esc(s.modality || '?') + ' | ' + esc(s.desc || s.key) +
                       ' → ' + s.images + ' 张' + (s.ghosts ? ('，' + s.ghosts + ' 张无地址') : ''));
          });
        }
        // 请求记录放在前面：判断"页面到底有没有去取影像"靠它，而不是靠解析结果
        if (diag.req && diag.req.length) {
          lines.push('');
          lines.push('<b>页面请求记录（共 ' + diag.req.length + ' 条，倒序）</b>');
          diag.req.slice(-14).reverse().forEach(function (u) {
            lines.push('· ' + esc(u));
          });
        } else {
          lines.push('');
          lines.push('<i>没有记录到任何请求</i>');
        }
        if (diag.net && diag.net.length) {
          lines.push('');
          lines.push('<b>解析命中的 JSON</b>');
          diag.net.slice(-8).reverse().forEach(function (u) {
            lines.push('· ' + esc(u));
          });
        }
        // 降级时给可操作的结论，而不是一堆 0 让用户自己猜
        if (diag.world !== 'main') {
          lines.push('');
          lines.push('<b>钩子装在了隔离世界 —— 下面是这么多个 0 的真正原因</b>');
          lines.push('页面的 fetch / XHR / JSON.parse 一个都拦不到，所以');
          lines.push('「解析响应 0 条」「真实影像址 0 条」都是假象，');
          lines.push('不代表页面没有数据（本页确实加载过图，见上方「已加载图片」）。');
          lines.push('');
          lines.push('二选一：');
          lines.push('1) 浏览器版本低于 111（360 / QQ / 医院受管 Chrome 常见），');
          lines.push('   manifest 的 world:"MAIN" 被静默忽略 → 升级到 Chrome 111+');
          lines.push('2) 页面 CSP 拦掉了扩展注入的 script → 升级后仍无效再查这条');
          lines.push('');
          lines.push('权宜之计：点「重新收集」，本页已加载过的图片会走兜底导出，');
          lines.push('但只能拿到页面上已经渲染出来的那几张。');
        }
      }
      lines.push('');
      lines.push('<div class="row">');
      lines.push('<button class="mini" data-act="copy">复制诊断信息</button>');
      lines.push('<button class="mini" data-act="retry">重新收集</button>');
      lines.push('<button class="mini" data-act="close">关闭</button>');
      lines.push('</div>');
      panelEl.innerHTML = lines.join('\n');

      var text = panelEl.textContent;
      panelEl.querySelector('[data-act="copy"]').addEventListener('click', function () {
        try {
          navigator.clipboard.writeText(text).then(function () {
            setState('诊断信息已复制', '');
          }, function () { /* ignore */ });
        } catch (e) { /* ignore */ }
      });
      panelEl.querySelector('[data-act="retry"]').addEventListener('click', function () {
        panelEl.classList.remove('on');
        onClick();
      });
      panelEl.querySelector('[data-act="close"]').addEventListener('click', function () {
        panelEl.classList.remove('on');
      });
    });
  }

  // ---------------------------------------------------------------- 启动

  try {
    chrome.runtime.onMessage.addListener(function (msg) {
      if (!msg) return;
      if (msg.cmd === 'click' && IS_TOP) onClick();
      // 子帧拿到数据了（临床系统里数据在 8989 的 iframe 里）。顶层帧自己去
      // 后台汇总，把张数亮到按钮上 —— 否则按钮永远停在「用知骨打开」，
      // 医生会以为还没准备好，反复刷新。
      if (msg.cmd === 'frameReady' && IS_TOP) {
        bg({ cmd: 'gather' }).then(function (r) {
          var d = r && r.data;
          if (!d || !d.total) return;
          READY_TOTAL = d.total;
          if (!BUSY) {
            show();
            setState('用知骨打开 · ' + READY_TOTAL + ' 张', '');
          }
        });
      }
    });
  } catch (e) { /* 扩展上下文失效时忽略 */ }

  var BOOT_TRIES = 0;
  function boot() {
    if (!IS_TOP) return;
    BOOT_TRIES += 1;
    var shown = false;
    if (looksLikePacs()) {
      show();
      setState('用知骨打开', '');
      shown = true;
    } else if (READY_TOTAL) {
      show();
      setState('用知骨打开 · ' + READY_TOTAL + ' 张', '');
      shown = true;
    }
    // SPA 的影像容器（临床系统的 #dicomview）通常比 DOMContentLoaded 晚得多，
    // 只判一次就再也不管的话，医生在最常用的那个页面上压根看不到按钮。
    // 补试几次（1.2s / 3s / 6s），够等到 Vue 把 iframe 挂上去。
    if (!shown && BOOT_TRIES < 4) {
      var delays = [1200, 3000, 6000];
      setTimeout(boot, delays[BOOT_TRIES - 1] || 6000);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
