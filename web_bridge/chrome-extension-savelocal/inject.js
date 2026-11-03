/**
 * inject.js —— 页面主世界（MAIN world）。必须早于页面自己的脚本执行。
 *
 * 为什么必须在主世界：
 *   云 PACS（鲁云影像 / 飞图 ftimage 架构）的接口响应是 SM2 密文（"04..." 开头），
 *   前端解密后再 JSON.parse。我们无法在扩展里复现它的国密加密（还带 BIZ-REQUEST-ID
 *   和设备指纹），所以只能在「页面已经把响应解成对象的那一刻」接管。
 *
 * 抓取面（任意一路命中即可，多路冗余是刻意的）：
 *   A. JSON.parse                     —— 页面解密后自己解析（最常见）
 *   B. Response.prototype.json/text   —— fetch().json() / fetch().text()
 *   C. XHR 的 response / responseText —— axios 读响应
 *   D. XHR loadend 主动扫描           —— 不依赖页面何时读响应，最保险的一路
 *   E. 全局 store / sessionStorage    —— 万一钩子装晚了（页面已加载完），回捞一次
 *
 * 与软件端的约定：只传「URL 清单 + 凭据」，不传像素。一个 CT 300 张约 150MB，
 * 走 postMessage / HTTP 都扛不住，必须让软件端自己并发下载。
 */
