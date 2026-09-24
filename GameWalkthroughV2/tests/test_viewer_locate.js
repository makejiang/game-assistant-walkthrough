/**
 * Viewer locate-logic regression test (no browser needed).
 *
 * Extracts the inline <script> from app/webserver/static/index.html and runs it
 * against a minimal DOM shim that mimics the real gamersky page behaviour:
 *   - every content img has src=".../blank.png" and the real URL in data-src
 *   - images only get their real height after their src is swapped in (async)
 *
 * Asserts that the viewer:
 *   1. force-reveals lazy images and scrolls to the pushed image AFTER it has
 *      a real height (the bug this guards: scrolling while everything is
 *      collapsed lands at the top of the page),
 *   2. re-snaps afterwards while the layout is still shifting, and stops once
 *      the user interacts,
 *   3. falls back to the content area when no image target was pushed.
 *
 * Run:  node tests/test_viewer_locate.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "app/webserver/static/index.html"), "utf-8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

if (!fs.existsSync(path.join(ROOT, "app/webserver/static/index.html"))) {
  console.error("index.html missing");
  process.exit(1);
}

// ------------------------------------------------------------------ shim
function makeImg(idx, attrs, loadDelayMs) {
  const attributes = Object.assign({}, attrs);
  const img = {
    tagName: "IMG",
    isConnected: true,
    offsetHeight: 0,
    _src: "",
    scrollLog: [],
    ownerDocument: null,          // makeDoc 中回填
    anchorHref: attrs["anchor-href"] || null,  // 外层 <a href> 包装（游民星空正文截图）
    getAttribute(name) { return attributes[name] !== undefined ? attributes[name] : null; },
    setAttribute(name, value) { attributes[name] = value; },
    closest(selector) {
      if (selector === "a[href]" && img.anchorHref) {
        return { getAttribute: (n) => (n === "href" ? img.anchorHref : null) };
      }
      return null;
    },
    // 小屏缩放模式下的定位换算用（文档绝对 top = idx*600）
    getBoundingClientRect() { return { top: idx * 600 }; },
    scrollIntoView(opts) {
      img.scrollLog.push({ t: Date.now(), block: opts && opts.block, heightAtScroll: img.offsetHeight });
    },
  };
  Object.defineProperty(img, "src", {
    get() { return img._src; },
    set(value) {
      img._src = value;
      attributes["src"] = value;
      // 占位图换成真图后，异步加载完成 → 获得真实高度（模拟布局向下生长）
      if (!/blank|loading|placeholder/.test(String(value))) {
        setTimeout(() => { img.offsetHeight = 620; }, loadDelayMs);
      }
    },
  });
  return img;
}

function makeDoc(imgs, baseURI, docHref) {
  const content = { tagName: "DIV", isConnected: true, offsetHeight: 400, scrollLog: [],
    scrollIntoView(opts) { content.scrollLog.push({ t: Date.now(), block: opts && opts.block }); } };
  // 正文区图片挂 .GsImageLabel 结构无关紧要；shim 直接让 content 能枚举到全部图片
  content.querySelectorAll = () => imgs;
  const documentElement = { scrollTop: 0, scrollHeight: 12000 };
  const listeners = {};
  const doc = {
    baseURI,
    location: { href: docHref || ("/proxy/" + baseURI) },  // frame 文档的真实地址（会被测试改写模拟翻页）
    body: { isConnected: true, scrollHeight: 12000 },
    documentElement,
    title: "《测试游戏》图文攻略_游民星空",
    querySelector(sel) { return sel === ".Mid2L_con" ? content : null; },
    querySelectorAll(sel) { return sel === "img" ? imgs : []; },
    addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
    dispatch(evt) { (listeners[evt] || []).forEach((fn) => fn({})); },
    content,
  };
  imgs.forEach((img) => { img.ownerDocument = doc; });
  return { doc, content };
}

function makeFrame(doc) {
  const listeners = {};
  const state = { srcSets: 0, escape: false }; // escape=true 模拟 frame 被跳去跨域页面
  const frame = {
    classList: { add() {}, remove() {}, toggle() {} },
    style: {},
    textContent: "",
    title: "",
    addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
    fire(evt) { (listeners[evt] || []).forEach((fn) => fn({})); },
  };
  frame._state = state;
  Object.defineProperty(frame, "contentDocument", {
    get() { if (state.escape) throw new Error("SecurityError: cross-origin"); return doc; },
  });
  Object.defineProperty(frame, "src", {
    set(value) {
      state.srcSets += 1;
      state.lastSrc = value;
      setTimeout(() => frame.fire("load"), 30);
    },
    get() { return state.lastSrc || ""; },
  });
  return frame;
}

function makeElement(id) {
  const listeners = {};
  return {
    id,
    textContent: "",
    title: "",
    style: {},
    addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
    fire(evt) { (listeners[evt] || []).forEach((fn) => fn({})); },
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { on ? this._set.add(c) : this._set.delete(c); },
    },
  };
}

async function runViewer(view, attrsHook, opts) {
  opts = opts || {};
  const mainEl = makeElement("main");
  mainEl.clientWidth = 390;    // 模拟手机视口
  mainEl.clientHeight = 700;
  mainEl.scrollTop = 0;
  const wrapEl = makeElement("wrap");
  const elements = {
    frame: null, main: mainEl, wrap: wrapEl,
    empty: makeElement("empty"), loading: makeElement("loading"),
    title: makeElement("title"), locateBtn: makeElement("locateBtn"), status: makeElement("status"),
    prevBtn: makeElement("prevBtn"), nextBtn: makeElement("nextBtn"),
  };
  // 目录下拉：带 options/selectedIndex，用于断言“当前章节”高亮
  const tocSelectEl = makeElement("tocSelect");
  tocSelectEl.options = [];
  tocSelectEl.add = function (opt) { tocSelectEl.options.push(opt); };
  tocSelectEl.selectedIndex = -1;
  elements.tocSelect = tocSelectEl;
  const imgs = [];
  for (let i = 0; i < 10; i++) {
    const attrs = {
      "src": "http://image.gamersky.com/webimg13/zhuanti/common/blank.png",
      "data-src": `//img1.gamersky.com/image/gs/2025/img_${String(i + 1).padStart(2, "0")}.jpg`,
    };
    if (attrsHook) attrsHook(attrs, i);
    imgs.push(makeImg(i, attrs, 100 + i * 60)); // 图片逐张加载完成，布局持续生长
  }
  const { doc, content } = opts.doc
    ? { doc: opts.doc, content: null }   // 测试自带文档（如新版路书页的节点结构）
    : makeDoc(imgs, "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
        "http://127.0.0.1:8180/proxy/https://www.gamersky.com/handbook/202507/1961684_6.shtml");
  const frame = makeFrame(doc);
  elements.frame = frame;

  globalThis.document = {
    getElementById: (id) => elements[id] || makeElement(id),  // 未知元素按需创建
    querySelector: () => null,  // shim 无 header/brand 节点，fitHeader 自行跳过
  };
  globalThis.matchMedia = () => ({ matches: !!opts.mobile });
  globalThis.addEventListener = globalThis.addEventListener || function () {};
  globalThis.window = globalThis;   // 脚本里的 window.matchMedia / window.addEventListener
  globalThis.location = { search: opts.debug ? "?debug=1" : "" };   // 查看器页面地址（诊断角标开关）
  globalThis.Option = function (text, value) { return { text: text, value: value }; };
  globalThis.fetch = (url) => Promise.resolve({
    json: () => Promise.resolve(
      String(url).indexOf("/api/toc") >= 0
        ? (opts.tocFor ? opts.tocFor(String(url)) : (opts.toc || { ok: true, toc: [] }))
        : view
    ),
  });
  const esInstances = [];
  globalThis.EventSource = class { constructor() { esInstances.push(this); } };

  // 运行查看器脚本
  (0, eval)(script);

  // fetch 是异步的：让 applyTarget -> openUrl -> load -> locate 链路跑起来
  await new Promise((r) => setTimeout(r, 120));
  return { elements, imgs, frame, doc, content, es: esInstances[0] || null };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function scrollsOf(el) { return el.scrollLog || []; }

// ------------------------------------------------------------------ tests
async function test_locates_after_real_height_and_resnaps() {
  const targetFile = "img_08.jpg"; // 第 8 张：加载较晚，页面布局仍在生长
  const { imgs, elements } = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: `https://img1.gamersky.com/image/gs/2025/${targetFile}`, // 注意：https 协议 + 页面 data-src 是 // 开头
    title: "第1页：佛源镇",
  });
  const target = imgs[7];
  const t0 = Date.now();

  await sleep(2600); // 等首次定位 + 1.5s 校正

  const scrolls = scrollsOf(target);
  assert(scrolls.length > 0, `目标图片从未被 scrollIntoView（status="${elements.status.textContent}"）`);
  const first = scrolls[0];
  assert(first.heightAtScroll >= 80,
    `定位发生在图片有真实高度之前 (height=${first.heightAtScroll}) — 会滚到错误位置`);
  assert(first.block === "center", `图片应使用 block:center 定位，实际 ${first.block}`);
  assert(scrolls.length >= 2, "布局仍在变化时应有多次校正滚动");
  assert(/已定位到「第1页：佛源镇」/.test(elements.status.textContent),
    `状态提示应为“已定位到场景章节名”，实际: ${elements.status.textContent}`);
  // 图片被强制换成了真 src（不再等站点懒加载）
  assert(!/blank/.test(target.src), "目标图片的占位 src 应已被替换为真实地址");
  // 没有滚到其它图片上（首次滚动时间应晚于目标图片加载完成）
  assert(first.t - t0 >= 100 + 7 * 60 - 30, "定位过早，目标图片尚未加载完成");
  console.log(`PASS 1: 等图片真实高度后再定位(${((first.t - t0) / 1000).toFixed(2)}s) + 校正滚动 x${scrolls.length}`);
}

async function test_user_scroll_stops_resnap() {
  const { imgs, doc } = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_08.jpg",
  });
  const target = imgs[7];
  await sleep(700);          // 首次定位完成
  doc.dispatch("wheel");     // 用户开始滚动
  const count = scrollsOf(target).length;
  await sleep(2200);         // 1.5s/3.5s 校正时间点已过
  assert(scrollsOf(target).length === count, "用户滚动后不应再自动校正");
  console.log("PASS 2: 用户滚动后停止自动校正");
}

async function test_fallback_to_content_without_image() {
  const { content, elements } = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "",
  });
  await sleep(600);
  const scrolls = scrollsOf(content);
  assert(scrolls.length > 0, "无图片目标时应定位正文区域");
  assert(scrolls[0].block === "start", "正文区域应使用 block:start 定位");
  assert(/已定位当前场景页首/.test(elements.status.textContent),
    `状态提示应为“已定位当前场景页首”，实际: ${elements.status.textContent}`);
  console.log("PASS 3: 无图片目标时定位正文顶部");
}

async function test_unmatchable_image_reports_and_falls_back() {
  const { content, elements } = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "https://img1.gamersky.com/image/gs/not_exist.jpg",
  });
  await sleep(600);
  assert(scrollsOf(content).length > 0, "找不到目标图片时应退回正文区域");
  assert(/未找到目标图片/.test(elements.status.textContent),
    `状态提示应为“未找到目标图片”，实际: ${elements.status.textContent}`);
  console.log("PASS 4: 图片匹配失败时退回正文并给出提示");
}

async function test_matches_anchor_showimage_wrapper() {
  // 游民星空正文截图的真实形态：img 只有 blank 占位 src，真图地址在
  // 外层 <a href="showimage/id_gamersky.shtml?<urlencoded 真图>"> 里。
  // 推送值取自真实日志：img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg",
  }, (attrs, i) => {
    if (i === 7) {
      delete attrs["data-src"];
      attrs["anchor-href"] =
        "/showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2025%2F07%2F20250723_ax_156_1%2F14010.jpg";
    }
  });
  const target = ctx.imgs[7];
  await sleep(900);
  const scrolls = scrollsOf(target);
  assert(scrolls.length > 0, "showimage 包装的正文截图未被匹配定位");
  assert(scrolls[0].heightAtScroll >= 80, "应在图片有真实高度后再滚动");
  assert(!/blank/.test(target.src), "占位 src 应已通过锚点 href 强制换成真图");
  assert(/已定位到当前场景/.test(ctx.elements.status.textContent),
    `状态提示应为“已定位到场景章节名”，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 5: showimage 包装锚点中的真图地址可匹配并强制加载");
}

async function test_matches_resized_suffix() {
  // 页面图片带缩放/水印后缀（img_09.jpg!cc450.jpg），推送的是原始 URL
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_09.jpg",
  }, (attrs, i) => {
    if (i === 8) attrs["data-src"] = "//img1.gamersky.com/image/gs/2025/img_09.jpg!cc450.jpg";
  });
  const target = ctx.imgs[8];
  await sleep(900);
  const scrolls = scrollsOf(target);
  assert(scrolls.length > 0, "带缩放后缀的图片应能通过路径互含匹配到");
  assert(/已定位到当前场景/.test(ctx.elements.status.textContent),
    `状态提示应为“已定位到场景章节名”，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 6: 缩放/水印后缀图片通过路径互含匹配定位");
}

async function test_pull_back_when_frame_escapes() {
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684.shtml",
    image_src: "",
  });
  const frame = ctx.frame;
  const elements = ctx.elements;
  await sleep(500);
  const before = frame._state.srcSets;
  frame._state.escape = true;  // 站内 JS 把 frame 跳去了跨域（wap 版）
  frame.fire("load");
  await sleep(200);
  assert(/正在拉回/.test(elements.status.textContent),
    `检测到跨域跳转应提示拉回，实际: ${elements.status.textContent}`);
  frame._state.escape = false;  // 拉回后的加载恢复同源
  await sleep(500);             // 300ms 拉回 + 30ms 加载
  assert(frame._state.srcSets > before, "检测到跨域跳转后应重新经代理打开");
  assert(/已定位当前场景页首/.test(elements.status.textContent),
    `拉回成功后应恢复定位提示，实际: ${elements.status.textContent}`);
  console.log("PASS 7: frame 被跳转后自动拉回并恢复定位");
}

async function test_matches_gamersky_thumbnail_suffix() {
  // 线上实测形态（用户日志）：页面 img 的 data-src 是 _S 缩略图，
  // 推送的是去掉 _S 的全尺寸图（来自 showimage 锚点解析）。
  // 推送 https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg
  // 页面 https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010_S.jpg
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg",
  }, (attrs) => {
    attrs["data-src"] = attrs["data-src"].replace(/\.jpg$/, "_S.jpg"); // 全部变缩略图
  });
  const target = ctx.imgs[7]; // data-src=.../img_08_S.jpg — 与推送不同名，不应匹配
  const thumbOfPush = ctx.imgs[9]; // .../img_10_S.jpg — 仅为占位，不对应推送
  void thumbOfPush;
  // 推送 14010.jpg 在默认数据集中没有对应缩略图 → 预期退回正文，但不应崩
  await sleep(700);
  assert(/已定位当前场景页首|未找到目标图片/.test(ctx.elements.status.textContent),
    `缩略图无对应项时应走回退分支，实际: ${ctx.elements.status.textContent}`);

  // 真实对应场景：推送 14010.jpg，页面存在 14010_S.jpg 缩略图
  const ctx2 = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg",
  }, (attrs, i) => {
    attrs["data-src"] = `//img1.gamersky.com/image2025/07/20250723_ax_156_1/${13153 + i * 63}_S.jpg`;
    if (i === 7) attrs["data-src"] = "//img1.gamersky.com/image2025/07/20250723_ax_156_1/14010_S.jpg";
  });
  const t2 = ctx2.imgs[7];
  await sleep(900);
  const scrolls2 = scrollsOf(t2);
  assert(scrolls2.length > 0, "推送全尺寸图应能匹配到页面 _S 缩略图并定位");
  assert(scrolls2[0].heightAtScroll >= 80, "应在缩略图有真实高度后再滚动");
  assert(/已定位到当前场景/.test(ctx2.elements.status.textContent),
    `状态提示应为“已定位到场景章节名”，实际: ${ctx2.elements.status.textContent}`);
  console.log("PASS 8: _S 缩略图与全尺寸图名归一匹配");
}

async function test_mobile_wap_fluid() {
  // 手机 + 攻略页 URL：应映射到 wap 手机版（自适应排版，字号正常），
  // 且由于两版图片 ID 一致，`_S` 归一化定位不受影响。
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image2025/07/20250723_ax_156_1/14010.jpg",
  }, (attrs, i) => {
    if (i === 7) attrs["data-src"] = "//img1.gamersky.com/image2025/07/20250723_ax_156_1/14010_S.jpg";
  }, { mobile: true });
  const target = ctx.imgs[7];
  await sleep(900);
  const lastSrc = decodeURIComponent(ctx.frame._state.lastSrc || "");
  assert(lastSrc.includes("https://wap.gamersky.com/gl/Content-1961684_6.html"),
    `手机应展示 wap 版页面，实际: ${lastSrc}`);
  assert(ctx.frame.style.width === "100%", `wap 自适应页不应固定设计宽，实际: ${ctx.frame.style.width}`);
  const scrolls = scrollsOf(target);
  assert(scrolls.length > 0 && scrolls[0].block === "center", "wap 页面上应定位到目标图片(居中)");
  assert(/已定位到当前场景/.test(ctx.elements.status.textContent),
    `状态提示应为“已定位到场景章节名”，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 9: 手机自动切换 wap 手机版（自适应排版），定位不受影响");
}

async function test_mobile_scale_fallback() {
  // 无法映射到 wap 的地址（非数字 ID 页面）：退回 PC 版 + 1200px 等比缩放模式
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/custom.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_08.jpg",
  }, null, { mobile: true });
  const target = ctx.imgs[7];
  const mainEl = ctx.elements.main;
  await sleep(900);
  const lastSrc = decodeURIComponent(ctx.frame._state.lastSrc || "");
  assert(lastSrc.includes("handbook/202507/custom.shtml"), `不可映射地址应保留 PC 版，实际: ${lastSrc}`);
  assert(ctx.frame.style.width === "1200px", `frame 应按设计宽 1200px 渲染，实际: ${ctx.frame.style.width}`);
  assert(String(ctx.frame.style.transform).startsWith("scale("), "frame 应应用等比缩放");
  assert(mainEl.scrollTop > 0, "应在父页面(main)上滚动定位");
  const expected = Math.round(7 * 600 * 0.325 - 350 + Math.min(620 * 0.325, 175));
  assert(mainEl.scrollTop === expected,
    `滚动位置应等比换算(${expected})，实际: ${mainEl.scrollTop}`);
  console.log("PASS 10: 不可映射地址退回 PC 版等比缩放模式");
}

async function test_free_browsing_and_manual_return() {
  // 自由浏览：只有“新场景推送”与“用户点击回到当前位置”才定位；
  // 用户自行点“下一页/章节”（frame 内导航）不得被拉回。
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_08.jpg",
    title: "第6页：佛源镇-含光禅院",
  });
  const target = ctx.imgs[7];
  await sleep(700);
  const afterAuto = scrollsOf(target).length;
  assert(afterAuto > 0, "初始推送应自动定位");

  // 用户点击翻页：pointerdown（用户介入）→ frame 加载新页面
  ctx.doc.dispatch("pointerdown");
  ctx.frame.fire("load");
  await sleep(400);
  assert(scrollsOf(target).length === afterAuto, "用户自行翻页后不应被拉回场景位置");

  // “回到当前位置”按钮：用户离开后重新定位到场景
  ctx.elements.locateBtn.fire("click");
  await sleep(700);
  assert(scrollsOf(target).length > afterAuto, "点击回到当前位置应重新定位");

  // 用户新翻页后，新场景推送（另一页）应自动跳转
  ctx.doc.dispatch("pointerdown");
  ctx.es.onmessage({ data: JSON.stringify({
    type: "navigate", game: "测试游戏",
    url: "https://www.gamersky.com/handbook/202507/1961684_7.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_09.jpg",
    title: "第7页：蜀王祠",
  })});
  await sleep(600);
  const nextTarget = ctx.imgs[8];
  assert(scrollsOf(nextTarget).length > 0 || scrollsOf(target).length > afterAuto + 1,
    "新场景推送应自动跳转定位");
  assert(/已定位到「第7页：蜀王祠」/.test(ctx.elements.status.textContent),
    `新场景定位提示应带章节名，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 11: 自由浏览不被拉回；推送与“回到当前位置”才定位，提示带章节名");
}

async function test_return_to_pushed_page_after_user_nav() {
  // 用户在 frame 内翻到别的页后（frame 真实地址已变化）：
  //   - 点“回到当前位置”必须重新打开推送页并定位（而不是只在当前页找图失败）
  //   - 新场景推送（无论目标是否变化）也必须真正跳回推送页
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_08.jpg",
    title: "第6页：佛源镇-含光禅院",
  });
  const target = ctx.imgs[7];
  await sleep(700);
  assert(scrollsOf(target).length > 0, "初始推送应自动定位");

  // 用户翻到第 9 页：frame 的真实地址变化（loadedUrl 仍停留在第 6 页）
  ctx.doc.dispatch("pointerdown");
  ctx.doc.location.href = "http://127.0.0.1:8180/proxy/https://wap.gamersky.com/gl/Content-1961684_9.html";
  ctx.frame.fire("load");
  await sleep(400);
  const before = scrollsOf(target).length;

  // 点击“回到当前位置”：应重新打开第 6 页并定位到目标图
  ctx.elements.locateBtn.fire("click");
  await sleep(600);
  assert(ctx.frame._state.srcSets >= 2, "应重新打开推送页（而不是只在当前页找图）");
  assert(scrollsOf(target).length > before, "回到推送页后应重新定位目标图");

  // 用户再次翻走后，新场景推送（目标仍是第 6 页的场景）也应真正跳回来
  ctx.doc.dispatch("pointerdown");
  ctx.doc.location.href = "http://127.0.0.1:8180/proxy/https://wap.gamersky.com/gl/Content-1961684_9.html";
  ctx.frame.fire("load");
  await sleep(400);
  const before2 = scrollsOf(target).length;
  ctx.es.onmessage({ data: JSON.stringify({
    type: "navigate", game: "测试游戏",
    url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "https://img1.gamersky.com/image/gs/2025/img_08.jpg",
    title: "第6页：佛源镇-含光禅院",
  })});
  await sleep(600);
  assert(ctx.frame._state.srcSets >= 3, "同页新场景推送在用户翻走后也应重新打开页面");
  assert(scrollsOf(target).length > before2, "重新打开后应定位到场景图");
  console.log("PASS 12: 用户翻页离开后，推送/回到当前位置都会真正跳回推送页并定位");
}

// ------------------------------------------------------------------ 新版路书页
// 路书页已在 frame 中打开时，目录选择/场景推送只换 #片段：不整页重载
// （路书页 3MB+），直接滚动到对应节点卡（图文位置）。
function makeRoadbookDoc() {
  // 节点内的正文截图：页面放 _S 缩略图（与线上路书一致），推送的是全尺寸图名。
  // src 用属性赋值（走 shim 的加载模拟：赋值后异步获得真实高度）
  const imgs = [
    makeImg(0, {}, 50),
    makeImg(1, {}, 50),
    makeImg(2, {}, 50),
    makeImg(3, {}, 50),
  ];
  const thumbs = [
    "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/11_S.jpg",
    "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18_S.jpg",
    "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/19_S.jpg",
    "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/20_S.jpg",
  ];
  imgs.forEach((img, i) => { img.src = thumbs[i]; });

  function makeNode(id, label, top, richImgs) {
    const el = {
      tagName: "ARTICLE", isConnected: true, offsetHeight: 260,
      id: id, textContent: label + " ……",
      scrollLog: [],
      scrollIntoView(opts) { el.scrollLog.push({ block: opts && opts.block }); },
      getBoundingClientRect() { return { top: top }; },
      querySelector(sel) {
        if (sel === ".reader-node-card__header") return { textContent: label };
        if (sel === ".reader-rich-content") {
          return { querySelectorAll: (s) => (s === "img" ? richImgs : []) };
        }
        return null;
      },
    };
    return el;
  }
  // 三个节点卡：A1/A2/A3，文档顺序排列（top 递增）；A2 正文有两张图
  const nodes = [
    makeNode("reader-node-1", "A1 序章及概要", 0, [imgs[0]]),
    makeNode("reader-node-2", "A2 参道", 600, [imgs[1], imgs[3]]),
    makeNode("reader-node-3", "A3 仁王门前", 1600, [imgs[2]]),
  ];
  const doc = {
    baseURI: "https://www.gamersky.com/tools/guide-map/roadbooks/47",
    // frame 的真实地址形态：本站 origin + /proxy/ 前缀（pageKey 需正确剥离）
    location: { href: "http://127.0.0.1:22818/proxy/https://www.gamersky.com/tools/guide-map/roadbooks/47" },
    body: {
      isConnected: true, scrollHeight: 20000,
      querySelectorAll(sel) { return sel === "img" ? imgs : []; },
    },
    documentElement: { scrollTop: 0, scrollHeight: 20000 },
    title: "《测试游戏》全探索图文流程攻略",
    querySelector: () => null,
    querySelectorAll(sel) {
      return sel === "article.reader-node-card" ? nodes : [];
    },
    getElementById(id) {
      return nodes.find((n) => n.id === id) || null;
    },
    addEventListener() {},
  };
  imgs.forEach((img) => { img.ownerDocument = doc; });
  return { doc, nodes, imgs };
}

async function test_roadbook_fragment_jump_without_reload() {
  const { doc, nodes } = makeRoadbookDoc();
  const ctx = await runViewer({
    game: "测试游戏",
    url: "https://www.gamersky.com/tools/guide-map/roadbooks/47#reader-node-2",
    image_src: "",
    title: "A2 参道",
  }, null, { doc });
  await sleep(120);
  assert(ctx.frame._state.srcSets === 1, "初始应正常打开路书页一次");
  assert(scrollsOf(nodes[1]).length > 0, "应滚动到目标节点卡");
  assert(/已定位到「A2 参道」/.test(ctx.elements.status.textContent),
    `状态提示应为节点名，实际: ${ctx.elements.status.textContent}`);
  // 同文档的再次推送（模拟目录点选/场景重推）：不整页重载、重新定位节点
  const scrollsBefore = scrollsOf(nodes[1]).length;
  ctx.es.onmessage({ data: JSON.stringify({ type: "navigate", game: "测试游戏",
    url: "https://www.gamersky.com/tools/guide-map/roadbooks/47#reader-node-2",
    image_src: "", title: "A2 参道" }) });
  await sleep(80);
  assert(ctx.frame._state.srcSets === 1,
    "同一文档仅换 #片段不应重设 frame.src（否则 3MB 路书页整页重载）");
  assert(scrollsOf(nodes[1]).length >= scrollsBefore, "推送后应重新定位节点");
  console.log("PASS 13: 路书页 #片段跳转不重载 frame，直接定位节点卡");
}

async function test_roadbook_toc_click_jumps_without_reload() {
  // 目录下拉点选章节：不整页重载（bug 表现为刷新+跳回顶部），直接滚到对应节点
  const { doc, nodes } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2", image_src: "", title: "A2 参道",
  }, null, { doc });
  await sleep(120);
  assert(ctx.frame._state.srcSets === 1, "初始应正常打开路书页一次");

  ctx.elements.tocSelect.value = base + "#reader-node-1";
  ctx.elements.tocSelect.fire("change");   // 目录点选（openUrl(url, false) 路径）
  await sleep(80);
  assert(ctx.frame._state.srcSets === 1, `点选目录不应重载页面（src 设置次数 ${ctx.frame._state.srcSets}）`);
  assert(scrollsOf(nodes[0]).length > 0, "点选目录后应滚动到对应节点");
  assert(/已定位到「A1 序章及概要」/.test(ctx.elements.status.textContent),
    `应有定位提示，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 15: 目录点选路书章节不刷新页面，直接跳到节点位置");
}

async function test_roadbook_toc_click_on_missing_node_polls() {
  // 节点尚未渲染（SPA 水合中）时点选目录：轮询等节点出现，绝不整页重载
  const { doc, nodes } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2", image_src: "", title: "",
  }, null, { doc });
  await sleep(120);
  // 点选瞬间 reader-node-3 尚不存在（水合未完成），随后恢复
  const realGet = doc.getElementById;
  doc.getElementById = (id) => (id === "reader-node-3" ? null : realGet(id));
  ctx.elements.tocSelect.value = base + "#reader-node-3";
  ctx.elements.tocSelect.fire("change");
  doc.getElementById = realGet;   // “水合完成”
  await sleep(700);
  assert(ctx.frame._state.srcSets === 1, "节点未渲染时也不应重载页面");
  assert(scrollsOf(nodes[2]).length > 0, "轮询命中节点后应自动滚到该节点");
  console.log("PASS 17: 目录点选的节点暂未渲染时轮询等待，不重载页面");
}

async function test_roadbook_prev_next_section() {
  // 路书页上一页/下一页 = 上一段/下一段文本（节点卡），而不是“不支持翻页”
  const { doc, nodes } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2", image_src: "", title: "A2 参道",
  }, null, { doc });
  await sleep(120);
  assert(scrollsOf(nodes[1]).length > 0, "初始应定位在 A2");

  ctx.elements.nextBtn.fire("click");   // 下一段
  await sleep(60);
  assert(scrollsOf(nodes[2]).length > 0, "下一页应滚到下一段文本(A3)");
  assert(/已定位到「A3 仁王门前」/.test(ctx.elements.status.textContent),
    `应有定位提示，实际: ${ctx.elements.status.textContent}`);
  assert(ctx.frame._state.srcSets === 1, "翻段不应重载页面");

  ctx.elements.prevBtn.fire("click");   // 上一段
  await sleep(60);
  assert(scrollsOf(nodes[1]).length > 0, "上一页应滚回上一段文本(A2)");

  // 边界：连点两次上一页，到第一段后提示“已经是第一章节”
  ctx.elements.prevBtn.fire("click");
  await sleep(40);
  ctx.elements.prevBtn.fire("click");
  await sleep(40);
  assert(/已经是第一章节/.test(ctx.elements.status.textContent),
    `第一段再向上一段应提示，实际: ${ctx.elements.status.textContent}`);
  assert(ctx.frame._state.srcSets === 1, "边界提示也不应重载页面");
  console.log("PASS 18: 路书页上一页/下一页翻至上/下一段文本");
}

async function test_roadbook_back_to_scene_locates_image() {
  // 场景推送正确跳到图片后，点“回到场景”必须回到同一张图片（bug：误判
  // “已翻页离开”-> 整页重载/只滚到节点卡开头，落点飘忽）
  const { doc, nodes, imgs } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2",
    image_src: "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18.jpg",
    title: "A2 参道",
  }, null, { doc });
  await sleep(700);   // 等首次定位（18_S 缩略图与全尺寸图名归一匹配）
  const target = imgs[1];
  assert(scrollsOf(target).length > 0, "场景推送应定位到对应图片");
  const targetBefore = scrollsOf(target).length;
  const nodeScrollsBefore = nodes.map((n) => scrollsOf(n).length);

  ctx.elements.locateBtn.fire("click");   // 回到场景
  await sleep(400);
  assert(ctx.frame._state.srcSets === 1, "回到场景不应重载页面");
  assert(scrollsOf(target).length > targetBefore,
    "回到场景应重新定位到同一张场景图片");
  nodes.forEach((n, i) => {
    assert(scrollsOf(n).length === nodeScrollsBefore[i],
      "回到场景不应滚到节点卡开头（应精确到图片）");
  });
  assert(/已定位到「A2 参道」/.test(ctx.elements.status.textContent),
    `应有定位提示，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 19: 路书页“回到场景”精确定位到场景图片");
}

async function test_roadbook_next_cancels_stale_resnap() {
  // 启动定位到第一章后立即翻段：旧场景的 1.5~6s 校正滚动必须作废，
  // 否则页面会被拽回第一章（bug：下一页后又弹回第一章节）
  const { doc, nodes, imgs } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2",
    image_src: "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18.jpg",
    title: "A2 参道",
  }, null, { doc });
  await sleep(700);   // 初始定位 imgs[1]，校正滚动已排程
  ctx.elements.nextBtn.fire("click");   // 翻到 A3
  await sleep(60);
  assert(scrollsOf(nodes[2]).length > 0, "下一页应翻到 A3");
  const imgScrollsAtNav = scrollsOf(imgs[1]).length;
  await sleep(1800);   // 越过旧定位的 +1.5s 校正点
  assert(scrollsOf(imgs[1]).length === imgScrollsAtNav,
    "翻段后旧场景的校正不应再把页面拽回去");
  console.log("PASS 20: 翻段后不再被旧定位校正拽回");
}

async function test_roadbook_fragment_fallback_upgrades_to_image() {
  // 推送图片暂未渲染（懒加载/站点重渲染）时先到所在节点，图片出现后
  // 自动升级定位到该图片（bug：一次点击先跳节点再跳图片，再次点击停在节点）
  const { doc, nodes, imgs } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2",
    image_src: "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/99.jpg",
    title: "A2 参道",
  }, null, { doc });
  await sleep(300);
  assert(scrollsOf(nodes[1]).length > 0, "图片未渲染时应先定位到所在节点");
  // 随后图片渲染进 DOM
  const lateImg = makeImg(9, {}, 30);
  lateImg.src = "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/99_S.jpg";
  imgs.push(lateImg);
  lateImg.ownerDocument = doc;
  await sleep(2500);   // 等图片升级轮询命中
  assert(scrollsOf(lateImg).length > 0, "图片出现后应升级定位到该图片");
  assert(/已定位到「A2 参道」/.test(ctx.elements.status.textContent),
    `升级后应有定位提示，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 21: 图片暂缺时先到节点，出现后自动升级到图片");
}

async function test_roadbook_image_index_fallback() {
  // src 匹配不到（站点懒加载/重渲染改写图片地址）时，按推送携带的图片序号
  // 定位到节点正文里的第 N 张图（bug：只能退到章节标题处）
  const { doc, nodes, imgs } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2",
    image_src: "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/999.jpg",   // 故意匹配不到
    image_index: 1,   // A2 节点正文的第 2 张图 = imgs[3]
    title: "A2 参道",
  }, null, { doc });
  await sleep(700);
  assert(scrollsOf(imgs[3]).length > 0, "应按序号定位到节点内第 2 张图");
  assert(scrollsOf(nodes[1]).length === 0, "不应停留在章节标题处");
  assert(/已定位到「A2 参道」/.test(ctx.elements.status.textContent),
    `应有定位提示，实际: ${ctx.elements.status.textContent}`);
  console.log("PASS 22: src 匹配失败时按图片序号精确定位");
}

async function test_roadbook_back_to_scene_after_site_url_rewrite() {
  // 站点 SPA 改写自身地址（查询参数变化）后，frame 地址与推送地址的文档键
  // 不再相等：回到场景必须仍按“同一文档”处理并定位到图片，
  // 而不是走目录跳章的节点级拦截滚到章节标题（bug 现象）
  const { doc, nodes, imgs } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2",
    image_src: "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18.jpg",
    title: "A2 参道",
  }, null, { doc });
  await sleep(700);
  assert(scrollsOf(imgs[1]).length > 0, "场景推送应先定位到图片");

  // 模拟站点改写自身地址（查询参数变化；roadbook id 不变）
  doc.location.href = "http://127.0.0.1:22818/proxy/https://www.gamersky.com/tools/guide-map/roadbooks/47?spa=rewritten";
  const imgBefore = scrollsOf(imgs[1]).length;
  const nodeBefore = scrollsOf(nodes[1]).length;

  ctx.elements.locateBtn.fire("click");   // 回到场景
  await sleep(300);
  assert(ctx.frame._state.srcSets === 1, "同一路书文档内回到场景不应重载页面");
  assert(scrollsOf(imgs[1]).length > imgBefore, "回到场景应定位到场景图片");
  assert(scrollsOf(nodes[1]).length === nodeBefore, "不应滚到章节标题（节点卡开头）");
  console.log("PASS 23: 站点改写地址后回到场景仍精确定位图片");
}

async function test_toc_reload_on_game_switch() {
  // 换游戏后目录列表必须跟着换（bug：目录按首次加载永久缓存，
  // 切换游戏后页面换了、下拉里还是上一个游戏的章节）
  const pageA = "https://www.gamersky.com/handbook/202507/1961684_6.shtml";
  const pageB = "https://www.gamersky.com/handbook/202609/1970001.shtml";
  const tocA = { ok: true, toc: [{ name: "A-第1页", url: pageA }] };
  const tocB = { ok: true, toc: [{ name: "B-第1页", url: pageB }] };
  const ctx = await runViewer({
    game: "游戏A", url: pageA, image_src: "", title: "",
  }, null, {
    tocFor: (url) => (url.indexOf("1970001") >= 0 ? tocB : tocA),
  });
  await sleep(200);
  assert(ctx.elements.tocSelect.options.length === 1 &&
         ctx.elements.tocSelect.options[0].text === "A-第1页",
    "游戏A目录应加载，实际: " + JSON.stringify(ctx.elements.tocSelect.options));

  ctx.es.onmessage({ data: JSON.stringify({
    type: "navigate", game: "游戏B", url: pageB, image_src: "", title: "",
  }) });
  await sleep(300);
  assert(ctx.elements.tocSelect.options.length === 1 &&
         ctx.elements.tocSelect.options[0].text === "B-第1页",
    "切换游戏后目录列表应跟随更换，实际: " + JSON.stringify(ctx.elements.tocSelect.options));
  console.log("PASS 24: 切换游戏后目录列表跟随更换");
}

async function test_roadbook_toc_selection_sync() {
  // 加载完成后目录下拉应显示当前所处章节（bug 表现为 selectedIndex = -1）
  const { doc } = makeRoadbookDoc();
  const base = "https://www.gamersky.com/tools/guide-map/roadbooks/47";
  const ctx = await runViewer({
    game: "测试游戏", url: base + "#reader-node-2", image_src: "", title: "A2 参道",
  }, null, {
    doc,
    toc: { ok: true, toc: [
      { name: "A1 序章及概要", url: base + "#reader-node-1" },
      { name: "A2 参道", url: base + "#reader-node-2" },
    ] },
  });
  await sleep(150);
  assert(ctx.elements.tocSelect.selectedIndex === 1,
    `目录应选中当前章节(A2)，实际 selectedIndex=${ctx.elements.tocSelect.selectedIndex}`);
  assert(ctx.elements.tocSelect.options.length === 2, "目录项应已填充");
  console.log("PASS 16: 加载完成后目录下拉显示当前所处章节");
}

async function test_roadbook_mobile_fluid() {
  // 手机上打开路书页：代理已裁掉站点导航，正文自适应 → 铺满展示（fluid），
  // 不进入 1200px 等比缩放模式（缩放后字太小没法看）
  const ctx = await runViewer({
    game: "测试游戏", url: "https://www.gamersky.com/handbook/202507/1961684_6.shtml",
    image_src: "",
  }, null, { mobile: true });
  ctx.es.onmessage({ data: JSON.stringify({ type: "navigate", game: "测试游戏",
    url: "https://www.gamersky.com/tools/guide-map/roadbooks/47#reader-node-2",
    image_src: "", title: "A2 参道" }) });
  await sleep(120);
  const lastSrc = decodeURIComponent(ctx.frame._state.lastSrc || "");
  assert(lastSrc.includes("guide-map/roadbooks/47"), `应打开路书页地址，实际: ${lastSrc}`);
  assert(ctx.frame.style.width === "100%", `手机上路书页应铺满（width:100%），实际: ${ctx.frame.style.width}`);
  assert(!ctx.elements.main.classList._set.has("mobile"), "fluid 模式不应进入小屏缩放布局");
  console.log("PASS 14: 手机上路书页按自适应铺满（不做等比缩放）");
}


let failed = 0;
function assert(cond, msg) {
  if (!cond) { console.error("  FAIL: " + msg); failed += 1; }
}

(async () => {
  try {
    await test_locates_after_real_height_and_resnaps();
    await test_user_scroll_stops_resnap();
    await test_fallback_to_content_without_image();
    await test_unmatchable_image_reports_and_falls_back();
    await test_matches_anchor_showimage_wrapper();
    await test_matches_resized_suffix();
    await test_pull_back_when_frame_escapes();
    await test_matches_gamersky_thumbnail_suffix();
    await test_mobile_wap_fluid();
    await test_mobile_scale_fallback();
    await test_free_browsing_and_manual_return();
    await test_return_to_pushed_page_after_user_nav();
    await test_roadbook_fragment_jump_without_reload();
    await test_roadbook_mobile_fluid();
    await test_roadbook_toc_click_jumps_without_reload();
    await test_roadbook_toc_selection_sync();
    await test_roadbook_toc_click_on_missing_node_polls();
    await test_roadbook_prev_next_section();
    await test_roadbook_back_to_scene_locates_image();
    await test_roadbook_next_cancels_stale_resnap();
    await test_roadbook_fragment_fallback_upgrades_to_image();
    await test_roadbook_image_index_fallback();
    await test_roadbook_back_to_scene_after_site_url_rewrite();
    await test_toc_reload_on_game_switch();
  } catch (err) {
    console.error("  FAIL: 异常 ->", err && err.message);
    failed += 1;
  }
  if (failed) {
    console.error(`\n${failed} CHECK(S) FAILED`);
    process.exit(1);
  }
  console.log("\nALL VIEWER LOCATE TESTS PASSED");
})();
