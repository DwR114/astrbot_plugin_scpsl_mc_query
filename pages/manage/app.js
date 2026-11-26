const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
let state = { servers: {}, admins: [], settings: {}, backgrounds: [] };

// ---------- 小工具（iframe 沙箱禁止 alert/confirm，这里自己做提示与二次确认） ----------
let toastTimer;
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 2600);
}
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids) e.append(c);
  return e;
}
/** 危险操作按钮：第一次点击变成“确认？”，3 秒内再点才执行 */
function confirmButton(label, action, cls = "danger small") {
  const b = el("button", { class: cls }, label);
  let armed = false, timer;
  b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    if (!armed) {
      armed = true; b.textContent = "确认？";
      timer = setTimeout(() => { armed = false; b.textContent = label; }, 3000);
      return;
    }
    clearTimeout(timer); armed = false; b.textContent = label;
    await action();
  });
  return b;
}
async function guarded(fn) {
  try { return await fn(); }
  catch (e) { toast(e?.message || String(e), true); }
}
const withPlatform = (v, kind) => {
  v = v.trim();
  return v && !v.includes(":") ? "aiocqhttp:" + v : v;
};

// ---------- 数据 ----------
async function refresh() {
  state = await bridge.apiGet("state");
  renderServers(); renderAdmins(); renderBg();
}

// ---------- 服务器 ----------
function renderServers() {
  const dl = $("scope-list"); dl.replaceChildren();
  const box = $("server-groups"); box.replaceChildren();
  const scopes = Object.keys(state.servers).sort();
  for (const sc of scopes) dl.append(el("option", { value: sc }));
  if (!scopes.length) { box.append(el("div", { class: "empty" }, "还没有绑定任何服务器")); return; }
  for (const sc of scopes) {
    const servers = state.servers[sc];
    box.append(el("div", { class: "group-title" }, el("span", {}, "群 " + sc),
      el("button", { class: "ghost small", onclick: () => { $("s-scope").value = sc; $("s-name").focus(); } }, "在此群添加")));
    const ul = el("ul", { class: "list card" });
    for (const [name, s] of Object.entries(servers)) {
      const addr = s.sid ? `scplist ID ${s.sid}` : `${s.kind === "mc" ? "MC " : ""}${s.host}:${s.port}`;
      const res = el("small", {}, addr);
      const test = el("button", { class: "ghost small", onclick: () => guarded(async () => {
        res.textContent = `${addr}  测试中…`;
        const r = await bridge.apiPost("servers/test", s.sid ? { sid: s.sid } : { host: s.host, port: s.port, kind: s.kind });
        res.textContent = r.ok ? `${addr}  ✅ ${r.players}/${r.max_players}` + (r.version ? ` · ${r.version}` : "") + (r.latency_ms != null ? `（${r.latency_ms}ms）` : "") : `${addr}  ❌ ${r.error}`;
      }) }, "测试");
      const edit = el("button", { class: "ghost small", onclick: () => {
        $("s-scope").value = sc; $("s-kind").value = s.kind === "mc" ? "mc" : "sl"; applyKind(); $("s-name").value = name; $("s-sid").value = s.sid || ""; $("s-host").value = s.host || ""; $("s-port").value = s.port || "";
        window.scrollTo({ top: 0, behavior: "smooth" });
      } }, "编辑");
      const del = confirmButton("删除", () => guarded(async () => {
        await bridge.apiPost("servers/delete", { scope: sc, name }); toast("已删除"); await refresh();
      }));
      ul.append(el("li", {}, el("div", { class: "main" }, el("b", {}, name), res), test, edit, del));
    }
    box.append(ul);
  }
}
function applyKind() {
  const mc = $("s-kind").value === "mc";
  $("sid-wrap").hidden = mc;
  $("addr-hint").textContent = mc ? "Java 版地址（端口留空默认 25565，会尝试 SRV 记录）：" : "或者用 A2S（UDP，需要服务器能连通）：";
  $("port-label").textContent = mc ? "端口（可空）" : "A2S 端口";
  $("s-port").placeholder = mc ? "25565" : "7777";
}
function serverForm() {
  $("s-kind").onchange = applyKind; applyKind();
  const body = () => ({
    scope: withPlatform($("s-scope").value), name: $("s-name").value.trim(),
    kind: $("s-kind").value, sid: $("s-sid").value.trim(), host: $("s-host").value.trim(), port: $("s-port").value.trim(),
  });
  $("s-save").onclick = () => guarded(async () => {
    const r = await bridge.apiPost("servers/save", body());
    toast(r.updated ? "已更新" : "已添加");
    $("s-name").value = ""; $("s-sid").value = ""; $("s-host").value = ""; $("s-port").value = "";
    await refresh();
  });
  $("s-test").onclick = () => guarded(async () => {
    const b = body(); const out = $("s-test-result"); out.textContent = "测试中…";
    const r = await bridge.apiPost("servers/test", b.kind === "mc" ? { kind: "mc", host: b.host, port: b.port } : (b.sid ? { sid: b.sid } : { host: b.host, port: b.port }));
    out.textContent = r.ok ? `✅ 在线 ${r.players}/${r.max_players}` + (r.version ? ` · 版本 ${r.version}` : "") + (r.latency_ms != null ? `，${r.latency_ms}ms` : "")
      : `❌ ${r.error}` + (b.sid || b.kind === "mc" ? "" : "（A2S 用 UDP，端口不一定等于游戏端口）");
  });
  const search = () => guarded(async () => {
    const ul = $("sl-results"); ul.replaceChildren(el("li", { class: "empty" }, "搜索中…"));
    const r = await bridge.apiPost("scplist/search", { keyword: $("sl-kw").value });
    ul.replaceChildren();
    if (!r.items.length) { ul.append(el("li", { class: "empty" }, "没有找到在线服务器")); return; }
    for (const it of r.items) {
      ul.append(el("li", {}, el("div", { class: "main" }, el("b", {}, it.name || `ID ${it.id}`), el("small", {}, `ID ${it.id} · ${it.players}/${it.max_players}`)),
        el("button", { class: "primary small", onclick: () => {
          $("s-sid").value = it.id;
          if (!$("s-name").value) $("s-name").value = (it.name || "").replace(/\s+/g, "").slice(0, 12);
          $("s-sid").scrollIntoView({ behavior: "smooth", block: "center" });
        } }, "填入")));
    }
  });
  $("sl-search").onclick = search;
  $("sl-kw").onkeydown = (e) => { if (e.key === "Enter") search(); };
}

