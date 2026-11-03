/**
 * content.js —— 运行在扩展隔离世界。
 *
 * 「知骨影像 · 云影像另存本地」版。与「网页取图」插件的差异：
 *   - 按钮是绿色的，文字是「另存到本地」；点击后由扩展后台直接下载落盘，
 *     不依赖本机知骨软件，也不唤起任何程序。
 *   - 诊断面板同样保留：抓不到图时用它定位（钩子世界、请求记录、已加载图片）。
 *
 * 只在顶层帧画按钮；子帧只上报数据，避免 iframe 里冒出好几个按钮。
 */
(function () {
  'use strict';
  if (window.__zgsaveContentInstalled) return;
  window.__zgsaveContentInstalled = true;

  var READY_TOTAL = 0;
  var BUSY = false;
  var LAST_DIAG = null;
  var LAST_FOLDER = '';
  var IS_TOP = (window === window.top);

  // ---------------------------------------------------------------- UI

  var hostEl = null, shadow = null, btnEl = null, txEl = null, dotEl = null, panelEl = null;

  function looksLikePacs() {
    var probe = (location.host + location.pathname).toLowerCase();
    if (/dimage|webpacs|pacs|viewer|cloudimage|webdicom|dicomweb|imageviewer/.test(probe)) {
      return true;
    }
    if (/影像|PACS|放射|放射科|报告/.test(document.title || '')) return true;

    // 内网临床系统：顶层页本身长得不像影像页，真正的 viewer 在跨端口 iframe 里。
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
    hostEl.setAttribute('data-zgsave', 'host');
    // 左下角：右下角已被「网页取图」插件（原插件）占住，两按钮都挂
    // right:16px/bottom:88px 会完全重叠，所以本插件固定挂左边。
    hostEl.style.cssText = 'position:fixed;left:16px;bottom:88px;z-index:2147483647;';
    shadow = hostEl.attachShadow({ mode: 'open' });
    shadow.innerHTML = [
      '<style>',
      ':host{all:initial}',
      '.btn{display:flex;align-items:center;gap:8px;padding:9px 16px 9px 12px;',
      'border:0;border-radius:22px;cursor:pointer;font:600 13px/1.2 "Microsoft YaHei",system-ui,sans-serif;',
      'color:#fff;background:linear-gradient(135deg,#0f8a4d,#1a9b4b);',
      'box-shadow:0 4px 14px rgba(15,110,60,.42);transition:transform .12s,filter .12s;',
      'letter-spacing:.3px;white-space:nowrap}',
      '.btn:hover{filter:brightness(1.1);transform:translateY(-1px)}',
      '.btn:active{transform:translateY(0);filter:brightness(.95)}',
      '.btn[disabled]{cursor:default;filter:grayscale(.35) brightness(.9)}',
      '.tx{max-width:232px;overflow:hidden;text-overflow:ellipsis}',
      '.dot{width:8px;height:8px;border-radius:50%;background:#5df2a8;flex:0 0 auto;',
      'box-shadow:0 0 6px #5df2a8}',
      '.dot.warn{background:#ffcf5c;box-shadow:0 0 6px #ffcf5c}',
      '.dot.busy{background:#8fd0ff;box-shadow:0 0 6px #8fd0ff;animation:pz 1s infinite}',
      '.dot.err{background:#ff6b6b;box-shadow:0 0 6px #ff6b6b}',
      '@keyframes pz{0%,100%{opacity:1}50%{opacity:.25}}',
      '.panel{display:none;position:absolute;left:0;bottom:46px;width:440px;max-height:60vh;',
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
      '<span class="tx">另存到本地</span></button>'
    ].join('');
    document.body.appendChild(hostEl);
    btnEl = shadow.querySelector('.btn');
    txEl = shadow.querySelector('.tx');
    dotEl = shadow.querySelector('.dot');
    panelEl = shadow.querySelector('.panel');

    // 单击 = 另存；双击 = 诊断。靠 240ms 迟滞区分，不会互相串。
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
    var full = String(msg == null ? '另存失败' : msg);
    var brief = full.replace(/\s+/g, ' ').trim();
    if (brief.length > 16) {
      if (!hint) hint = full;
      brief = brief.slice(0, 15) + '…';
    }
    setState(brief, 'err');
    if (hint) showDiagHint(hint);
    setTimeout(function () {
      if (!BUSY) setState(READY_TOTAL ? ('另存到本地 · ' + READY_TOTAL + ' 张') : '另存到本地', '');
    }, 4600);
  }

  // ---------------------------------------------------------------- 与主世界通信

  function collect() {
    return new Promise(function (resolve) {
      var done = false;
      // 两份 inject.js 都会回（主世界 + 隔离世界），必须认主世界那份：
      // 隔离世界的钩子拦不到页面请求，回的是空数据，让它抢先回就把真结果盖掉了。
      var fallback = null;

      function finish(payload) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        window.removeEventListener('message', onMsg);
        resolve(payload);
      }

      var timer = setTimeout(function () {
        finish(fallback);
      }, 2000);

      function onMsg(ev) {
        if (ev.source !== window) return;
        var d = ev.data;
        if (!d || d.__zgsave !== 'collected') return;
        var payload = d.data || null;
        if (payload && payload.world === 'main') {
          finish(payload);
          return;
        }
        if (!fallback) fallback = payload;
      }

      window.addEventListener('message', onMsg);
      try {
        window.postMessage({ __zgsave: 'collect' }, '*');
      } catch (e) {
        clearTimeout(timer);
        resolve(null);
      }
    });
  }

  function diagnose() {
    return new Promise(function (resolve) {
      var done = false;
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
        if (!d || d.__zgsave !== 'diagResult') return;
        var payload = d.data || null;
        if (payload && payload.world === 'main') {
          finish(payload);
          return;
        }
        if (!fallback) fallback = payload;
      }

      window.addEventListener('message', onMsg);
      try {
        window.postMessage({ __zgsave: 'diag' }, '*');
      } catch (e) {
        clearTimeout(timer);
        resolve(null);
      }
    });
  }

  // 主世界一收到数据就通知，用来点亮按钮上的张数
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data) return;
    if (ev.data.__zgsave === 'ready') {
      READY_TOTAL = ev.data.total || 0;
      if (IS_TOP) {
        if (!BUSY) {
          show();
          setState('另存到本地 · ' + READY_TOTAL + ' 张', '');
        }
      } else {
        // 子帧有数据：赶紧存到后台，等着顶层帧来取
        collect().then(function (d) {
          if (d && d.total) {
            bg({ cmd: 'stash', data: d });
            bg({ cmd: 'frameReady' });
          }
        });
      }
    }
  });

  // ---------------------------------------------------------------- 后台 RPC

  function bg(msg) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage(msg, function (resp) {
          if (chrome.runtime.lastError) {
            resolve(null);
            return;
          }
          resolve(resp || null);
        });
      } catch (e) {
        // 扩展被重载后旧脚本会抛 "context invalidated"，用户刷新页面即可
        resolve(null);
      }
    });
  }

  // ---------------------------------------------------------------- 清单合并

  function mergeData(remote, local) {
    var out = { series: [], total: 0, patient: '', title: '', diag: null,
                srcUrl: '' };
    var byKey = Object.create(null);

    function push(src) {
      if (!src || !Array.isArray(src.series)) return;
      if (!out.patient && src.patient) out.patient = src.patient;
      if (!out.title && src.title) out.title = src.title;
      if (!out.diag && src.diag) out.diag = src.diag;
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
    push(local);

    out.series.forEach(function (s) { delete s.seen; out.total += s.images.length; });
    out.series = out.series.filter(function (s) { return s.images.length > 0; });
    return out;
  }

  // ---------------------------------------------------------------- 主流程

  function buildPayload(data) {
    var origins = [];
    (data.series || []).forEach(function (s) {
      (s.images || []).forEach(function (im) {
        if (!im || !im.url || origins.length >= 6) return;
        try {
          var o = new URL(im.url, location.href).origin;
          if (origins.indexOf(o) < 0) origins.push(o);
        } catch (e) { /* ignore */ }
      });
    });
    return {
      pageUrl: location.href,
      pageTitle: data.title || document.title || '',
      patient: data.patient || '',
      referer: data.srcUrl || location.href,
      userAgent: navigator.userAgent,
      origins: origins,
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

  // 后台逐张下载时的进度推送（cmd:'progress'）
  try {
    chrome.runtime.onMessage.addListener(function (msg) {
      if (!msg || msg.cmd !== 'progress') return;
      if (msg.total && msg.total !== READY_TOTAL) READY_TOTAL = msg.total;
      if (msg.state === 'running') {
        setState('正在另存 ' + (msg.done || 0) + '/' + msg.total +
                 (msg.failed ? ('（失败 ' + msg.failed + '）') : '') + '…', 'busy');
        return;
      }
      if (msg.state === 'done' || msg.state === 'cancelled') {
        BUSY = false;
        if (btnEl) btnEl.disabled = false;
        var failed = msg.failed || 0;
        if (failed && !msg.done) {
          fail('全部失败（' + failed + ' 张）',
               '每个地址都下载失败。常见原因：会话已过期（刷新页面重新登录后再点）、' +
               '取图地址带时效签名且已过期、或页面还没翻到图（S3 签名部署要' +
               '先翻一遍图再点，翻过的图才拿得到带签名的地址）。');
        } else {
          setState((msg.state === 'cancelled' ? '已取消，完成 ' : '已另存 ') +
                   (msg.done || 0) + ' 张' +
                   (failed ? ('，失败 ' + failed + ' 张') : '') + ' ✓', '');
          setTimeout(function () {
            if (!BUSY) setState('另存到本地 · ' + READY_TOTAL + ' 张', '');
          }, 5200);
        }
      }
    });
  } catch (e) { /* 扩展上下文失效时忽略 */ }

  function onClick() {
    if (BUSY) return;
    BUSY = true;
    if (btnEl) btnEl.disabled = true;
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
        if (btnEl) btnEl.disabled = false;
        // 抓不到时把诊断打出来：是钩子没装上，还是页面压根没下发地址
        return diagnose().then(function (diag) {
          handleEmpty(diag);
        });
      }

      setState('正在另存 ' + data.total + ' 张…', 'busy');
      return bg({ cmd: 'save', payload: buildPayload(data) }).then(function (rr) {
        if (!rr || !rr.ok) {
          fail((rr && rr.error) || '另存失败');
          return null;
        }
        // 后台已开跑，进度走 progress 消息推送；这里记录落盘位置
        LAST_FOLDER = rr.folder || '';
        setState('正在另存 0/' + rr.total + ' 张…', 'busy');
      });
    }).catch(function (err) {
      BUSY = false;
      if (btnEl) btnEl.disabled = false;
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
      tip = '页面没提供原始文件地址';
      hint = '已识别 ' + ghosts + ' 张图像，但这个部署只下发了 JPEG 预览，没有原始文件地址。属于部署形态限制。';
    } else if (seriesN === 0) {
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

    setState(tip, 'err');
    setTimeout(function () {
      if (!BUSY) setState('另存到本地', 'warn');
    }, 4600);
    showDiagHint(hint);
  }

  function showDiagHint(hint) {
    if (!IS_TOP) return;
    ensureUI();
    if (!panelEl) return;
    panelEl.innerHTML = [
      '<b>另存未完成</b>',
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

    var info = { page: location.href.slice(0, 120), readyTotal: READY_TOTAL };

    Promise.all([diagnose(), bg({ cmd: 'gather' }), bg({ cmd: 'jobInfo' })]).then(function (arr) {
      var diag = arr[0];
      var remoteDiag = arr[1] && arr[1].data && arr[1].data.diag;
      var lastJob = arr[2] && arr[2].job;
      var lines = [];
      lines.push('<b>云影像另存 · 诊断</b>');
      lines.push('页面      ：' + esc(info.page));
      lines.push('版本      ：' + esc((diag && diag.version) || '未响应'));
      if (remoteDiag && remoteDiag.wado && remoteDiag.wado.images) {
        var rw = remoteDiag.wado;
        lines.push('子帧 Wado ：序列 ' + rw.series + ' 个 / ' + rw.images + ' 张' +
                   (rw.real ? ('，真实地址 ' + rw.real + ' 条') : '') +
                   (rw.jpgOnly ? ' · 仅 JPEG 预览' : ''));
        if (rw.base) lines.push('  取图基址：' + esc(rw.base));
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
        var de = document.documentElement;
        var hasMain = !!(de && de.getAttribute('data-zgsave-main'));
        if (diag.world === 'main') {
          lines.push('钩子世界  ：主世界（有效）');
        } else {
          lines.push('钩子世界  ：<b>隔离世界 · 钩子无效</b>' +
                     (hasMain ? '（主世界已装载，本次取的不是它）' : '（主世界未装载）'));
        }
        lines.push('已运行    ：' + diag.uptime + ' 秒');
        lines.push('页面标题  ：' + esc(diag.docTitle || '(空)'));
        lines.push('解析响应  ：' + diag.responses + ' 条（JSON.parse 命中 ' + diag.parsed + '）');
        lines.push('序列/图像 ：' + (diag.series ? diag.series.length : 0) + ' 个 / ' + diag.total + ' 张');
        if (diag.wado && (diag.wado.active || diag.wado.note)) {
          var w = diag.wado;
          lines.push('内网 Wado ：' + (w.active
            ? ('序列 ' + w.series + ' 个 / ' + w.images + ' 张' +
               (w.real ? ('，真实地址 ' + w.real + ' 条') : '') +
               (w.jpgOnly ? ' · 仅 JPEG 预览' : ''))
            : '未识别到 viewer 对象') +
            (w.note ? (' · ' + w.note) : ''));
          if (w.base) lines.push('  取图基址：' + esc(w.base));
          if (w.study) lines.push('  检查号  ：' + esc(w.study));
        }
        lines.push('无地址张数：' + diag.ghosts);
        if (diag.assetKeys) lines.push('影像字段  ：' + esc(diag.assetKeys));
        if (typeof diag.knownCount === 'number') {
          lines.push('真实影像址：' + diag.knownCount + ' 条' +
                     (diag.knownCount ? '' : '  ← 页面还没加载过任何一张图'));
          if (diag.knownSample && diag.knownSample.length) {
            diag.knownSample.forEach(function (u) {
              lines.push('  ' + esc(u));
            });
          }
        }
        if (diag.imageBase) lines.push('CDN 基址  ：' + esc(diag.imageBase));
        if (diag.cdnBases && diag.cdnBases.length) {
          lines.push('CDN 域名  ：' + esc(diag.cdnBases.join(' | ')));
        } else {
          lines.push('CDN 域名  ：数据里没找到');
        }
        if (diag.probe) lines.push('域名探测  ：' + esc(diag.probe));
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
      }
      if (lastJob) {
        lines.push('');
        lines.push('<b>上次保存任务</b>');
        var st = lastJob.state === 'running' ? '进行中'
               : lastJob.state === 'cancelled' ? '已取消' : '已结束';
        lines.push('状态      ：' + st + ' · 成功 ' + lastJob.done + ' / ' + lastJob.total +
                   '，失败 ' + lastJob.failed);
        if (lastJob.urls && lastJob.urls.length) {
          lines.push('下载地址  ：');
          lastJob.urls.forEach(function (u) {
            lines.push('  ' + esc(u) +
                       (/[?&][Ss]ignature=/.test(u) ? '' : '  ← 无签名'));
          });
        }
        if (lastJob.errors && lastJob.errors.length) {
          lines.push('<b>失败原因</b>：');
          lastJob.errors.forEach(function (e) { lines.push('  ' + esc(e)); });
        }
      }
      if (LAST_FOLDER) {
        lines.push('');
        lines.push('<span class="hint">最近一次保存位置：下载/知骨另存/…（见浏览器下载记录）</span>');
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
      // 子帧拿到数据了。顶层帧自己去后台汇总，把张数亮到按钮上。
      if (msg.cmd === 'frameReady' && IS_TOP) {
        bg({ cmd: 'gather' }).then(function (r) {
          var d = r && r.data;
          if (!d || !d.total) return;
          READY_TOTAL = d.total;
          if (!BUSY) {
            show();
            setState('另存到本地 · ' + READY_TOTAL + ' 张', '');
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
      setState('另存到本地', '');
      shown = true;
    } else if (READY_TOTAL) {
      show();
      setState('另存到本地 · ' + READY_TOTAL + ' 张', '');
      shown = true;
    }
    // SPA 的影像容器通常比 DOMContentLoaded 晚得多，补试几次（1.2s / 3s / 6s）。
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