(function () {
  'use strict';

  if (window.__zgsaveBridgeInstalled) return;      // 双注入去重（boot.js 与 manifest 两条路）
  window.__zgsaveBridgeInstalled = true;
  window.__zgsaveBridgeVersion = '1.1.0';

  var BOOT_TS = Date.now();

  // ------------------------------------------------------------------ 世界判定
  //
  // 只有跑在**页面主世界**里，钩子才拦得到页面的 fetch / XHR / JSON.parse。
  // 两种情况下我们会掉进 content-script 世界（隔离世界），此时钩子全是死的：
  //   ① Chrome < 111：manifest 的 world:"MAIN" 被静默忽略，降级成 content script
  //   ② 页面 CSP 拦掉 <script src="chrome-extension://…">，boot.js 那条路断掉
  //
  // 判定依据：隔离世界（content script）拿得到 chrome.runtime.id；http(s)
  // 页面的主世界脚本拿不到这个对象。
  //
  // 为什么要显式判定：降级时**没有任何报错**，钩子照装、诊断照出，只是永远
  // 一条都抓不到 —— 表现为「换台电脑就失效」。更糟的是两份 inject.js 会同时
  // 存在（主世界一份 + 隔离世界一份），content.js 取先回的那个，可能正好是
  // 空数据的隔离世界那份，把真数据盖掉。
  var IN_MAIN = !(typeof chrome !== 'undefined' && chrome &&
                  chrome.runtime && chrome.runtime.id);

  // DOM 在两个世界之间共享 —— 用它把「哪一份装载成功了」告诉 content.js。
  // 这样即使消息时序不利，content.js 也能从 DOM 上读到真实情况。
  try {
    var _de = document.documentElement;
    if (_de) {
      if (IN_MAIN) _de.setAttribute('data-zgsave-main', '1');
      else _de.setAttribute('data-zgsave-iso', '1');
    }
  } catch (e) { /* ignore */ }

  var seriesMap = Object.create(null);            // key -> {key,desc,modality,images,ghosts}
  var studyInfo = { patient: '' };
  var ghostCount = 0;                             // 有 uuid 但页面没暴露地址的张数
  var stats = { responses: 0, parsed: 0, net: [] };
  var reqLog = [];                                // 所有请求（不管解析成不成功）
  var hookedLibs = [];

  // 浏览器实际请求过的影像地址，按末段（rawKey）建索引。
  // 为什么必须用「实际请求过的」：imageResult 里给的是相对 CDN 根的路径，
  // 而 CDN 基址（images2.ftimage.cn/v3 之类）每个部署都不同，自己拼必错。
  var imgByKey = Object.create(null);
  var imageBase = '';                             // 如 https://images2.ftimage.cn/v3
  var ctSeen = 0;
  var assetKeys = Object.create(null);            // imageResult 里出现过的字段名（诊断用）

  // ------------------------------------------------------------------ 判定工具

  var IMG_EXT = /\.(jpe?g|png|gif|bmp|webp|svg|ico|css|js|json|html?|woff2?|ttf|eot|map|mp4|mp3|xml|txt)(\?|#|$)/i;
  var FAKE_PROTO = /^(data:|blob:|about:|javascript:|ft:|file:|mailto:)/i;

  function sane(s) {
    if (typeof s !== 'string') return false;
    var p = s.trim();
    if (p.length < 3 || p.length > 800) return false;
    if (FAKE_PROTO.test(p)) return false;
    if (IMG_EXT.test(p)) return false;            // .dcm 不在此列，故意留着
    if (/[\s"'<>\\^`{|}]/.test(p)) return false;
    return true;
  }

  // 越大越像「原始 DICOM 地址」。用分数排序，而不是假设字段名。
  function score(s) {
    var p = s.toLowerCase();
    if (/\.dcm(\?|#|$)/.test(p)) return 4;
    if (/dicom|wado|\/dcm\b/.test(p)) return 3;
    if (/series|instance|study|image/.test(p)) return 1;
    return 0;
  }

  function candidates(raw) {
    var out = [];
    if (!sane(raw)) return out;
    var p = raw.trim();
    if (/^https?:\/\//i.test(p)) { out.push(p); return out; }
    if (p.indexOf('//') === 0) { out.push(location.protocol + p); return out; }

    var abs = p.charAt(0) === '/' ? p : ('/' + p.replace(/^\.?\//, ''));

    // ⓪ 接口数据里写死的 CDN 域名。优先级最高：域名是页面自己给的，
    //    不用反推、不用假设版本段，只要它存在就基本一定对。
    //    放在最前面是因为用户可能停在序列列表、一张图都没加载过，
    //    这时 performance 里什么都采不到（实测就是这个场景）。
    var ci, vi, vs;
    for (ci = 0; ci < cdnBases.length && out.length < 6; ci++) {
      vs = baseVariants(cdnBases[ci]);
      for (vi = 0; vi < vs.length; vi++) {
        var u = joinBase(vs[vi], abs);
        if (out.indexOf(u) < 0) out.push(u);
      }
    }

    // ① 真实地址反推的基址：有 v3 版本段的部署得到 …/v3，
    //    没有的得到 origin 本身，两种都自动对。
    var bases = basesFor(abs);
    for (var bi = 0; bi < bases.length; bi++) {
      var u2 = bases[bi] + abs;
      if (out.indexOf(u2) < 0) out.push(u2);
    }

    // ② 老兜底：早先按第一段猜出来的 CDN 根（仍可能有对的）。
    if (imageBase && out.indexOf(imageBase + abs) < 0) out.push(imageBase + abs);

    // 已经拿到任何 CDN 基址时，站点域名/老拼法一律不再给 —— 影像在 CDN 上时
    // 它们 100% 是 404。419 张 × 3 个假候选 = 一千多次白跑，还会把下载端的
    // 熔断计数器提前打满。同域部署由 ① 覆盖（那时反推的基址正好是 origin）。
    if (cdnBases.length || bases.length) return out;

    // ③ 站点自己的域名 —— 兼容「影像和页面同域」的部署。
    if (out.indexOf(location.origin + abs) < 0) out.push(location.origin + abs);

    // ④ 更老的自建部署拼法，由下载端按序回退。
    var clean = p.replace(/^\.?\//, '');
    var trys = [location.origin + '/' + clean, location.origin + '/dimage/' + clean];
    try { trys.push(new URL(p, location.href).href); } catch (e) { /* ignore */ }
    for (var i = 0; i < trys.length; i++) {
      if (out.indexOf(trys[i]) < 0) out.push(trys[i]);
    }
    return out;
  }

  var IMG_HOST = /(^|\.)ftimage\.(cn|com)$/i;
  // 影像 CDN 上的真实地址（浏览器确实拉过的）。反推基址只认这个。
  var knownUrls = [];
  var knownSet = Object.create(null);
  var knownPaths = [];                            // {origin, segs} 预解析，避免反复 new URL

  /**
   * 登记一条「浏览器真的拉过的影像地址」。
   *
   * 判定口径（踩过的坑，别随意改）：
   *  - 末段长度必须放宽到 8 位。ftimage 真实末段是 **12 位**十六进制
   *    （如 711a597354f4），之前卡 {16,64} 导致一条都登记不上，
   *    CDN 基址永远是空串，于是所有相对路径被拼到站点域名上、419 张全 404。
   *  - 但也不能只看末段，否则站点自己的静态资源会被收进来。所以要求
   *    路径里至少有 2 段 ≥16 位 hex —— 对应 study / series 两级 uid，
   *    普通静态资源不会有这种形状。
   *  - 【临沂 S3 部署的坑】CDN 对象末段**带扩展名**（xxx.dcm / xxx.jpg），
   *    而接口里的 rawKey 不带。之前直接拿带点的末段去卡正则，一条都登记
   *    不上 → rawKey 精确命中永远落空 → 只能按基址拼**无签名**地址 →
   *    S3 预签名体系下必 403（浏览器真拉过的地址明明带着签名）。
   *    现在按「去掉媒体扩展名的末段」当键（对齐 rawKey），值保留完整
   *    URL **含 query** —— 签名就在 query 里，丢了必 403。
   *  - 同键冲突（jpeg 预览 / dcm 原始文件同 rawKey）时 .dcm 优先：
   *    原始 DICOM 永远比预览图值钱。
   */
  function noteImageUrl(abs) {
    if (!abs || typeof abs !== 'string') return 0;
    if (!/^https?:\/\//i.test(abs)) return 0;
    var u;
    try { u = new URL(abs, location.href); } catch (e) { return 0; }
    if (!IMG_HOST.test(u.hostname)) return 0;
    var segs = u.pathname.split('/').filter(Boolean);
    if (segs.length < 4) return 0;
    var last = segs[segs.length - 1];
    var stem = String(last).replace(/\.(dcm|jpe?g|png|bmp|gif|webp|icon)$/i, '');
    if (!/^[0-9a-zA-Z_-]{8,64}$/.test(stem)) return 0;
    var hexDeep = 0;
    for (var i = 0; i < segs.length - 1; i++) {
      if (/^[0-9a-fA-F]{16,64}$/.test(segs[i])) hexDeep += 1;
    }
    if (hexDeep < 2) return 0;

    var full = u.origin + u.pathname + (u.search || '');
    var isDcm = /\.dcm(\?|$)/i.test(full);
    if (!imgByKey[stem] || (isDcm && !/\.dcm(\?|$)/i.test(imgByKey[stem]))) {
      imgByKey[stem] = full;
    }
    if (!knownSet[full]) {
      knownSet[full] = 1;
      knownUrls.push(full);
      knownPaths.push({ origin: u.origin, segs: segs });
      if (knownUrls.length > 500) { knownUrls.shift(); knownPaths.shift(); }
    }
    // 给诊断面板一个「基址看起来是什么」的展示值。仅展示 + 最后兜底，
    // 真正拼接用的是 basesFor() 的后缀对齐结果。
    if (!imageBase) imageBase = u.origin + '/' + segs[0];
    ctSeen += 1;
    return 1;
  }

  /**
   * 从已登记的真实地址反推「相对路径的基址」。
   *
   * 关键是**按目录前缀对齐**，不是整条路径匹配：用户往往只翻到序列的前
   * 几张，同序列后面几百张的 instance 段都不在 known 里。只认整条路径的话
   * 第 2 张起就反推不出基址，又退回站点域名 → 又是 404。
   *
   * 做法：把相对路径切段，在真实地址里找它的起始位置，比较公共前缀；
   * 公共段 ≥3（如 sdly/院区/医院码）就认定同一个 CDN，左边剩下的即基址。
   * 这样有 v3 版本段的部署得到 …/v3，没有的得到 origin 本身，不必假设形态。
   */
  function basesFor(abs) {
    var p = String(abs).split('#')[0].split('?')[0].replace(/^\/+/, '');
    if (!p) return [];
    var R = p.split('/').filter(Boolean);
    if (R.length < 4) return [];
    var out = [];
    for (var i = 0; i < knownPaths.length; i++) {
      var kp = knownPaths[i];
      var K = kp.segs;
      for (var start = K.length - 1; start >= 0; start--) {
        if (K[start] !== R[0]) continue;
        var n = 0;
        while (n < R.length && (start + n) < K.length && K[start + n] === R[n]) n += 1;
        if (n < 3) continue;                      // 只重合一两段不足以认定同源
        var base = kp.origin + (start ? ('/' + K.slice(0, start).join('/')) : '');
        if (out.indexOf(base) < 0) out.push(base);
        break;
      }
      if (out.length >= 2) break;                 // 正常只有一个真基址
    }
    return out;
  }

  /**
   * 页面彻底没有加载过影像时的补救：从浏览器资源计时和 <img> 里收一遍。
   *
   * 这些是「已经发生过的事实」，比任何反推都可靠。用户点按钮时若只翻到
   * 前几张图，也能靠这一条拿到正确的 CDN 基址，剩下的按基址拼。
   */
  function harvestRealUrls() {
    try {
      var es = performance.getEntriesByType('resource') || [];
      var lim = es.length > 3000 ? es.length - 3000 : 0;
      for (var i = lim; i < es.length; i++) {
        try { noteImageUrl(es[i].name); } catch (e) { /* ignore */ }
        try { wadoNoteUrl(es[i].name); } catch (e) { /* ignore */ }
      }
    } catch (e) { /* ignore */ }
    try {
      var imgs = document.getElementsByTagName('img');
      for (var j = 0; j < imgs.length && j < 600; j++) {
        try { noteImageUrl(imgs[j].currentSrc || imgs[j].src || ''); } catch (e) { /* ignore */ }
      }
    } catch (e) { /* ignore */ }
  }

  /**
   * 最后兜底：接口清单一个都没抓到时，把浏览器真拉过的影像地址直接当序列。
   *
   * 触发场景（都不是 bug，是部署形态差异）：
   *   - 页面服务端直出 <img>，压根没有影像清单接口
   *   - 钩子降级到隔离世界，清单一条都拦不到，但图照样被浏览器拉过来了
   *
   * 这些地址是「已经发生过的事实」—— CDN 基址、路径形状全都是现成的，
   * 不需要任何反推。代价是只拿得到页面渲染过的那几张（首屏缩略图居多），
   * 但总比让用户面对「0 张」强，而且能把正确基址带出去。
   */
  function buildFromDomSeries() {
    try { harvestRealUrls(); } catch (e) { return 0; }
    if (!knownUrls.length) return 0;
    var key = 'dom-images';
    var title = '';
    try { title = String(document.title || '').slice(0, 40); } catch (e) { title = ''; }
    var entry = ensureEntry(key, {
      key: key,
      desc: title || '当前页面影像',
      modality: ''
    });
    var made = 0;
    for (var i = 0; i < knownUrls.length; i++) {
      var full = knownUrls[i];
      var segs = full.split('/').filter(Boolean);
      var last = segs[segs.length - 1] || full;
      if (entry.images[last]) continue;
      entry.images[last] = {
        raws: [full],                       // 已是绝对地址，resolveUrls 直接用
        rawKey: last,
        name: last,
        kind: /\.dcm(\?|#|$)/i.test(full) ? 'dicom' : 'jpeg'
      };
      made += 1;
    }
    return made;
  }

  // ------------------------------------------------------------------ CDN 域名（从数据里取）

  /**
   * 接口数据里直接给出的影像 CDN 域名。
   *
   * 鲁云 V3 的影像对象上带 `jpgUseDomain`，有的部署在 siteConfig 类响应里给
   * `imageServerUrl` / `*Domain` / `*Host`。这是**唯一不用猜部署形态**的基址
   * 来源 —— 域名是页面自己写死在数据里的，跟 v3 版本段、院区层级都无关。
   *
   * 为什么必须加这一路：v1.4.1 只靠 performance 里「浏览器真拉过的图片」反推，
   * 而用户停在序列列表时一张图都没请求过 → 采到 0 条 → 基址全猜错 → 419 张全 404。
   */
  var cdnBases = [];                       // 规范化后的基址候选（去重）
  var cdnBaseSeen = Object.create(null);
  var baseFields = Object.create(null);    // 诊断用：字段名 -> 原始值

  var BASE_KEY_RE = /(domain|host|baseurl|base_url|basepath|serverurl|server_url|imgurl|imageurl|cdns?url|prefix|imgsrc|imgroot)/i;
  // 只认「域名 + 可选路径」，且不认静态文件地址，避免把 jpeg 路径当域名。
  var HOSTY_RE = /^(https?:\/\/|\/\/)?[a-z0-9.-]+\.[a-z]{2,10}(\/[\w./?&=%-]*)?$/i;

  function normBase(v) {
    if (typeof v !== 'string') return '';
    var s = v.trim();
    if (!s || s.length > 300) return '';
    if (/\.(jpe?g|png|gif|css|js|json|html?)(\?|#|$)/i.test(s)) return '';
    if (!HOSTY_RE.test(s)) return '';
    if (s.indexOf('//') === 0) s = location.protocol + s;
    else if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
    return s.replace(/\/+$/, '');
  }

  function addBase(v) {
    var b = normBase(v);
    if (!b || cdnBaseSeen[b]) return;
    cdnBaseSeen[b] = 1;
    cdnBases.push(b);
    if (cdnBases.length > 20) cdnBases.shift();   // 收太多说明判定过宽，丢最老的
  }

  /**
   * 深扫接口响应，把所有像 CDN 域名的字符串收进来。
   *
   * 两条线索取并集，任何一条命中即可：
   *   ① 字段名像域名（jpgUseDomain / imageServerUrl / cdnHost …）
   *   ② 值里出现 ftimage（这个架构的影像域名）
   */
  function harvestBasesFrom(node, depth, seen) {
    if (!node || typeof node !== 'object' || depth > 8) return;
    if (seen.indexOf(node) >= 0) return;
    seen.push(node);
    if (Array.isArray(node)) {
      for (var i = 0, n = Math.min(node.length, 200); i < n; i++) {
        harvestBasesFrom(node[i], depth + 1, seen);
      }
      return;
    }
    var keys = Object.keys(node);
    for (var k = 0, m = Math.min(keys.length, 200); k < m; k++) {
      var key = keys[k];
      var v = node[key];
      if (typeof v === 'string') {
        if (BASE_KEY_RE.test(key) && v.length < 300) {
          baseFields[key] = v.slice(0, 120);
          addBase(v);
        } else if (v.indexOf('ftimage') >= 0 && v.length < 300) {
          addBase(v);
        }
      } else if (typeof v === 'boolean' || typeof v === 'number') {
        // jpgUseDomain 这类字段可能是布尔开关，域名在别处。
        // 记下来能在诊断里立刻看出「这个字段不是域名」，不用再猜。
        if (BASE_KEY_RE.test(key)) baseFields[key] = String(v);
      } else if (v && typeof v === 'object') {
        harvestBasesFrom(v, depth + 1, seen);
      }
    }
  }

  /**
   * 把基址和相对路径拼起来，能处理重叠段。
   *
   * 基址可能给到 `https://images2.ftimage.cn`，也可能给到
   * `https://images2.ftimage.cn/v3/sdly`。后者直接拼 `/sdly/...` 会出现
   * `sdly/sdly` —— 所以先找 b 的后缀与 p 前缀的最长重叠，重叠部分只留一份。
   */
  function joinBase(base, abs) {
    var b = String(base).replace(/\/+$/, '');
    var p = String(abs).replace(/^\/+/, '');
    if (!p) return b;
    var bSegs = b.replace(/^https?:\/\//i, '').split('/');
    var pSegs = p.split('/');
    // bSegs[0] 是域名，不参与重叠
    var maxK = Math.min(bSegs.length - 1, pSegs.length - 1);
    for (var k = maxK; k > 0; k--) {
      if (bSegs.slice(bSegs.length - k).join('/') === pSegs.slice(0, k).join('/')) {
        return b + '/' + pSegs.slice(k).join('/');
      }
    }
    return b + '/' + p;
  }

  /**
   * 一个基址可能缺版本段。ftimage 的真实形态是 `<域名>/v3/sdly/...`，
   * 而数据里常常只给 `<域名>`，所以域名后面没有路径时补 v3 先试一次。
   */
  function baseVariants(b) {
    var noProto = String(b).replace(/^https?:\/\//i, '');
    if (noProto.indexOf('/') < 0) return [b + '/v3', b];
    return [b];
  }

  // ------------------------------------------------------------------ 域名探针

  var probeState = { started: false, note: '' };

  /**
   * 接口数据里连域名都没给时，用 `<img>` 静默试出 CDN 基址。
   *
   * 只在**页面一出现影像清单**时触发（不是点击时），所以用户点按钮那一刻
   * 结果早就回来了，不额外等待。代价是页面多几个图片请求，且只在
   * 「没有任何域名线索」时才做。
   *
   * 为什么用 `<img>` 而不是 fetch：图片请求不受 CORS 限制，能真正反映
   * 「浏览器能不能取到这张图」；fetch 跨域多半直接失败，得到的结论是假的。
   *
   * 候选只试 `images1..9.ftimage.cn/v3` —— 这是 ftimage V3 的固定命名规律，
   * 且仅当相对路径确实是 `/sdly/...` 这种 V3 形态时才试，不会瞎撞。
   */
  function maybeProbeBase(abs) {
    if (probeState.started || cdnBases.length || knownUrls.length) return;
    if (typeof abs !== 'string' || abs.indexOf('/sdly/') !== 0) return;
    probeState.started = true;
    probeState.note = 'trying';
    var left = 9;
    for (var n = 1; n <= 9; n++) {
      (function (idx) {
        var base = 'https://images' + idx + '.ftimage.cn/v3';
        var el = null;
        try { el = document.createElement('img'); } catch (e) { el = null; }
        if (!el) { left -= 1; return; }
        function settle(hit) {
          el.onload = null;
          el.onerror = null;
          left -= 1;
          if (hit) {
            addBase(base);
            probeState.note = 'ok ' + base;
          } else if (left <= 0 && probeState.note === 'trying') {
            probeState.note = 'fail（9 个候选都不通）';
          }
        }
        el.onload = function () { settle(true); };
        el.onerror = function () { settle(false); };
        try { el.src = base + abs; } catch (e) { settle(false); }
      })(n);
    }
  }

  /**
   * 从影像相对路径里反推 seriesUuid。
   *
   * ftimage 的路径形态不固定：有时是 `…/<study>/<series>/<rawKey>`（3 段 hex），
   * 有时多一层 instance（`…/<study>/<series>/<inst>/<rawKey>`，4 段 hex）。
   * 后者按「倒数第二段」取到的是 instance，**每张都不同** —— 实测 419 张图
   * 被拆成了 419 个序列目录。所以这里只在形态确定（恰好 3 段 hex）时才返回，
   * 其余归一组：知骨打开时会按 DICOM 的 SeriesInstanceUID 自行分组，
   * 目录只是物理落点，分错不影响显示，拆太碎反而难看又慢。
   */
  function seriesFromPath(p) {
    if (typeof p !== 'string' || !p) return '';
    var segs = p.split('?')[0].split('#')[0].split('/').filter(Boolean);
    if (segs.length < 3) return '';
    var idx = [];
    for (var i = 0; i < segs.length; i++) {
      if (/^[0-9a-fA-F]{16,64}$/.test(segs[i])) idx.push(i);
    }
    if (idx.length !== 3) return '';
    return segs[idx[0] + 1].toLowerCase();
  }

  // ------------------------------------------------------------------ 从单张图里挖地址

  var STRONG_KEYS = ['dicom', 'dicomUrl', 'dicomPath', 'dcmUrl', 'dcmPath',
                     'wadoUrl', 'wadoUri', 'dicomFile', 'originalUrl'];
  // 明确是影像、但**未必**是 DICOM 的字段。ftimage v3 的 imageResult 就是
  // {jpeg: "/sdly/<院区>/<科室>/<study>/<series>/<rawKey>"} 这种形状：路径里
  // 一个 series/study 单词都没有，score() 给它 0 分，按弱字段规则会被直接丢掉。
  var SOFT_KEYS = ['jpeg', 'jpg', 'imageJpeg', 'previewUrl', 'thumbnail'];
  var WEAK_KEYS = ['url', 'imageUrl', 'src', 'path', 'filePath', 'fileUrl',
                   'downloadUrl', 'originUrl', 'fullPath', 'rawKey'];

  /**
   * 从一张图里挖出「原始地址候选」——**不做 URL 拼接**。
   *
   * 为什么不能在这里拼：CDN 基址（images2.ftimage.cn/v3）是从浏览器真实
   * 图片请求里反推的，而识别发生在响应到达时、图片请求之前。这里拼死，
   * imageBase 必然还是空串，只能退到 location.origin —— 那一定是 404。
   * 所以只存原值，等用户点按钮（snapshot）时再拼，那时基址早就有了。
   */
  function rawCandidates(img) {
    var res = (img.imageResult && typeof img.imageResult === 'object') ? img.imageResult : {};
    var picked = [];
    var seen = Object.create(null);
    var i, k;

    // 记录 imageResult 的字段名。诊断里直接显示，能立刻看出这个部署到底
    // 给不给 dicom —— 不用再靠猜结构反复试探。
    try {
      for (k in res) if (k.length < 20) assetKeys[k] = 1;
      for (k in img) if (k !== 'imageResult' && k.length < 20) assetKeys['·' + k] = 1;
    } catch (e) { /* ignore */ }

    function push(v, strong, soft) {
      if (typeof v !== 'string') return;
      v = v.trim();
      if (!sane(v) || seen[v]) return;
      var sc = score(v);
      if (!strong && !soft && sc <= 0) return;    // 弱字段必须自己长得像影像地址
      seen[v] = 1;
      picked.push({ u: v, r: (strong ? 100 : (soft ? 50 : 0)) + sc });
    }

    // ① 精确命中：rawKey 对应的地址浏览器真的拉过，直接拿来用（最可信）。
    // 注意这里只可能在「点按钮时」命中 —— 识别时页面还没发图片请求。
    var rk = img.rawKey || res.rawKey || '';
    if (typeof rk === 'string' && rk && imgByKey[rk]) {
      seen[imgByKey[rk]] = 1;
      picked.push({ u: imgByKey[rk], r: 999, exact: true });
    }

    for (i = 0; i < STRONG_KEYS.length; i++) {
      push(res[STRONG_KEYS[i]], true);
      push(img[STRONG_KEYS[i]], true);
    }
    for (i = 0; i < SOFT_KEYS.length; i++) {
      push(res[SOFT_KEYS[i]], false, true);
      push(img[SOFT_KEYS[i]], false, true);
    }
    for (i = 0; i < WEAK_KEYS.length; i++) {
      push(res[WEAK_KEYS[i]], false);
      push(img[WEAK_KEYS[i]], false);
    }
    // 兜底：字段名没见过也无所谓，只要值以 .dcm 收尾
    for (k in res) {
      if (typeof res[k] === 'string' && /\.dcm(\?|#|$)/i.test(res[k])) push(res[k], true);
    }
    for (k in img) {
      if (typeof img[k] === 'string' && /\.dcm(\?|#|$)/i.test(img[k])) push(img[k], true);
    }
    if (!picked.length) return null;

    picked.sort(function (a, b) { return b.r - a.r; });

    // 只留前 3 个原值。拼接推迟到 snapshot()，那时 CDN 基址已经反推出来了。
    var raws = [];
    for (i = 0; i < picked.length && raws.length < 3; i++) raws.push(picked[i].u);
    return { raws: raws, rawKey: (typeof rk === 'string' ? rk : '') };
  }

  /**
   * 把「原始候选」解析成可下载的绝对 URL 列表（点按钮时才调用）。
   *
   * 顺序：rawKey 精确命中（浏览器真拉过）> 原始值各自的候选拼接。
   * 这里也是唯一能拿到 CDN 基址的时机 —— 前面的 addImage 只存原值。
   */
  function resolveUrls(it) {
    var out = [];
    var raws = it.raws || [];
    if (it.rawKey && imgByKey[it.rawKey]) out.push(imgByKey[it.rawKey]);
    for (var i = 0; i < raws.length; i++) {
      var cs = candidates(raws[i]);
      for (var m = 0; m < cs.length; m++) {
        if (out.indexOf(cs[m]) < 0) out.push(cs[m]);
      }
      if (out.length >= 4) break;                 // 候选太多反而拖慢下载端
    }
    return out;
  }

  // ------------------------------------------------------------------ series 归组

  var LIST_KEYS = ['imageList', 'images', 'instances', 'imageInstances',
                   'imageInfoList', 'seriesImages', 'instanceList'];

  function asImageList(node) {
    for (var i = 0; i < LIST_KEYS.length; i++) {
      var v = node[LIST_KEYS[i]];
      if (Array.isArray(v) && v.length) return v;
    }
    return null;
  }

  function seriesMeta(node) {
    var key = node.seriesUuid || node.seriesId || node.seriesUID ||
              node.seriesInstanceUid || node.seriesNumber || '';
    var desc = node.seriesDesc || node.seriesDescription || node.bodyPart ||
               node.protocolName || node.seriesNo || node.seriesName || '';
    var mod = node.modality || node.modalityType || node.bodyPartExamined || '';
    return { key: key ? String(key) : '', desc: String(desc || ''), modality: String(mod || '') };
  }

  function ensureEntry(key, meta) {
    var k = key || 'default';
    var e = seriesMap[k];
    if (!e) {
      e = seriesMap[k] = { key: k, desc: '', modality: '', images: Object.create(null), ghosts: 0 };
    }
    if (!e.desc && meta && meta.desc) e.desc = meta.desc;
    if (!e.modality && meta && meta.modality) e.modality = meta.modality;
    return e;
  }

  function imgId(img) {
    return String(img.imageUuid || img.sopInstanceUid || img.instanceUid ||
                  img.imageId || img.rawKey || img.instanceNumber || '');
  }

  /** imageResult 里有没有「明确是 DICOM」的字段（决定 kind 与诊断提示） */
  function hasDicom(img) {
    var res = (img.imageResult && typeof img.imageResult === 'object') ? img.imageResult : {};
    for (var i = 0; i < STRONG_KEYS.length; i++) {
      if (typeof res[STRONG_KEYS[i]] === 'string' && res[STRONG_KEYS[i]]) return true;
      if (typeof img[STRONG_KEYS[i]] === 'string' && img[STRONG_KEYS[i]]) return true;
    }
    return false;
  }

  /** 从一张图里取出最像"资源路径"的那个值（用于反推 seriesUuid） */
  function pickPath(img) {
    var res = (img.imageResult && typeof img.imageResult === 'object') ? img.imageResult : {};
    var order = ['dicom', 'dicomPath', 'dicomUrl', 'jpeg', 'jpg', 'url', 'path'];
    for (var i = 0; i < order.length; i++) {
      if (typeof res[order[i]] === 'string' && res[order[i]]) return res[order[i]];
      if (typeof img[order[i]] === 'string' && img[order[i]]) return img[order[i]];
    }
    return '';
  }

  function addImage(entry, img) {
    var picked = rawCandidates(img);
    var id = imgId(img);
    if (!picked || !picked.raws.length) {
      // 有身份没地址：页面加载了缩略图，但原始地址没下发。
      // 计入 ghost 用于诊断提示（不是 bug，是部署形态）。
      if (id) {
        entry.ghosts += 1;
        ghostCount += 1;
      }
      return;
    }
    var pid = id || picked.raws[0];
    if (entry.images[pid]) return;
    entry.images[pid] = {
      // 存原值，不存拼好的 URL —— CDN 基址要到点按钮时才有，见 resolveUrls
      raws: picked.raws,
      rawKey: picked.rawKey,
      name: id || pid,
      // 最终落盘扩展名由软件端按响应魔数决定（这里只作提示）
      kind: hasDicom(img) ? 'dicom' : 'jpeg'
    };
    // 一张图都还没加载、数据里也没给域名时，趁现在把基址探出来。
    // 放在这里而不是点击时：用户点按钮那一刻结果早已就绪，不额外等待。
    try { maybeProbeBase(picked.raws[0]); } catch (e) { /* ignore */ }
  }

  // imageAssets 是 uuid -> {dicom, jpeg, rawKey} 的 map，与 imageList 平行存在
  function considerAssets(node, ctx) {
    var assets = node.imageAssets;
    if (!assets || typeof assets !== 'object' || Array.isArray(assets)) return;
    var entry = null;
    for (var uuid in assets) {
      var a = assets[uuid];
      if (!a || typeof a !== 'object') continue;
      if (!entry) entry = ensureEntry(ctx.key, ctx);
      addImage(entry, {
        imageUuid: uuid,
        imageResult: a,
        dicom: a.dicom,
        rawKey: a.rawKey
      });
    }
  }

  /**
   * 单张图响应：ftimage 在翻图时按张调接口拿地址，响应形如
   *   {dicom: "...", jpeg: "..."}
   * 既没有 imageList 也没有 seriesUuid —— 只靠递归会整条漏掉。
   *
   * 判定要够严：score>=3 才算，否则会把 /api/series/image/123 这种纯接口
   * 地址当成 dcm 去下，白白多出一堆失败项。
   */
  var lastCtx = { key: '', desc: '', modality: '' };

  function looksLikeSingleImage(node) {
    if (node.imageResult && typeof node.imageResult === 'object') return true;
    for (var i = 0; i < STRONG_KEYS.length; i++) {
      var v = node[STRONG_KEYS[i]];
      if (typeof v === 'string' && sane(v) && score(v) >= 3) return true;
    }
    return false;
  }

  function walk(node, depth, seen, ctx) {
    if (!node || typeof node !== 'object' || depth > 9) return;
    if (seen.indexOf(node) >= 0) return;
    seen.push(node);

    if (Array.isArray(node)) {
      var lim = node.length > 5000 ? 5000 : node.length;
      for (var i = 0; i < lim; i++) walk(node[i], depth + 1, seen, ctx);
      return;
    }

    var meta = seriesMeta(node);
    var nextCtx = meta.key ? meta : (ctx || { key: '', desc: '', modality: '' });

    considerAssets(node, nextCtx);

    var list = asImageList(node);
    if (list) {
      var entry = ensureEntry(nextCtx.key, nextCtx);
      if (nextCtx.key) lastCtx = nextCtx;
      var n = list.length > 5000 ? 5000 : list.length;
      for (var j = 0; j < n; j++) {
        var im = list[j];
        if (im && typeof im === 'object') addImage(entry, im);
      }
      return;                                     // 命中即止：imageList 动辄几百项，别再递归
    }

    // 单张图元素。ftimage v3 的 {rawKey, imageResult:{jpeg}} 就是这种形状 ——
    // 它既没有 imageList 也没有 seriesUuid，只能靠影像路径反推序列
    // （…/<study>/<series>/<rawKey>，倒数第二段就是 seriesUuid）。
    //
    // 这里**绝不能因为"没上下文"就丢弃**：上一版正是这样，把整个检查的图
    // 全丢了还一声不响。宁可归到 default 桶也不能丢，知骨打开时按 DICOM
    // 头里的 SeriesInstanceUID 分组，目录只是物理落点。
    if (looksLikeSingleImage(node)) {
      var p0 = pickPath(node);
      var sk = seriesFromPath(p0) || (nextCtx.key || (lastCtx && lastCtx.key) || '');
      if (nextCtx.key) lastCtx = nextCtx;
      var e2 = ensureEntry(sk || 'default', nextCtx.key ? nextCtx : null);
      addImage(e2, node);
      return;
    }

    var keys = Object.keys(node);
    for (var k = 0; k < keys.length && k < 260; k++) {
      var kn = keys[k];
      if (kn === 'imageAssets' || kn === 'imageIds' ||
          kn === 'sliceLocationList' || kn === 'imageList') continue;
      var v = node[kn];
      if (v && typeof v === 'object') walk(v, depth + 1, seen, nextCtx);
    }
  }

  function pickPatient(root) {
    if (studyInfo.patient) return;
    var d = (root.data && typeof root.data === 'object') ? root.data : {};
    var cands = [
      root.patientName, root.name,
      root.patient && root.patient.name,
      root.patient && root.patient.patientName,
      d.patientName,
      root.patientInfo && root.patientInfo.name
    ];
    for (var i = 0; i < cands.length; i++) {
      var v = cands[i];
      if (typeof v === 'string' && v && v.length < 40) { studyInfo.patient = v; return; }
    }
  }

  function ingest(obj) {
    if (!obj || typeof obj !== 'object') return;
    stats.responses += 1;
    // 先收 CDN 域名：它可能在 imageList 之外（siteConfig、检查信息里都有），
    // 而 walk 命中 imageList 就 return 不再往下递归，放后面会漏。
    try { harvestBasesFrom(obj, 0, []); } catch (e) { /* ignore */ }
    try {
      var seen = [];
      walk(obj, 0, seen, null);
    } catch (e) { /* 钩子里的异常绝不能冒泡到页面 */ }
    try { pickPatient(obj); } catch (e) { /* ignore */ }
    // 内网 Wado 的 GetStudyInfo（serieses/imgs）走这条独立通路：
    // 它的字段名（serieses、imgs、uuid）和鲁云那套没有任何交集，硬塞进
    // walk() 只会互相干扰，所以单独一个只认自己结构的解析器。
    try { wadoIngestObj(obj, 0); } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ 网络画像（诊断用）

  function topKeys(o) {
    if (!o || typeof o !== 'object') return '';
    try {
      var k = Object.keys(o);
      return k.slice(0, 8).join(',');
    } catch (e) { return ''; }
  }

  function pushNet(url, size, kind, extra, snippet) {
    if (!url) return;
    stats.net.push({
      u: String(url).slice(0, 150),
      n: size || 0,
      k: kind,
      x: extra || '',
      s: snippet ? String(snippet).replace(/\s+/g, ' ').slice(0, 200) : ''
    });
    if (stats.net.length > 60) stats.net.shift();
  }

  // 每个请求都记一条，写回结果由 scanXhr / scanResponse 补。诊断靠它看
  // 「页面到底请求了什么接口、响应是什么形状」，比只看解析成功的那几条有用得多。
  function logReq(method, url) {
    var full = String(url || '');
    var rec = { m: String(method || 'GET').toUpperCase(), u: full.slice(0, 400), r: '' };
    reqLog.push(rec);
    if (reqLog.length > 100) reqLog.shift();
    // 顺手把「浏览器真的拉过的影像地址」登记下来 —— 这是全流程里唯一
    // 无需猜测就能拿到的可靠 URL（CDN 基址、路径前缀全都现成的）。
    try { noteImageUrl(full); } catch (e) { /* ignore */ }
    // 内网 Wado 的 /GetBlock/ 同理：一条真实地址就能反证 seriesId 与基址都没错。
    try { wadoNoteUrl(full); } catch (e) { /* ignore */ }
    return rec;
  }

  function tryParseText(text, url) {
    if (typeof text !== 'string') return;
    var n = text.length;
    if (n < 2 || n > 8e6) return;
    var t = text.trim();
    var c = t.charAt(0);
    // 只处理 JSON 形状。密文（"04..."）和 HTML 在这里就被挡住，不做无谓尝试。
    if (c !== '{' && c !== '[') return;
    var obj;
    try { obj = origParse.call(JSON, t); } catch (e) { return; }
    if (url) pushNet(url, n, 'json', topKeys(obj));
    ingest(obj);
    sweepSignedUrls(t);          // 预签名地址直捞（S3 部署，见函数头注释）
  }

  function decodeText(ab) {
    try {
      var u8 = new Uint8Array(ab);
      if (u8.length < 2) return '';
      if (u8[0] !== 0x7b && u8[0] !== 0x5b) return '';   // 不是 { 或 [
      if (typeof TextDecoder === 'function') return new TextDecoder('utf-8').decode(u8);
      var s = '', step = 8192;
      for (var i = 0; i < u8.length; i += step) {
        s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + step, u8.length)));
      }
      return decodeURIComponent(escape(s));
    } catch (e) { return ''; }
  }

  // ------------------------------------------------------------------ 预签名地址直捞

  /**
   * ftimage V3 的新部署（临沂市兰山区人民医院等）把影像放到 AWS S3，
   * 取图地址带预签名 query（AWSAccessKeyId / Expires / Signature），
   * **没有签名必 403**。签名地址可能出现在任何解密后的响应里
   * （imagePR 预签列表、pat/image、imageResult.dicom……），不一定是
   * imageList —— walk() 的结构识别管不到，按 URL 特征直接捞最稳：
   * 带 query 且 query 里有 Signature 的 http(s) 地址，交给 noteImageUrl
   * 按影像域名过滤登记。捞到的地址在 rawKey 精确命中时原样使用（签名齐全）。
   */
  var SIGNED_URL_RE = /https?:\/\/[^\s"'<>()]+\?[^\s"'<>()]*[Ss]ignature=[^\s"'<>()]+/g;

  function sweepSignedUrls(text) {
    if (typeof text !== 'string' || text.length < 40 || text.length > 8e6) return;
    if (!sweepOne(text) && text.indexOf('\\/') >= 0) {
      // JSON 序列化常把 / 转义成 \/ —— 正则在 https:\/\/ 开头就断了，
      // 原文捞不到时对还原后的副本再捞一次（去重由 noteImageUrl 负责）。
      sweepOne(text.replace(/\\\//g, '/'));
    }
  }

  function sweepOne(text) {
    var hit = 0;
    var m, n = 0;
    SIGNED_URL_RE.lastIndex = 0;
    while ((m = SIGNED_URL_RE.exec(text)) !== null && n < 500) {
      // 尾部可能带上 JSON 的转义反斜杠或逗号，剥掉（会污染签名导致 403）。
      var u = m[0].replace(/[\\,;]+$/, '');
      try { hit += noteImageUrl(u); } catch (e) { /* ignore */ }
    }
    return hit;
  }

  // ------------------------------------------------------------------ 钩子 A：JSON.parse

  var origParse = JSON.parse;

  JSON.parse = function (text, reviver) {
    var obj = origParse.call(this, text, reviver);
    try {
      if (typeof text === 'string' && text.length > 20 && text.length < 8e6) {
        // 性能预筛。注意 imageResult / rawKey 必须在内：鲁云 V3 的影像清单
        // 里一个 imageList/dicom 字样都没有，只有 {rawKey, imageResult:{jpeg}}，
        // 少了这两个词整条响应会被直接跳过（上一版就是这么漏的）。
        //
        // 域名类字段和 `ftimage.` 也必须在内：影像域名常单独放在 siteConfig
        // 之类的小响应里，一个影像关键词都没有 —— 之前正是被这里挡掉，
        // 导致「接口里明明给了域名，插件却一条都没收到」。
        if (/imageUuid|imageList|imageAssets|imageResult|seriesUuid|rawKey|dicom|sopInstanceUid|jpgUseDomain|imageServerUrl|imageDomain|imgDomain|cdnUrl|cdnHost|imageBaseUrl|baseImageUrl|imageHost|ftimage\./i.test(text)) {
          stats.parsed += 1;
          // 记一条画像：诊断时能看出这 14 条命中到底是什么，而不是只看到"没找到"。
          // 没捞到图的那种额外存 200 字符样本 —— 下一次就不用猜结构了。
          var before = totalImages();
          ingest(obj);
          sweepSignedUrls(text);   // 预签名地址直捞（S3 部署，见函数头注释）
          var grown = totalImages() > before;
          pushNet('JSON.parse#' + stats.parsed, text.length,
                  grown ? 'parse-hit' : 'parse-miss', topKeys(obj),
                  grown ? '' : text);
        } else if (/serieses|hasseries/i.test(text)) {
          // 内网 Wado 的 GetStudyInfo：字段名（serieses/imgs/uuid）和鲁云
          // 那套零交集，上面那个正则永远不命中，所以单独开一条分支。
          // 不在这里接一手的话，这个响应会被静默跳过 —— 表现就是
          // 「页面明明打开了检查，扩展却说 0 张」。
          var wbefore = wadoTotal();
          try { wadoIngestObj(obj, 0); } catch (e) { /* ignore */ }
          pushNet('GetStudyInfo', text.length,
                  wadoTotal() > wbefore ? 'wado-hit' : 'wado-miss', topKeys(obj),
                  wadoTotal() > wbefore ? '' : text);
        }
      }
    } catch (e) { /* ignore */ }
    return obj;
  };

  // ------------------------------------------------------------------ 钩子 B：fetch

  try {
    var origFetch = window.fetch;
    if (typeof origFetch === 'function') {
      window.fetch = function (input, init) {
        var url = '';
        try {
          url = typeof input === 'string' ? input : ((input && input.url) || '');
        } catch (e) { url = ''; }
        var rec = logReq((init && init.method) || (input && input.method) || 'GET', url);
        var p = origFetch.apply(this, arguments);
        try {
          p.then(function (resp) { try { scanResponse(resp, rec); } catch (e) {} },
                 function () { if (rec) rec.r = '失败'; });
        } catch (e) { /* ignore */ }
        return p;
      };
    }
  } catch (e) { /* ignore */ }

  function scanResponse(resp, rec) {
    if (!resp || typeof resp.clone !== 'function') return;
    try {
      var ct = '';
      try { ct = (resp.headers && resp.headers.get('content-type')) || ''; } catch (e) { ct = ''; }
      if (ct && !/json|text|plain|javascript/i.test(ct)) {
        if (rec) rec.r = 'skip ' + ct.split(';')[0];
        return;
      }
      if (/image\/|octet-stream|pdf|font|video\//i.test(ct)) {
        if (rec) rec.r = 'skip ' + ct.split(';')[0];
        return;
      }
      var url = '';
      try { url = resp.url || ''; } catch (e) { url = ''; }
      resp.clone().text().then(function (t) {
        try {
          if (rec) rec.r = 'text ' + t.length + 'B ' + String(t).slice(0, 24).replace(/\s+/g, ' ');
          tryParseText(t, url);
        } catch (e) { /* ignore */ }
      }, function () { /* ignore */ });
    } catch (e) { /* ignore */ }
  }

  try {
    if (window.Response && Response.prototype && Response.prototype.json) {
      var origJson = Response.prototype.json;
      Response.prototype.json = function () {
        return origJson.apply(this, arguments).then(function (d) {
          try { ingest(d); } catch (e) { /* ignore */ }
          return d;
        });
      };
    }
  } catch (e) { /* ignore */ }

  try {
    if (window.Response && Response.prototype && Response.prototype.text) {
      var origText = Response.prototype.text;
      Response.prototype.text = function () {
        var self = this;
        return origText.apply(this, arguments).then(function (t) {
          try {
            var u = '';
            try { u = self.url || ''; } catch (e) { u = ''; }
            tryParseText(t, u);
          } catch (e) { /* ignore */ }
          return t;
        });
      };
    }
  } catch (e) { /* ignore */ }

  // ------------------------------------------------------------------ 钩子 C+D：XHR

  function scanXhr(xhr) {
    if (!xhr || xhr.__zgsaveScanned) return;
    xhr.__zgsaveScanned = true;
    var rec = xhr.__zgsaveRec;
    try {
      var rt = '';
      try { rt = xhr.responseType || ''; } catch (e) { rt = ''; }
      var url = xhr.__zgsaveUrl || '';

      if (rt === '' || rt === 'text') {
        var t = '';
        try { t = xhr.responseText || ''; } catch (e) { t = ''; }
        if (t) {
          var c = t.trim().charAt(0);
          if (rec) {
            rec.r = (c === '{' || c === '[')
              ? ('json ' + t.length + 'B')
              : ('非JSON ' + String(t).slice(0, 14).replace(/\s+/g, ' '));
          }
          tryParseText(t, url);
        } else if (rec) {
          rec.r = '空';
        }
        return;
      }
      if (rt === 'json') {
        var r = null;
        try { r = xhr.response; } catch (e) { r = null; }
        if (r && typeof r === 'object') {
          if (rec) rec.r = 'json ' + topKeys(r);
          if (url) pushNet(url, 0, 'json*', topKeys(r));
          ingest(r);
        } else if (rec) {
          rec.r = 'json 空';
        }
        return;
      }
      if (rt === 'arraybuffer') {
        var ab = null;
        try { ab = xhr.response; } catch (e) { ab = null; }
        if (ab && ab.byteLength && ab.byteLength < 4e6) {
          var txt = decodeText(ab);
          if (txt) {
            if (rec) rec.r = 'ab→json ' + txt.length + 'B';
            tryParseText(txt, url);
          } else if (rec) {
            rec.r = 'ab ' + ab.byteLength + 'B';
          }
        } else if (rec && ab) {
          rec.r = 'ab ' + ab.byteLength + 'B（跳过）';
        }
      }
    } catch (e) { /* ignore */ }
  }

  try {
    var XHRp = window.XMLHttpRequest && XMLHttpRequest.prototype;
    if (XHRp) {
      var origOpen = XHRp.open;
      XHRp.open = function (m, u) {
        try {
          this.__zgsaveUrl = (typeof u === 'string') ? u : '';
          this.__zgsaveScanned = false;
          this.__zgsaveRec = logReq(m, u);
        } catch (e) { /* ignore */ }
        return origOpen.apply(this, arguments);
      };

      var origSend = XHRp.send;
      XHRp.send = function () {
        var self = this;
        try { self.addEventListener('loadend', function () { scanXhr(self); }, false); }
        catch (e) { /* ignore */ }
        return origSend.apply(this, arguments);
      };

      // 页面自己读响应时也顺手扫一遍（覆盖 loadend 之前就读到的情况）
      ['response', 'responseText'].forEach(function (name) {
        try {
          var d = Object.getOwnPropertyDescriptor(XHRp, name);
          if (!d || !d.get) return;
          Object.defineProperty(XHRp, name, {
            configurable: true,
            enumerable: d.enumerable,
            get: function () {
              var v = d.get.call(this);
              try {
                if (!this.__zgsaveScanned) {
                  if (typeof v === 'string') tryParseText(v, this.__zgsaveUrl);
                  else if (v && typeof v === 'object') ingest(v);
                }
              } catch (e) { /* ignore */ }
              return v;
            }
          });
        } catch (e) { /* ignore */ }
      });
    }
  } catch (e) { /* ignore */ }

  // ------------------------------------------------------------------ 钩子 E：全局回捞

  // 用途只有一个：万一钩子装晚了（页面在我们之前就把 imageList 拉完了），
  // 从 store / 会话缓存里再捞一次。正常情况下这里是空转。
  function scanGlobals() {
    var names = ['__INITIAL_STATE__', '__NUXT__', '__APOLLO_STATE__',
                 '__store__', 'appData', 'globalData'];
    for (var i = 0; i < names.length; i++) {
      try {
        var v = window[names[i]];
        if (v && typeof v === 'object') ingest(v);
      } catch (e) { /* ignore */ }
    }

    // Vue 2 / Vue 3
    try {
      var el = document.querySelector('#app') || document.body;
      var guard = 0;
      while (el && guard++ < 10) {
        var inst = el.__vue__ ||
                   (el.__vueParentComponent && (el.__vueParentComponent.ctx || el.__vueParentComponent.proxy));
        if (inst) {
          try { ingest((inst.$store && inst.$store.state) || inst.$data || inst); } catch (e) {}
          try {
            if (inst.$root && inst.$root !== inst) {
              ingest((inst.$root.$store && inst.$root.$store.state) || inst.$root.$data || {});
            }
          } catch (e) {}
          break;
        }
        el = el.parentElement;
      }
    } catch (e) { /* ignore */ }

    // 会话缓存：SPA 常把 studyData 序列化塞在这里
    try {
      var stores = [window.sessionStorage, window.localStorage];
      for (var si2 = 0; si2 < stores.length; si2++) {
        var ss = stores[si2];
        if (!ss) continue;
        var cnt = 0;
        for (var key in ss) {
          if (cnt++ > 40) break;
          var val = null;
          try { val = ss.getItem(key); } catch (e) { continue; }
          if (val && val.length > 60 && val.length < 4e6 &&
              (val.indexOf('imageUuid') >= 0 || val.indexOf('dicom') >= 0 ||
               val.indexOf('imageList') >= 0)) {
            tryParseText(val, (si2 ? 'localStorage:' : 'sessionStorage:') + key);
          }
        }
      }
    } catch (e) { /* ignore */ }

    hookCryptoLib();

    // 内网 Wado：viewer 实例就挂在 window 上，直接读它比任何钩子都直接。
    // 放在这里是因为 scanGlobals 在 1.8s / 5s / 12s / 25s 各跑一次，
    // 正好覆盖「页面还在初始化 GetStudyInfo」的那段时间。
    try { wadoIngestViewer(null); } catch (e) { /* ignore */ }
  }

  /**
   * 国密解密库兜底钩子。
   *
   * ftimage 系的响应是 SM2 密文，前端用 `Sz.decrypt()` 解。如果这个库挂在
   * window 上，直接钩它的 decrypt 比等 JSON.parse 更早、更稳 —— 尤其是
   * 某些打包器会缓存 `var _parse = JSON.parse` 的原始引用，让 JSON.parse 钩子失效。
   */
  function hookCryptoLib() {
    var names = ['Sz', 'sz', 'SM2', 'sm2', 'DecryptUtil', 'decryptUtil', 'CryptoUtil'];
    for (var i = 0; i < names.length; i++) {
      var lib;
      try { lib = window[names[i]]; } catch (e) { continue; }
      if (!lib || typeof lib !== 'object' || hookedLibs.indexOf(lib) >= 0) continue;
      hookedLibs.push(lib);
      ['decrypt', 'decode', 'sm2Decrypt', 'sm4Decrypt'].forEach(function (fn) {
        var orig;
        try { orig = lib[fn]; } catch (e) { return; }
        if (typeof orig !== 'function') return;
        try {
          lib[fn] = function () {
            var r = orig.apply(this, arguments);
            try {
              if (r && typeof r === 'object') ingest(r);
              else if (typeof r === 'string' && r.length > 20) tryParseText(r, 'crypto.' + fn);
            } catch (e) { /* ignore */ }
            return r;
          };
        } catch (e) { /* 只读属性，跳过 */ }
      });
    }
  }

  // ============================================================ 内网 WADO 适配层
  //
  // 适用对象：医院内网「临床系统 + WebDicomView」这一套（实测 172.123.3.5:11519
  // 前端、172.123.3.5:8989 Wado 取图）。它和鲁云 ftimage 完全不是一回事：
  //
  //   鲁云：接口下发影像清单（JSON 里带 CDN 相对路径），URL 要反推 CDN 基址
  //   内网：**没有清单接口**。页面初始化时调一次 GetStudyInfo，拿回检查/序列/
  //         图像 id，之后每张图按 id 单独取：
  //
  //   GET {wado}/GetBlock/{imageId}?studyuid=..&hospitalid=..&seriesId=..
  //       &blockIndex=0&imageType=1
  //
  // 所以这里的活儿不是"抓 URL"，而是"读懂它的内存模型，把 URL 拼出来"。
  // 三条线索按可信度从高到低：
  //   ① window.webDicomView.studyArr[].arrSeries[].arrImage[].id —— 页面自己的对象，
  //      序列/图像 id 齐全（含 user 没翻到的那些），最完整
  //   ② GetStudyInfo 的响应 JSON（serieses[].imgs[].id）—— ① 拿不到时兜底，
  //      也覆盖"钩子装晚了、模型已被销毁"的情况
  //   ③ 浏览器真实请求过的 /GetBlock/ 地址 —— 永远正确但只有翻过的那几张，
  //      作为最终兜底（同时也是 ① ② 拼出的 URL 的"验货标准"）
  //
  // ⚠️ 拼 URL 时 seriesId 必须取「去掉 query 的 uuid」：页面在 flatSeries 模式下
  //    会给 uuid 追加 `?index=N`（见 WebDicomView 的 setSeries），原样拼进去会把
  //    整个查询串截断，服务端只会返回一张图或直接 500。

  var WADO = {
    wado: '',                 // http://host:8989/Wado
    hospId: '',               // 111
    studyUID: '',
    jpgOnly: false,           // 检查只有 JPEG（totaltype=JPG），此时 GetBlock 不出图
    series: Object.create(null),
    real: Object.create(null),   // imageId -> 浏览器真拉过的 GetBlock 绝对地址
    realN: 0,                    // 去重后的真实地址条数
    note: '',
    src: { viewer: 0, json: 0, real: 0 }
  };
  var WADO_REAL_MAX = 3000;

  function wadoKey(v) {
    return String(v == null ? '' : v).split('?')[0].split('#')[0];
  }

  function wadoEl(id) {
    try {
      var el = document.getElementById(id);
      if (el && typeof el.value === 'string') return el.value.trim();
    } catch (e) { /* ignore */ }
    return '';
  }

  /** 找页面的 viewer 实例。页面脚本是 `webDicomView = new WebDicomView(...)`（无 var，
   *  落在 window 上），构造里还会挂一份到传入的配置对象。两条都试，再退到鸭子类型扫描。 */
  function wadoViewer() {
    var cands = [];
    try { if (window.webDicomView) cands.push(window.webDicomView); } catch (e) { /* ignore */ }
    try {
      if (window.config && window.config.webDicomView) cands.push(window.config.webDicomView);
    } catch (e) { /* ignore */ }
    var i;
    for (i = 0; i < cands.length; i++) {
      var v = cands[i];
      if (v && typeof v === 'object' && (v.studyArr || v.wado)) return v;
    }
    // 兜底：换个变量名也能认出来。只扫一遍、限 500 个 key，取不出就算了 ——
    // 这个函数在点按钮/定时器里都会调，绝不能因为 window 上有个 getter 抛错就卡住。
    try {
      var keys = Object.keys(window);
      for (i = 0; i < keys.length && i < 500; i++) {
        var o;
        try { o = window[keys[i]]; } catch (e) { continue; }
        if (o && typeof o === 'object' && Array.isArray(o.studyArr) &&
            typeof o.wado === 'string' && /wado/i.test(o.wado)) {
          return o;
        }
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  function wadoBase() {
    if (WADO.wado) return WADO.wado;
    var b = wadoEl('hdurl');
    if (b) { WADO.wado = b.replace(/\/+$/, ''); return WADO.wado; }
    var v = wadoViewer();
    if (v && typeof v.wado === 'string' && /^https?:\/\//i.test(v.wado)) {
      WADO.wado = v.wado.replace(/\/+$/, '');
      return WADO.wado;
    }
    // Viewer.html 的地址形如 http://host:8989/Wado/Viewer?studyUid=…
    try {
      var p = location.pathname;
      var i = p.toLowerCase().indexOf('/wado');
      if (i >= 0) {
        WADO.wado = location.origin + p.slice(0, i + 5);
        return WADO.wado;
      }
    } catch (e) { /* ignore */ }
    return '';
  }

  function wadoHosp() {
    if (WADO.hospId) return WADO.hospId;
    var h = wadoEl('hdhospid');
    if (!h) {
      var v = wadoViewer();
      if (v && v.hospId) h = String(v.hospId);
    }
    if (h) WADO.hospId = h;
    return WADO.hospId;
  }

  function wadoStudy() {
    if (WADO.studyUID) return WADO.studyUID;
    var s = wadoEl('hdstuuid');
    if (!s) {
      var m = /[?&]study_?u?id=([^&#]+)/i.exec(location.search || '');
      if (m) { try { s = decodeURIComponent(m[1]); } catch (e) { s = m[1]; } }
    }
    if (!s) {
      var v = wadoViewer();
      try {
        if (v && v.studyUIDArr && v.studyUIDArr.length) s = v.studyUIDArr[0];
        else if (v && v.studyArr && v.studyArr[0] && v.studyArr[0].studyuid) {
          s = v.studyArr[0].studyuid;
        }
      } catch (e) { /* ignore */ }
    }
    if (s) WADO.studyUID = String(s);
    return WADO.studyUID;
  }

  function wadoThumbUrl(imageId, seriesId, sign) {
    var b = wadoBase();
    if (!b) return '';
    var uid = wadoStudy() || '';
    var hosp = wadoHosp() || '';
    var u = b + '/GetImageThumb/' + imageId +
            '?studyuid=' + uid + '&studyUID=' + uid +
            '&seriesId=' + seriesId + '&seriesID=' + seriesId +
            '&hospId=' + hosp + '&hospID=' + hosp;
    if (sign) u += '&sign=' + sign;
    return u;
  }

  /**
   * 拼一张原始 DICOM 的地址。刻意和厂商代码保持一致的**原样拼接**
   * （不做 URL 编码）：页面里 imageId / seriesId 都是纯 uid，编码反而会把
   * 服务端的精确匹配弄坏；真出现特殊字符时，② 的真实地址优先顶上。
   */
  function wadoBlockUrl(imageId, seriesId) {
    var b = wadoBase();
    if (!b || !imageId) return '';
    var uid = wadoStudy() || '';
    var hosp = wadoHosp() || '0';
    return b + '/GetBlock/' + imageId +
           '?studyuid=' + uid +
           '&hospitalid=' + hosp +
           '&seriesId=' + (seriesId || '') +
           '&blockIndex=0&imageType=1';
  }

  /** 记录一条浏览器真拉过的 GetBlock 地址（比任何拼接都可信）。 */
  function wadoNoteUrl(full) {
    if (!full || typeof full !== 'string') return;
    var m = /\/GetBlock\/([^\/?#]+)/i.exec(full);
    if (!m) return;
    var id = m[1];
    try { id = decodeURIComponent(id); } catch (e) { /* 保留原样 */ }
    var clean = full.split('#')[0];
    if (WADO.real[id] === clean) return;
    if (!WADO.real[id]) WADO.realN += 1;
    WADO.real[id] = clean;
    if (WADO.realN > WADO_REAL_MAX) {          // 极端长会话，丢最老的
      var keys = Object.keys(WADO.real);
      for (var i = 0; i < keys.length - WADO_REAL_MAX; i++) delete WADO.real[keys[i]];
    }
  }

  function wadoEntry(key, desc, mod) {
    var k = key || 'default';
    var e = WADO.series[k];
    if (!e) {
      e = WADO.series[k] = { key: k, desc: '', modality: '', studyUID: '',
                             images: Object.create(null) };
    }
    if (!e.desc && desc) e.desc = String(desc);
    if (!e.modality && mod) e.modality = String(mod);
    return e;
  }

  function wadoAddImage(entry, id, num, sign) {
    if (id === undefined || id === null) return 0;
    var sid = String(id);
    if (!sid || sid.length > 200) return 0;
    var old = entry.images[sid];
    var n = Number(num);
    if (old) {
      if (!old.num && n) old.num = n;
      return 0;
    }
    entry.images[sid] = { id: sid, num: isNaN(n) ? 0 : n, sign: sign ? String(sign) : '' };
    return 1;
  }

  /**
   * 吃一份 GetStudyInfo 形态的 JSON。
   *
   * 真实结构（从 WebDicomView 的 study 构造反推，字段名一个都不能少）：
   *   { studyuid, studyuuid, name, sex, age, modality, imgcount, seriescount,
   *     hasseries, totaltype, studydate, studyid, patientid, orgname,
   *     serieses: [ { uuid, uid, num, desp, sign, flatSeries,
   *                   imgs: [ { id, num, url?, acqtm? } ] } ],
   *     imgs: [...]   // hasseries 为假时用它（此时只有缩略图） }
   */
  function wadoIngestStudy(st) {
    if (!st || typeof st !== 'object') return 0;
    if (!WADO.studyUID && typeof st.studyuid === 'string' && st.studyuid) {
      WADO.studyUID = st.studyuid;
    }
    if (typeof st.totaltype === 'string' && st.totaltype) {
      WADO.jpgOnly = /^JPG$/i.test(st.totaltype);
    }
    var mod = typeof st.modality === 'string' ? st.modality : '';
    var made = 0;
    var i, j;

    var list = Array.isArray(st.serieses) ? st.serieses : null;
    if (list && list.length) {
      for (i = 0; i < list.length && i < 200; i++) {
        var s = list[i];
        if (!s || typeof s !== 'object') continue;
        var key = wadoKey(s.uuid || s.uid || ('s' + i));
        var e = wadoEntry(key, s.desp || s.desc || '', mod);
        if (!e.studyUID) e.studyUID = WADO.studyUID;
        var imgs = Array.isArray(s.imgs) ? s.imgs : (Array.isArray(s.images) ? s.images : []);
        for (j = 0; j < imgs.length && j < 5000; j++) {
          var im = imgs[j];
          if (!im || typeof im !== 'object') continue;
          made += wadoAddImage(e, im.id, im.num, s.sign);
        }
      }
      return made;
    }

    if (Array.isArray(st.imgs) && st.imgs.length) {
      var e2 = wadoEntry(wadoKey(st.uuid || 'default'), st.name || '', mod);
      if (!e2.studyUID) e2.studyUID = WADO.studyUID;
      for (j = 0; j < st.imgs.length && j < 5000; j++) {
        var im2 = st.imgs[j];
        if (!im2 || typeof im2 !== 'object') continue;
        made += wadoAddImage(e2, im2.id, im2.num, '');
      }
    }
    return made;
  }

  /** 从页面自己的 viewer 实例里读序列/图像 id（最完整的一路）。 */
  function wadoIngestViewer(v) {
    if (!v) v = wadoViewer();
    var arr = v && v.studyArr;
    if (!arr || !arr.length) return 0;
    var made = 0, i, j, k;
    for (i = 0; i < arr.length && i < 20; i++) {
      var st = arr[i];
      if (!st || typeof st !== 'object') continue;
      if (st.hospId && !WADO.hospId) WADO.hospId = String(st.hospId);
      if (!WADO.studyUID && typeof st.studyuid === 'string') WADO.studyUID = st.studyuid;
      var ser = st.arrSeries || [];
      for (j = 0; j < ser.length && j < 200; j++) {
        var s = ser[j];
        if (!s || typeof s !== 'object') continue;
        var key = wadoKey(s.uuid || s.uid || ('v' + i + '_' + j));
        var e = wadoEntry(key, s.desp || '', s.modality || '');
        if (!e.studyUID) e.studyUID = s.studyUID || WADO.studyUID;
        if (!WADO.wado && typeof s.wado === 'string' && s.wado) {
          WADO.wado = s.wado.replace(/\/+$/, '');
        }
        var ims = s.arrImage || s.images || [];
        for (k = 0; k < ims.length && k < 5000; k++) {
          var im = ims[k];
          if (!im || typeof im !== 'object') continue;
          made += wadoAddImage(e, im.id, im.num, '');
        }
      }
    }
    if (made) WADO.src.viewer += made;
    return made;
  }

  /** 在任意 JSON 里找 GetStudyInfo 形态的节点（响应可能被包在 data/result 里）。 */
  function wadoIngestObj(node, depth) {
    if (!node || typeof node !== 'object') return 0;
    if (depth > 4) return 0;
    if (Array.isArray(node)) {
      var n0 = 0;
      for (var a = 0; a < node.length && a < 40; a++) n0 += wadoIngestObj(node[a], depth + 1);
      return n0;
    }
    if (Array.isArray(node.serieses) && node.serieses.length) {
      var n1 = wadoIngestStudy(node);
      if (n1) WADO.src.json += n1;
      return n1;
    }
    if (Array.isArray(node.imgs) && node.imgs.length &&
        (node.uuid || node.uid || node.studyuid)) {
      var n2 = wadoIngestStudy(node);
      if (n2) WADO.src.json += n2;
      return n2;
    }
    var keys = Object.keys(node);
    var out = 0;
    for (var i = 0; i < keys.length && i < 40; i++) {
      var v = node[keys[i]];
      if (v && typeof v === 'object') out += wadoIngestObj(v, depth + 1);
    }
    return out;
  }

  function wadoIngestText(text) {
    if (typeof text !== 'string' || text.length < 20 || text.length > 8e6) return 0;
    var t = text.trim();
    if (t.charAt(0) !== '{' && t.charAt(0) !== '[') return 0;
    var obj;
    try { obj = origParse.call(JSON, t); } catch (e) { return 0; }
    return wadoIngestObj(obj, 0);
  }

  function wadoTotal() {
    var n = 0;
    for (var k in WADO.series) {
      for (var p in WADO.series[k].images) n++;
    }
    return n;
  }

  /** 模型一条都读不到时，用「浏览器真拉过的 GetBlock 地址」反推序列。 */
  function wadoFromReal() {
    for (var id in WADO.real) {
      var u = WADO.real[id];
      var m = /[?&]seriesId=([^&]*)/i.exec(u);
      var key = '';
      if (m) { try { key = decodeURIComponent(m[1]); } catch (e) { key = m[1]; } }
      var e = wadoEntry(wadoKey(key) || 'real', '', '');
      if (!e.images[id]) e.images[id] = { id: id, num: 0, sign: '', realOnly: true };
    }
  }

  /**
   * 产出清单（点按钮时调用）。返回的 URL 一律是绝对地址 —— 这里不需要
   * 鲁云那套 candidates() 反推，Wado 的基址是页面明写出来的。
   */
  function wadoSeries() {
    try { wadoIngestViewer(null); } catch (e) { /* ignore */ }
    if (!wadoTotal()) { try { wadoFromReal(); } catch (e) { /* ignore */ } }

    var base = wadoBase();
    var uid = wadoStudy();
    var out = [];
    for (var k in WADO.series) {
      var s = WADO.series[k];
      var stu = s.studyUID || uid;
      var ids = Object.keys(s.images);
      if (!ids.length) continue;
      ids.sort(function (x, y) {
        var ax = s.images[x], ay = s.images[y];
        if (ax.num !== ay.num) return ax.num - ay.num;
        return x < y ? -1 : (x > y ? 1 : 0);
      });
      var images = [];
      for (var i = 0; i < ids.length; i++) {
        var id = ids[i];
        var it = s.images[id];
        var url = WADO.real[id] || '';
        var alt = [];
        if (!url) {
          url = wadoBlockUrl(id, s.key);
          // 缩略图兜底：只在「这检查本来就只有 JPEG」或「签名齐」时给，
          // 否则每张多一次必败请求，反而把下载端的熔断计数打满。
          if (url && (WADO.jpgOnly || it.sign)) {
            var th = wadoThumbUrl(id, s.key, it.sign);
            if (th) alt.push(th);
          }
        }
        if (!url) continue;
        images.push({ url: url, alt: alt, name: id, kind: 'dicom' });
      }
      if (!images.length) continue;
      var e2 = WADO.series[k];
      if (!e2.studyUID) e2.studyUID = stu;
      out.push({
        key: s.key, desc: s.desc || '', modality: s.modality || '',
        kind: 'dicom', images: images
      });
    }
    if (!base) WADO.note = '取图基址未找到（hdurl / viewer.wado 都为空）';
    else if (!uid) WADO.note = '检查号 studyUid 未找到';
    else if (!out.length) WADO.note = '有 viewer 但没读到图像 id（页面可能还没初始化完）';
    return out;
  }

  function wadoInfo() {
    return {
      active: !!(wadoTotal() || WADO.realN),
      base: WADO.wado || '',
      hosp: WADO.hospId || '',
      study: WADO.studyUID || '',
      series: (function () { var n = 0; for (var k in WADO.series) n++; return n; })(),
      images: wadoTotal(),
      real: WADO.realN,
      jpgOnly: !!WADO.jpgOnly,
      fromViewer: WADO.src.viewer,
      fromJson: WADO.src.json,
      note: WADO.note || ''
    };
  }

  // ------------------------------------------------------------------ 对外接口

  function totalImages() {
    var n = 0;
    for (var k in seriesMap) {
      for (var p in seriesMap[k].images) n++;
    }
    return n;
  }

  function snapshot() {
    // 拼 URL 之前先补采一次：把浏览器已经拉过的影像地址全收进来。
    // 用户可能只翻了前几张图，或者轮播只渲染了首屏，靠这一趟就能拿到
    // 真实 CDN 基址，剩下的图按基址拼，不必要求他手滑到最后一页。
    try { harvestRealUrls(); } catch (e) { /* ignore */ }
    // 内网 Wado：序列/图像 id 在页面自己的 viewer 对象里，先读一遍再判断要不要兜底。
    var wado = [];
    try { wado = wadoSeries(); } catch (e) { wado = []; }
    // 接口清单一个都没抓到时兜底建一个序列（页面直出 <img> / 钩子降级）。
    if (!totalImages() && !wado.length) {
      try { buildFromDomSeries(); } catch (e) { /* ignore */ }
    }
    var series = [];
    var total = 0;
    for (var key in seriesMap) {
      var s = seriesMap[key];
      var images = [];
      for (var pid in s.images) {
        var it = s.images[pid];
        // 到这里才拼 URL：CDN 基址（imageBase）和精确命中（imgByKey）
        // 都是页面真实请求过图片之后才有的，早一步拼就是 404。
        var urls = resolveUrls(it);
        if (!urls.length) continue;
        images.push({
          url: urls[0],
          alt: urls.slice(1, 4),
          name: it.name,
          kind: it.kind || ''
        });
      }
      if (!images.length) continue;
      total += images.length;
      series.push({
        key: s.key, desc: s.desc, modality: s.modality,
        kind: (images[0] && images[0].kind) || '',
        images: images
      });
    }
    // 内网 Wado 的序列排后面：两套并存时（几乎不会）也不会把鲁云那份挤掉。
    for (var wi = 0; wi < wado.length; wi++) {
      if (!wado[wi] || !wado[wi].images || !wado[wi].images.length) continue;
      total += wado[wi].images.length;
      series.push(wado[wi]);
    }
    return {
      series: series, total: total,
      title: document.title || '',
      patient: studyInfo.patient || '',
      // 采集这一份数据的**帧地址**。内网临床系统把 viewer 放在跨端口 iframe 里，
      // 顶层帧并不认识 172.123.3.5:8989，只有子帧地址才能当 Referer 用 ——
      // 少了它，GetBlock 的防盗链会直接 403。
      srcUrl: location.href,
      // CDN 根（从真实请求反推）。软件端用它拼兜底候选，没有也不影响下载。
      imageBase: imageBase,
      // 一条真实影像地址都没采到 = 下面所有 URL 都是猜的，几乎必 404。
      // 前端据此给出「先翻几张图」的提示，而不是放出去让用户干等。
      baseReady: knownUrls.length > 0,
      knownCount: knownUrls.length,
      // 内网 Wado 的取数情况（诊断面板直接显示）
      wado: wadoInfo(),
      // 哪一份 inject.js 回的。content.js 必须认主世界那份 —— 隔离世界那份
      // 钩子是死的，数据恒为空，让它抢先回就会把真结果盖掉。
      world: IN_MAIN ? 'main' : 'isolated',
      hookOk: IN_MAIN
    };
  }

  function diagnose() {
    // 先看世界，再补采，最后才统计。
    //
    // 补采必须放在这里：knownUrls 平时只由 fetch/XHR 钩子喂，而图片是浏览器
    // 原生加载的（<img src>），根本不经过那两个钩子。钩子一旦降级到隔离世界
    // （Chrome<111 或 CSP 拦注入），knownCount 就恒为 0，诊断会显示「页面还没
    // 加载过任何一张图」—— 而 performance 里明明躺着 6 条真实地址。这条假
    // 信息会把排查方向整个带偏。
    try { harvestRealUrls(); } catch (e) { /* ignore */ }
    try { wadoIngestViewer(null); } catch (e) { /* ignore */ }
    var series = [];
    for (var k in seriesMap) {
      var s = seriesMap[k];
      var n = 0;
      for (var p in s.images) n++;
      series.push({ key: s.key, desc: s.desc, modality: s.modality, images: n, ghosts: s.ghosts });
    }
    // 内网 Wado 的序列也列进「序列明细」，否则面板会显示 0 个却实际能取图
    for (var wk in WADO.series) {
      var ws = WADO.series[wk];
      var wn = 0;
      for (var wp in ws.images) wn++;
      if (wn) series.push({ key: ws.key, desc: ws.desc, modality: ws.modality, images: wn, ghosts: 0 });
    }
    var wadoNow = wadoInfo();
    if (wadoNow.images) wadoNow.base = wadoBase();
    if (wadoNow.images) wadoNow.study = wadoStudy();
    return {
      version: window.__zgsaveBridgeVersion,
      // 隔离世界 = 钩子无效，诊断面板要据此给出可操作的指引
      world: IN_MAIN ? 'main' : 'isolated',
      hookOk: IN_MAIN,
      uptime: Math.round((Date.now() - BOOT_TS) / 1000),
      href: location.href,
      hash: location.hash || '',
      docTitle: document.title || '',
      isTop: (window === window.top),
      responses: stats.responses,
      parsed: stats.parsed,
      series: series,
      total: totalImages(),
      // 内网 Wado 取数情况：active=false 且 images=0 说明这套页面不是 Wado 形态
      wado: wadoNow,
      ghosts: ghostCount,
      // 影像 CDN 情况：assetKeys 直接回答「这个部署到底给不给 dicom」
      imageBase: imageBase,
      ctSeen: ctSeen,
      // 采到几条浏览器真拉过的影像地址。为 0 = 页面一张图都没加载过，
      // 拼出来的 URL 必然是猜的（也就是 404 的根源），诊断里要能一眼看出。
      knownCount: knownUrls.length,
      knownSample: knownUrls.slice(-2).map(function (s) {
        return s.length > 130 ? (s.slice(0, 60) + '…' + s.slice(-60)) : s;
      }),
      // 从接口数据里取到的 CDN 域名。有它才能拼对，为 0 说明这个部署
      // 把域名写在前端代码里（只能靠 performance/探针，见 imgRes）。
      cdnBases: cdnBases.slice(-6),
      probe: probeState.note,
      baseFields: (function () {
        try {
          var out = [];
          var keys = Object.keys(baseFields);
          for (var i = 0; i < keys.length && i < 6; i++) {
            out.push(keys[i] + '=' + baseFields[keys[i]]);
          }
          return out.join('\n');
        } catch (e) { return ''; }
      })(),
      // 浏览器加载过的图片类资源（不限 ftimage 域名）。
      // knownCount=0 时看这里就能判断「页面到底有没有渲染过图」。
      imgRes: (function () {
        try {
          var es = performance.getEntriesByType('resource') || [];
          var out = [];
          for (var i = es.length - 1; i >= 0 && out.length < 6; i--) {
            var it = es[i];
            if (it.initiatorType === 'img' ||
                /\.(jpe?g|png|webp|dcm)(\?|#|$)/i.test(it.name)) {
              out.push(String(it.name).slice(0, 160));
            }
          }
          return out;
        } catch (e) { return []; }
      })(),
      assetKeys: (function () {
        try { return Object.keys(assetKeys).slice(0, 24).join(','); } catch (e) { return ''; }
      })(),
      // 两个视角：net 是"解析出 JSON 的"，req 是"页面请求过的全部"
      net: stats.net.slice(-20).map(function (r) {
        var s = r.k + ' ' + r.u + (r.n ? (' ' + r.n + 'B') : '') + (r.x ? (' keys:' + r.x) : '');
        if (r.s) s += '\n    样本: ' + r.s.slice(0, 170);
        return s;
      }),
      req: reqLog.slice(-25).map(function (r) {
        return r.m + ' ' + r.u + (r.r ? ('  → ' + r.r) : '');
      }),
      globals: {
        Sz: typeof window.Sz,
        SM2: typeof window.SM2,
        cryptoLibs: hookedLibs.length
      },
      hooks: {
        jsonParse: true,
        fetch: typeof window.fetch === 'function',
        xhr: !!(window.XMLHttpRequest && XMLHttpRequest.prototype)
      }
    };
  }

  window.addEventListener('message', function (ev) {
    // 只接受本窗口的消息（iframe 上报不该走这里）
    if (ev.source && ev.source !== window) return;
    var d = ev.data;
    if (!d || !d.__zgsave) return;

    if (d.__zgsave === 'collect') {
      var data;
      try { data = snapshot(); } catch (e) { data = { series: [], total: 0 }; }
      // 一份都没抓到就现场回捞一次，别让"装晚了"变成死局
      if (!data.total) {
        try { scanGlobals(); } catch (e) {}
        try { data = snapshot(); } catch (e) { data = { series: [], total: 0 }; }
      }
      data.cookies = (function () {
        // HttpOnly 读不到。多数云影像的 dicom 直链靠 URL 签名，不依赖会话 cookie；
        // 真遇到 403 再考虑给扩展加 cookies 权限（会多一条安装警告）。
        try { return document.cookie || ''; } catch (e) { return ''; }
      })();
      data.diag = diagnose();
      try { window.postMessage({ __zgsave: 'collected', data: data }, '*'); } catch (e) {}
      return;
    }

    if (d.__zgsave === 'diag') {
      var dg;
      try { dg = diagnose(); } catch (e) { dg = { error: String(e) }; }
      try { window.postMessage({ __zgsave: 'diagResult', data: dg }, '*'); } catch (e) {}
      return;
    }

    if (d.__zgsave === 'probe') {
      try { scanGlobals(); } catch (e) {}
      var snap;
      try { snap = snapshot(); } catch (e) { snap = { series: [], total: 0 }; }
      try { window.postMessage({ __zgsave: 'probeResult', data: snap }, '*'); } catch (e) {}
      return;
    }
  }, false);

  // 数据一到位就主动通报，让按钮早点亮起来（不必等用户点）
  var notified = 0;
  var ticks = 0;
  function notifyReady() {
    var total = totalImages() + wadoTotal();
    if (total > 0 && total !== notified) {
      notified = total;
      try { window.postMessage({ __zgsave: 'ready', total: total }, '*'); } catch (e) {}
    }
  }

  var tick = setInterval(function () {
    ticks += 1;
    // 内网 Wado：viewer 实例比 JSON 响应还晚一点，而且序列可能是懒加载的、
    // 张数会继续涨，所以这里每轮都读一次（读不到就是空转，成本可忽略）。
    try { wadoIngestViewer(null); } catch (e) { /* ignore */ }
    notifyReady();
    if (notified > 0 && ticks > 3) clearInterval(tick);
    if (ticks > 40) clearInterval(tick);          // 48 秒还什么都没有就不再轮询
  }, 1200);

  // 页面加载后的几次回捞（应对"钩子装晚了"）
  [1800, 5000, 12000, 25000].forEach(function (ms) {
    setTimeout(function () {
      try { scanGlobals(); } catch (e) {}
      notifyReady();
    }, ms);
  });
})();