// ---------- 管理员 ----------
function renderAdmins() {
  const ul = $("admin-list"); ul.replaceChildren();
  if (!state.admins.length) { ul.append(el("li", { class: "empty" }, "还没有管理员。群里发 /op 可成为首位管理员，或在上面直接添加。")); return; }
  for (const uid of state.admins) {
    ul.append(el("li", {}, el("div", { class: "main" }, el("b", {}, uid)),
      confirmButton("移除", () => guarded(async () => { await bridge.apiPost("admins/remove", { uid }); toast("已移除"); await refresh(); }))));
  }
}
function adminForm() {
  $("a-add").onclick = () => guarded(async () => {
    const r = await bridge.apiPost("admins/add", { uid: withPlatform($("a-uid").value) });
    toast(r.added ? "已添加" : "已经是管理员"); $("a-uid").value = ""; await refresh();
  });
}

// ---------- 背景与出图 ----------
function renderBg() {
  const s = state.settings;
  $("opt-image").checked = !!s.use_image;
  $("opt-random").checked = !!s.random_bg;
  $("opt-quote").checked = s.show_quote !== false;
  $("q-count").textContent = state.quote_count ?? 0;
  const grid = $("bg-grid"); grid.replaceChildren();
  $("bg-empty").hidden = state.backgrounds.length > 0;
  for (const b of state.backgrounds) {
    const sel = s.selected_bg === b.id;
    const card = el("div", { class: "thumb" + (sel ? " sel" : ""), onclick: () => guarded(async () => {
      await bridge.apiPost("settings/save", { selected_bg: sel ? null : b.id }); await refresh();
    }) });
    if (b.thumb) card.append(el("img", { src: b.thumb, alt: b.name }));
    if (sel) card.append(el("span", { class: "tag" }, "指定"));
    card.append(el("div", { class: "nm" }, b.name));
    card.append(el("div", { class: "bar" },
      el("button", { class: "ghost small", onclick: (ev) => { ev.stopPropagation(); preview(b.id); } }, "预览"),
      confirmButton("删除", () => guarded(async () => { await bridge.apiPost("backgrounds/delete", { id: b.id }); toast("已删除"); await refresh(); }))));
    grid.append(card);
  }
}
async function preview(bgId) {
  await guarded(async () => {
    toast("正在生成预览…");
    const r = await bridge.apiPost("preview", bgId ? { bg_id: bgId } : {});
    $("preview-img").src = r.image; $("preview-card").hidden = false;
    $("preview-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}
function bgForm() {
  for (const [id, key] of [["opt-image", "use_image"], ["opt-random", "random_bg"], ["opt-quote", "show_quote"]]) {
    $(id).onchange = (e) => guarded(async () => {
      await bridge.apiPost("settings/save", { [key]: e.target.checked }); await refresh();
    });
  }
  $("bg-preview-cur").onclick = () => preview(null);
  $("bg-file").onchange = (e) => guarded(async () => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    if (f.size > 15 * 1024 * 1024) { toast("图片超过 15MB", true); return; }
    toast("上传中…");
    await bridge.upload("backgrounds/upload", f);
    toast("上传成功"); await refresh();
  });
}

// ---------- 语录 ----------
const Q = { offset: 0, limit: 50, kw: "" };
async function loadQuotes(reset = true) {
  if (reset) { Q.offset = 0; Q.kw = $("q-search").value.trim(); $("q-list").replaceChildren(); }
  const r = await bridge.apiPost("quotes/list", { q: Q.kw, offset: Q.offset, limit: Q.limit });
  $("q-count").textContent = r.all;
  const ul = $("q-list");
  if (reset && !r.items.length) ul.append(el("li", { class: "empty" }, Q.kw ? "没有匹配的语录" : "还没有语录，添加一条或从文件导入"));
  for (const it of r.items) {
    ul.append(el("li", {}, el("div", { class: "main" }, el("span", {}, it.text)),
      confirmButton("删除", () => guarded(async () => { await bridge.apiPost("quotes/delete", { id: it.id }); toast("已删除"); await loadQuotes(); }))));
  }
  Q.offset += r.items.length;
  $("q-more").hidden = Q.offset >= r.total;
}
function quotesForm() {
  $("q-add").onclick = () => guarded(async () => {
    const t = $("q-text").value.trim(); if (!t) return;
    const r = await bridge.apiPost("quotes/add", { text: t });
    toast(r.added ? "已添加" : "已存在，未重复添加"); $("q-text").value = ""; await loadQuotes();
  });
  $("q-text").onkeydown = (e) => { if (e.key === "Enter") $("q-add").click(); };
  $("q-search-btn").onclick = () => guarded(() => loadQuotes());
  $("q-search").onkeydown = (e) => { if (e.key === "Enter") guarded(() => loadQuotes()); };
  $("q-more").onclick = () => guarded(() => loadQuotes(false));
  $("q-clear-slot").append(confirmButton("清空全部", () => guarded(async () => {
    const r = await bridge.apiPost("quotes/clear", {}); toast(`已清空 ${r.removed} 条`);
    $("q-import-result").textContent = ""; await loadQuotes();
  }), "danger"));
  $("q-file").onchange = (e) => guarded(async () => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { toast("文件超过 2MB", true); return; }
    toast("导入中…");
    const r = await bridge.upload("quotes/import", f);
    const bits = [`新增 ${r.added} 条`, `重复跳过 ${r.duplicate} 条`];
    if (r.skipped_long) bits.push(`过长忽略 ${r.skipped_long} 条`);
    if (r.too_many) bits.push(`超出上限未导入 ${r.too_many} 条`);
    $("q-import-result").textContent = `${f.name}：` + bits.join("，") + `；现共 ${r.total} 条`;
    toast("导入完成"); await loadQuotes();
  });
}

// ---------- 标签页 ----------
function tabs() {
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    for (const t of ["servers", "admins", "bg", "quotes"]) $("tab-" + t).hidden = t !== b.dataset.tab;
  }));
}

await bridge.ready();
tabs(); serverForm(); adminForm(); bgForm(); quotesForm();
await guarded(refresh);
await guarded(() => loadQuotes());
