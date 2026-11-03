/**
 * boot.js —— 运行在隔离世界，document_start。
 *
 * 唯一职责：把 inject.js 送进页面主世界。
 *
 * 为什么需要这一步（manifest 里已经有 world:"MAIN" 的 inject.js 了）：
 *   content_scripts 的 world 字段是 Chrome 111 才有的。低于这个版本的浏览器
 *   （医院常见的受管 Chrome、以及 360/QQ 等 Chromium 内核浏览器）会**静默忽略**
 *   这个字段，把 inject.js 丢进隔离世界执行。结果就是：钩子挂在隔离世界的
 *   JSON.parse 上，而页面在主世界解析响应 —— 一个字节也抓不到，且没有任何报错。
 *
 *   <script src="chrome-extension://..."> 注入是所有版本都支持的老办法，两者互补：
 *   - 新 Chrome：manifest 的 MAIN 条目先生效，这里的注入被去重标志挡掉
 *   - 老 Chrome：manifest 条目退化到隔离世界（无效），这里的注入才是真正生效的那份
 *   - 页面 CSP 严格时：script 标签被拦，但新 Chrome 的 MAIN 条目仍能work
 */
(function () {
  'use strict';
  if (window.__zhiguBootDone) return;
  window.__zhiguBootDone = true;

  var url;
  try {
    url = chrome.runtime.getURL('inject.js');
  } catch (e) {
    return;                      // 扩展上下文失效（热重载后常见）
  }

  function inject() {
    try {
      var s = document.createElement('script');
      s.src = url;
      s.async = false;           // 保持顺序：必须早于页面自己的 bundle
      s.onload = function () {
        try { s.parentNode && s.parentNode.removeChild(s); } catch (e) { /* ignore */ }
      };
      s.onerror = function () {
        try { s.parentNode && s.parentNode.removeChild(s); } catch (e) { /* ignore */ }
        // 页面 CSP 拦掉了。此时只能指望 manifest 的 world:"MAIN" 条目，
        // 新 Chrome 上那条路是好的；老 Chrome + 严格 CSP 确实无解，
        // 这种情况要走 Native Messaging 或单独分发。
      };
      (document.head || document.documentElement).appendChild(s);
    } catch (e) { /* ignore */ }
  }

  // document_start 时 head 可能还没建出来，但 documentElement 一定有
  if (document.documentElement) {
    inject();
  } else {
    // 极端情况：连 documentElement 都没有，等下一个微任务
    setTimeout(inject, 0);
  }
})();
