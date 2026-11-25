/* Offline editor regressions. Run with node tests/test_editor.js; no browser, server, or media is used. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const boot = [...script.matchAll(/\n\s*\(async \(\) => \{/g)].pop()?.index ?? -1;
assert.ok(boot > 0, "exclude the network bootstrap, as in test_homepage.js");
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const response = body => ({ok: true, status: 200, json: async () => body});
const decode = s => String(s).replace(/&(?:amp|lt|gt|quot|#39);/g, x => ({"&amp;":"&", "&lt;":"<", "&gt;":">", "&quot;":'"', "&#39;":"'"}[x]));
function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return html.match(new RegExp(escaped + "\\s*\\{([^}]+)\\}"))?.[1] || "";
}
function cssColor(value) {
  if (value.startsWith("var(")) value = html.match(new RegExp(value.slice(4, -1).trim() + ":\\s*(#[A-Fa-f0-9]+)"))?.[1] || "";
  assert.match(value, /^#[A-Fa-f0-9]{6}$/);
  return value.match(/[A-Fa-f0-9]{2}/g).map(n => parseInt(n, 16) / 255);
}
function contrast(foreground, background) {
  const lum = value => cssColor(value).map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
  const a = lum(foreground), b = lum(background); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function page() {
  const listeners = new Map(), timers = new Map(), notices = [];
  let timerId = 0;
  let document;
  function matches(el, selector) {
    if (!el || el.tagName === "#TEXT") return false;
    return selector.split(",").some(part => {
      const s = part.trim(), tag = s.match(/^[\w-]+/);
      if (tag && el.tagName !== tag[0].toUpperCase()) return false;
      const id = s.match(/#([\w-]+)/);
      if (id && el.id !== id[1]) return false;
      for (const [, name] of s.matchAll(/\.([\w-]+)/g)) if (!el.classList.contains(name)) return false;
      for (const [, name, , value] of s.matchAll(/\[([\w-]+)(?:=(['"]?)(.*?)\2)?\]/g)) {
        if (!el.attributes.has(name) || value !== undefined && el.getAttribute(name) !== value) return false;
      }
      return true;
    });
  }
  function all(root, selector) {
    const parts = selector.trim().split(/\s+(?![^\[]*\])/), last = parts.pop(), found = [];
    function walk(parent) {
      for (const el of parent.children) {
        if (matches(el, last)) {
          let ancestor = el.parentElement, i = parts.length - 1;
          while (i >= 0 && ancestor) { if (matches(ancestor, parts[i])) i--; ancestor = ancestor.parentElement; }
          if (i < 0) found.push(el);
        }
        walk(el);
      }
    }
    walk(root); return found;
  }
  function node(tag = "div") {
    const attributes = new Map(), events = new Map(), classes = new Set();
    const el = {
      tagName: tag.toUpperCase(), attributes, events, dataset: new Proxy({}, {set(target, key, value) {
        target[key] = String(value); attributes.set("data-" + key.replace(/[A-Z]/g, x => "-" + x.toLowerCase()), String(value)); return true;
      }}), style: {setProperty(k, v) { this[k] = v; }},
      children: [], parentElement: null, writes: 0, value: "", checked: false, disabled: false, hidden: false,
      open: false, inert: false, currentTime: 0, paused: true, muted: false, rect: {left: 0, top: 0, width: 400, height: 300},
      classList: {contains: k => classes.has(k), add: (...ks) => ks.forEach(k => classes.add(k)),
        remove: (...ks) => ks.forEach(k => classes.delete(k)), toggle(k, force) { const on = force ?? !classes.has(k); if (on) classes.add(k); else classes.delete(k); return on; }},
      setAttribute(k, v) { v = String(v); attributes.set(k, v); if (k === "class") this.className = v; if (k === "id") this.id = v;
        if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] = v;
        if (k === "value") this.value = v; if (["hidden", "disabled", "checked", "inert", "muted"].includes(k)) this[k] = true; },
      getAttribute: k => attributes.get(k) ?? null,
      removeAttribute(k) { attributes.delete(k); if (["hidden", "disabled", "inert"].includes(k)) this[k] = false; },
      append(...els) { els.forEach(x => this.insertBefore(x, null)); },
      insertBefore(x, before) { x.remove(); x.parentElement = this; const i = before ? this.children.indexOf(before) : -1; this.children.splice(i < 0 ? this.children.length : i, 0, x); },
      remove() { if (this.parentElement) { const a = this.parentElement.children; a.splice(a.indexOf(this), 1); this.parentElement = null; } },
      contains(x) { for (; x; x = x.parentElement) if (x === this) return true; return false; },
      matches(s) { return matches(this, s); }, closest(s) { for (let x = this; x; x = x.parentElement) if (matches(x, s)) return x; return null; },
      querySelectorAll(s) { return all(this, s); }, querySelector(s) { return all(this, s)[0] || null; },
      addEventListener(k, fn) { events.set(k, [...(events.get(k) || []), fn]); }, removeEventListener() {},
      focus() { document.activeElement = this; }, scrollTo() {},
      showModal() { this.open = true; }, close() { this.open = false; (events.get("close") || []).forEach(fn => fn({target: this, currentTarget: this})); },
      getBoundingClientRect() { return {...this.rect}; },
      setPointerCapture(id) { this.capture = id; }, releasePointerCapture() { this.capture = null; }, hasPointerCapture(id) { return this.capture === id; },
      play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; }, load() {},
    };
    Object.defineProperties(el, {
      className: {get: () => [...classes].join(" "), set: s => { classes.clear(); String(s).split(/\s+/).filter(Boolean).forEach(k => classes.add(k)); attributes.set("class", s); }},
      isConnected: {get: () => !!el.parentElement},
      textContent: {get: () => el._text || el.children.map(x => x.textContent).join(""), set: s => { el._text = String(s); }},
      innerHTML: {get: () => el._html || "", set: markup => {
        el._html = String(markup); el.writes++; el.children.forEach(x => { x.parentElement = null; }); el.children = [];
        const stack = [el];
        for (const token of String(markup).matchAll(/<\/?[\w-]+[^>]*>|[^<]+/g)) {
          const text = token[0];
          if (!text.startsWith("<")) { const p = stack[stack.length - 1]; p._text = (p._text || "") + decode(text); if (p.tagName === "TEXTAREA") p.value = p._text; continue; }
          if (text.startsWith("</")) { const tag = text.match(/^<\/([\w-]+)/)[1].toUpperCase(); while (stack.length > 1) if (stack.pop().tagName === tag) break; continue; }
          const [, tag, attrs] = text.match(/^<([\w-]+)([\s\S]*?)\/?\s*>$/), child = node(tag);
          for (const [, name, a, b, c] of attrs.matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) child.setAttribute(name, decode(a ?? b ?? c ?? ""));
          stack[stack.length - 1].append(child);
          if (!/^(input|img|br|hr|source|path|meta|link)$/i.test(tag) && !text.endsWith("/>")) stack.push(child);
        }
        for (const select of el.querySelectorAll("select")) select.value = (select.querySelector("option[selected]") || select.querySelector("option"))?.value || "";
      }},
    });
    return el;
  }
  const body = node("body");
  document = {body, activeElement: null, hidden: false,
    querySelector: s => body.querySelector(s), querySelectorAll: s => body.querySelectorAll(s), createElement: node,
    addEventListener(k, fn) { listeners.set(k, [...(listeners.get(k) || []), fn]); }};
  for (const id of ["app", "main", "clipdlg", "file", "bellpop", "bellBtn", "bellDot", "nav", "foot", "topbarAiPill", "tpic", "utabs", "title", "subtitle", "actions", "view", "jobs", "selbar", "vbanner", "sync_status", "toasts"]) {
    const el = node(id === "clipdlg" ? "dialog" : "div"); el.setAttribute("id", id); body.append(el);
  }
  const topbar = node(); topbar.className = "topbar"; body.append(topbar);
  const window = {addEventListener() {}};
  const context = vm.createContext({document, window, innerWidth: 1440, AbortController, URL, URLSearchParams, console,
    localStorage: {getItem: () => null, setItem() {}}, location: {pathname: "/app/videos", hostname: "127.0.0.1"},
    history: {pushState() {}, replaceState() {}}, addEventListener() {}, requestAnimationFrame: fn => fn(),
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout: id => timers.delete(id),
    fetch: () => { throw new Error("Unexpected network request in offline editor test"); }, notices,
    Audio: function() { return {play: async () => {}, pause() {}}; },
  });
  vm.runInContext(script.slice(0, boot), context);
  vm.runInContext("toast = (message, kind) => notices.push({message, kind}); queueEditorRefresh = () => {};", context);
  const run = code => vm.runInContext(code, context);
  const p = {context, document, timers, notices, node, run, state: run("S"), get: selector => document.querySelector(selector),
    dispatch: (name, target, more = {}) => Promise.all((listeners.get(name) || []).map(fn => fn({target, preventDefault() {}, ...more}))),
    emit: (el, name, more = {}) => Promise.all((el.events.get(name) || []).map(fn => fn({target: el, currentTarget: el, preventDefault() {}, ...more}))),
    open(jid = "a", cid = "clip01") { run(`openClip(${JSON.stringify(jid)}, ${JSON.stringify(cid)})`); },
    draft(jid = "a", cid = "clip01") { return run(`CLIP_DRAFTS[JSON.stringify([${JSON.stringify(jid)},${JSON.stringify(cid)}])]`); },
  };
  p.state.view = "library"; p.state.status.styles = {bounce: "Bounce", clean: "Clean"}; p.state.status.layouts = {blur: "Blurred fill", zoom: "Zoom", crop: "Crop"};
  p.state.jobs = [job("a"), job("b")];
  return p;
}
function job(id, revision = 7) {
  return {id, status: "done", stage: "Ready", source: {name: id, type: "file"}, clips: [{id: "clip01", status: "done", version: revision, artifact_revision: revision,
    file: "saved.mp4", cover: "saved.jpg", start: 10, end: 40, hook: "Saved " + id, title: "Title " + id, description: "Description", style: "bounce", layout: "blur",
    voiceover: {enabled: true, mode: "mute", voice: "en-US-AndrewNeural", rate: "+0%", script: "Saved narration " + id}}]};
}
function setField(p, field, value, voice = false) { const el = p.get(`#clipdlg [data-${voice ? "vo" : "f"}='${field}']`); assert.ok(el, "fixture control exists: " + field); el.value = String(value); p.run("saveClipDraft()"); }
const action = p => p.run('EDITOR_ACTIONS[JSON.stringify(["a","clip01"])]');
function mediaFixture(p) {
  const media = p.run('editorMedia(clipKey("a","clip01"))');
  media.source = {status: "ready", url: "/editor-media/a/asset", offset: 0, start: 0, end: 100, width: 1920, height: 1080, muted: true};
  media.resolved = {schema_version: 1, mode: "legacy", width: 1080, height: 1920, layout: "blur", effective_roi: {x: 0, y: 0, width: 1, height: 1}, foreground: {x: 0, y: 656, width: 1080, height: 608}, hook_y: 466, caption_y: 1640, warnings: []};
  media.catalog = {fonts: [{id: "poppins", label: "Poppins ExtraBold"}, {id: "anton", label: "Anton"}], defaults: plain(p.run("VISUAL_DEFAULTS"))};
  media.geometryKey = p.run('sourceRequestKey(CLIP_DRAFTS[clipKey("a","clip01")])'); media.sourceKey = media.geometryKey;
  return media;
}
function bitmapFixture(image = "data:image/png;base64,ZmFrZQ==") {
  return {image, width: 400, height: 100, canvas_width: 1080, canvas_height: 1920, position: {x: 0.5, y: 0.2}, warnings: []};
}

let checks = 0, failures = 0;
async function check(name, test) {
  checks++;
  try { await test(); console.log("PASS " + name); }
  catch (error) { failures++; console.error("FAIL " + name + ": " + error.stack); }
}

(async () => {
  await check("inline JavaScript parses without executing the bootstrap", () => { new vm.Script(script); });
  await check("a ready visual source never replaces or pauses the audible saved clip", async () => {
    const p = page(); p.open(); const saved = p.get("#saved_clip_video"), media = mediaFixture(p);
    await saved.play(); p.run("loadCleanSource()");
    assert.equal(media.pane, "saved"); assert.equal(saved.paused, false);
    assert.equal(saved.muted, false); assert.equal(p.get("#saved_clip_pane").hidden, false);
    assert.equal(p.get("#clean_clip_pane").hidden, true);
  });
  await check("the card play action opens and starts the saved MP4, not muted source media", async () => {
    const p = page(); p.run("drawJobs()"); const shot = p.get('section[data-job="a"] .shot');
    await p.dispatch("click", shot);
    const video = p.get("#saved_clip_video"); assert.equal(video.paused, false); assert.equal(video.muted, false);
    assert.match(video.getAttribute("src"), /\/media\/a\/saved\.mp4/);
  });
  await check("unavailable secondary crop playback does not break the main visual preview", async () => {
    const p = page(); p.open(); mediaFixture(p); p.run('loadCleanSource(); setEditorPane("clean",true)');
    p.get("#crop_source_video").play = () => Promise.reject(new Error("secondary preview unavailable"));
    await p.dispatch("click", p.get("#clean_play_btn"));
    assert.equal(p.get("#editor_source_video").paused, false); assert.equal(p.get("#clean_play_btn").textContent, "Pause");
    assert.equal(p.notices.some(n => n.kind === "bad"), false);
  });
  await check("late background clean-media errors never interrupt audible rendered Preview", async () => {
    const p = page(); p.open(); const media = mediaFixture(p);
    media.preview = {id:"p1",url:"/media/a/preview.mp4",revision:8,generation:p.draft().generation};
    p.run('loadCleanSource(); setEditorPane("preview",true)');
    const video = p.get("#rendered_preview_video"); await video.play();
    await p.emit(p.get("#crop_source_video"),"error");
    assert.equal(media.pane,"preview"); assert.equal(video.paused,false);
    await p.emit(p.get("#editor_source_video"),"error");
    assert.equal(media.pane,"preview"); assert.equal(video.paused,false);
  });
  await check("editor source progress labels and percentages remain readable on the dark stage", () => {
    for (const selector of [".clip-stage-col .progress-heading",".clip-stage-col .progress-heading strong"]) {
      const foreground = cssRule(selector).match(/(?:^|;)\s*color:([^;]+)/)?.[1].trim();
      assert.ok(contrast(foreground || "#121212","#171717") >= 4.5,selector);
    }
  });
  await check("preview controls and the card play icon have readable text/icon contrast", () => {
    const rule = cssRule(".clip-stage-col .btn");
    const color = rule.match(/(?:^|;)\s*color:([^;]+)/)?.[1].trim();
    const background = rule.match(/(?:^|;)\s*background:([^;]+)/)?.[1].trim() || "#FFFFFF";
    assert.ok(contrast(color, background) >= 4.5, "inactive preview tabs cannot be white on white");
    const play = cssRule(".veil span");
    assert.ok(contrast(play.match(/(?:^|;)\s*color:([^;]+)/)[1].trim(), play.match(/(?:^|;)\s*background:([^;]+)/)[1].trim()) >= 4.5);
  });
  await check("editor options are organized into four panels with only one visible at a time", async () => {
    const p = page(); p.open();
    assert.equal(p.get(".editor-settings-nav").querySelectorAll('[role="tab"]').length, 4);
    assert.equal(p.get('[data-editor-panel="appearance"]').hidden, false);
    assert.equal(p.get('[data-editor-panel="framing"]').hidden, true);
    await p.dispatch("click", p.get('[data-editor-tab="framing"]'));
    assert.equal(p.get('[data-editor-panel="appearance"]').hidden, true);
    assert.equal(p.get('[data-editor-panel="framing"]').hidden, false);
    assert.equal(p.get('[data-editor-tab="framing"]').getAttribute("aria-selected"), "true");
  });
  await check("clip loading stays inside its fixed media frame and ready jobs hide completed progress", () => {
    const p = page(), j = p.state.jobs[0]; j.outcome = "success"; j.progress_detail = {phase:"Making clips",completed:2,total:2};
    j.clips[0].attempt = {id:"render1",kind:"render",status:"running",progress:{phase:"encode",completed:2,total:10}};
    j.active_work = true; p.run("drawJobs()");
    const progress = p.get('section[data-job="a"] [data-clip-progress]'); assert.ok(progress.closest(".shot"));
    j.clips[0].attempt.status = "done"; j.active_work = false; p.run("drawJobs()");
    assert.equal(p.get('section[data-job="a"] [data-job-progress]').hidden, true);
    assert.doesNotMatch(script, /Phase elapsed|Last advance|Heartbeat|of this phase/);
  });
  await check("unknown work retains a visible percentage instead of a sweeping indeterminate bar", () => {
    const p = page(), el = p.node(); p.document.body.append(el); p.context.progressNode = el;
    p.run('patchProgress(progressNode,{phase:"Finding the best moments",total:null},{id:"r1",kind:"render",status:"running"})');
    assert.match(el.querySelector("[data-progress-percent]").textContent, /\d+%/);
    assert.equal(el.querySelector('[role="progressbar"]').classList.contains("indeterminate"), false);
    assert.doesNotMatch(html, /phase-scan/);
  });
  await check("progress increases across measured phases and never resets on encoder fallback", () => {
    const p = page(), el = p.node(); p.document.body.append(el); p.context.progressNode = el;
    const update = detail => { p.context.detail = detail; p.run('patchProgress(progressNode,detail,{id:"r1",kind:"render",status:"running"})'); return Number(el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow")); };
    const encoded = update({phase:"encode",completed:8,total:10});
    const fallback = update({phase:"encode",phase_attempt:2,completed:1,total:10});
    const cover = update({phase:"cover",completed:0,total:null});
    const publishing = update({phase:"publish",completed:0,total:null});
    assert.ok(fallback >= encoded); assert.ok(cover >= fallback); assert.ok(publishing >= cover); assert.ok(publishing < 100);
  });
  await check("two jobs with clip01 retain distinct drafts and voice scripts", () => {
    const p = page(); p.open("a"); setField(p, "hook", "Draft A"); setField(p, "script", "Voice A", true); p.get("#clipdlg").close();
    p.open("b"); setField(p, "hook", "Draft B"); setField(p, "script", "Voice B", true); p.get("#clipdlg").close(); p.open("a");
    assert.equal(p.get("#clipdlg [data-f='hook']").value, "Draft A"); assert.equal(p.draft("a").voScript, "Voice A"); assert.equal(p.draft("b").voScript, "Voice B");
    assert.deepEqual(Object.keys(p.run("CLIP_DRAFTS")).sort(), ['["a","clip01"]', '["b","clip01"]']);
  });
  await check("composite keys cannot collide through delimiters", () => {
    const p = page(); assert.notEqual(p.run('clipKey("a:b", "c")'), p.run('clipKey("a", "b:c")')); assert.equal(p.run('clipKey("a","clip01")'), '["a","clip01"]');
  });
  await check("dirty draft and focused controls survive sync and close/reopen", () => {
    const p = page(); p.open(); setField(p, "hook", "Unsaved"); const field = p.get("#clipdlg [data-f='hook']"); field.focus();
    p.state.jobs[0].clips[0].attempt = {id: "a1", kind: "render", status: "running", revision: 8, progress: {phase: "encode", completed: 2, total: 30}};
    p.run("drawClipDlg(false)"); assert.equal(p.get("#clipdlg [data-f='hook']"), field); assert.equal(p.document.activeElement, field);
    const generation = p.draft().generation; assert.ok(p.draft().dirty); p.get("#clipdlg").close(); p.open();
    assert.equal(p.get("#clipdlg [data-f='hook']").value, "Unsaved"); assert.equal(p.draft().generation, generation);
  });
  await check("nested visual serialization preserves zero, transparent backgrounds, and excludes UI/internal fields", () => {
    const p = page(); p.open(); const d = p.draft(); d.visual = {schema_version: 1, headline: {font: "anton", size: 96, color: "#123456", background: null, width: 5 / 6, position: {x: 0, y: 0}},
      framing: {mode: "manual", ratio: "1:1", roi: {x: 0, y: 0, width: 1, height: 1}, fit: "contain"}, private_path: "do-not-send"};
    const body = plain(p.run('serializeClipDraft(CLIP_DRAFTS[clipKey("a","clip01")],jobOf("a").clips[0])'));
    assert.equal(body.expected_revision, 7); assert.deepEqual(body.visual.headline.position, {x: 0, y: 0}); assert.equal(body.visual.headline.background, null);
    assert.equal(body.visual.framing.ratio, "1:1"); assert.equal("private_path" in body.visual, false); assert.equal("generation" in body, false);
    assert.equal("voiceover" in body, false, "unchanged narration is omitted so the renderer reuses its successful asset");
  });
  await check("untouched legacy framing is omitted and longer legacy text survives visual-only edits", () => {
    const p = page(); p.state.jobs[0].clips[0].hook = "L".repeat(80); p.open();
    const body = p.run('serializeClipDraft(CLIP_DRAFTS[clipKey("a","clip01")],jobOf("a").clips[0])'); assert.equal(body.hook.length, 80); assert.equal("visual" in body, false);
    setField(p, "hook", "N".repeat(61)); assert.throws(() => p.run('serializeClipDraft(CLIP_DRAFTS[clipKey("a","clip01")],jobOf("a").clips[0])'), /60/);
  });
  await check("serializer rejects nonfinite intervals and malformed crop geometry", () => {
    const p = page(); p.open(); p.draft().start = NaN;
    assert.throws(() => p.run('serializeClipDraft(CLIP_DRAFTS[clipKey("a","clip01")],jobOf("a").clips[0])'), /time|finite|start/i);
    assert.throws(() => p.run('serializeVisual({schema_version:1,framing:{mode:"manual",ratio:"1:1",roi:{x:0,y:0,width:0,height:1},fit:"crop"}})'), /crop|ROI|width/i);
  });
  await check("serializer inherits partial nested legacy defaults and preserves renderer-supported sizes", () => {
    const p = page(); p.open();
    const visual = plain(p.run('serializeVisual({schema_version:1,headline:{size:512,width:0.1,background:{opacity:0}}})'));
    assert.equal(visual.headline.size, 512); assert.equal(visual.headline.width, 0.1); assert.deepEqual(visual.headline.background, {color: "#FFFFFF", opacity: 0});
    assert.throws(() => p.run('serializeVisual({schema_version:1,framing:{mode:"legacy",ratio:"1:1"}})'), /manual/i);
  });
  await check("caption style samples use renderer RGB catalog metadata rather than BGR-like hints", () => {
    const p = page(); p.state.status.visual = {styles: {clean: {font: "Poppins ExtraBold", active: "#FFD76B", base: "#FFFFFF", strong: "#FFFFFF"}}};
    assert.match(p.run('capPrev("clean")'), /#FFD76B/);
  });
  await check("pointer mapping excludes letterbox gutters and clamps the complete padded bitmap", () => {
    const p = page(); const rect = plain(p.run('containedRect({left:10,top:20,width:500,height:500},16,9)'));
    assert.deepEqual(rect, {left: 10, top: 129.375, width: 500, height: 281.25});
    p.context.rect = rect;
    assert.deepEqual(plain(p.run('pointerHeadlinePosition(rect,10,20,{width:200,height:90,canvas_width:1000,canvas_height:500})')), {x: 0.1, y: 0.09});
    assert.deepEqual(plain(p.run('pointerHeadlinePosition(rect,510,520,{width:200,height:90,canvas_width:1000,canvas_height:500})')), {x: 0.9, y: 0.91});
    p.context.rect = {left: 0, top: 0, width: 1000, height: 562.5};
    assert.deepEqual(plain(p.run('pointerHeadlinePosition(rect,500,281.25,{width:200,height:90,canvas_width:1000,canvas_height:500})')), {x: 0.5, y: 0.5});
  });
  await check("manual presets lock source-space crop proportions without stretching", () => {
    const p = page(); const roi = plain(p.run('roiForRatio({x:0,y:0,width:1,height:1},"9:16",1920,1080)'));
    assert.ok(Math.abs(roi.width * 1920 / (roi.height * 1080) - 9 / 16) < 1e-8); assert.equal(roi.height, 1); assert.ok(roi.x > 0);
    assert.deepEqual(plain(p.run('roiForRatio({x:0,y:0,width:1,height:1},"source",1920,1080)')), {x: 0, y: 0, width: 1, height: 1});
  });
  await check("instant crop geometry matches renderer even-pixel ROI and ratio unit rules", () => {
    const p = page(); p.open(); mediaFixture(p);
    p.run('CLIP_DRAFTS[clipKey("a","clip01")].visual={schema_version:1,framing:{mode:"manual",ratio:"9:16",fit:"crop",roi:roiForRatio({x:0,y:0,width:1,height:1},"9:16",1920,1080)}}');
    const geometry = plain(p.run('visualGeometry(CLIP_DRAFTS[clipKey("a","clip01")],editorMedia())'));
    assert.equal(geometry.width, 594); assert.equal(geometry.height, 1056); assert.deepEqual(geometry.crop_pixels, {x: 662, y: 12, width: 594, height: 1056});
    assert.deepEqual(geometry.foreground, {x: 0, y: 0, width: 594, height: 1056});
  });
  await check("square manual crop applies one effective crop even when input ROI is full source", () => {
    const p = page(); p.open(); mediaFixture(p); p.draft().visual = {schema_version: 1, framing: {mode: "manual", ratio: "1:1", fit: "crop", roi: {x: 0, y: 0, width: 1, height: 1}}};
    const g = plain(p.run('visualGeometry(CLIP_DRAFTS[clipKey("a","clip01")],editorMedia())'));
    assert.equal(g.width, 1080); assert.equal(g.height, 1080); assert.deepEqual(g.crop_pixels, {x: 420, y: 0, width: 1080, height: 1080}); assert.equal(g.foreground.width, g.foreground.height);
  });
  await check("Source ratio preserves the original picture aspect, not the selected ROI aspect", () => {
    const p = page(); p.open(); mediaFixture(p); p.draft().visual = {schema_version: 1, framing: {mode: "manual", ratio: "source", fit: "crop", roi: {x: 0.25, y: 0, width: 0.5, height: 1}}};
    const g = plain(p.run('visualGeometry(CLIP_DRAFTS[clipKey("a","clip01")],editorMedia())'));
    assert.equal(g.width, 960); assert.equal(g.height, 540); assert.deepEqual(g.crop_pixels, {x: 480, y: 270, width: 960, height: 540});
  });
  await check("SAR-normalized display pixels do not imply extra coded source detail", () => {
    const p = page(); p.open(); const m = mediaFixture(p); m.source.width = 3840; m.resolved.source = {coded_width: 1920, coded_height: 1080, width: 3840, height: 1080, rotation: 0, sar: "2:1"};
    p.draft().visual = {schema_version: 1, framing: {mode: "manual", ratio: "1:1", fit: "crop", roi: {x: 0, y: 0, width: 1, height: 1}}};
    const g = plain(p.run('visualGeometry(CLIP_DRAFTS[clipKey("a","clip01")],editorMedia())'));
    assert.equal(g.width, 540); assert.equal(g.height, 540); assert.ok(g.warnings.includes("sar_normalized"));
  });
  await check("position-only headline edits do not change the bitmap request/cache key", () => {
    const p = page(); p.open(); p.draft().visual = {schema_version: 1, headline: {font: "poppins", position: {x: 0.2, y: 0.3}}};
    const before = p.run('headlineBitmapKey(CLIP_DRAFTS[clipKey("a","clip01")])'); p.draft().visual.headline.position = {x: 0.7, y: 0.8};
    assert.equal(p.run('headlineBitmapKey(CLIP_DRAFTS[clipKey("a","clip01")])'), before);
    p.draft().visual.headline.font = "anton"; assert.notEqual(p.run('headlineBitmapKey(CLIP_DRAFTS[clipKey("a","clip01")])'), before);
  });
  await check("out-of-order bitmap replies cannot replace the latest text/style", async () => {
    const p = page(); p.open(); const media = mediaFixture(p), old = deferred(), latest = deferred(); let calls = 0;
    p.context.fetch = () => (++calls === 1 ? old.promise : latest.promise);
    const a = p.run("refreshHeadline()"); setField(p, "hook", "Latest hook"); const b = p.run("refreshHeadline()");
    latest.resolve(response(bitmapFixture("data:image/png;base64,bmV3"))); await b; old.resolve(response(bitmapFixture("data:image/png;base64,b2xk"))); await a;
    assert.equal(media.bitmap.image, "data:image/png;base64,bmV3"); assert.equal(p.get("#editor_headline_bitmap").getAttribute("src"), media.bitmap.image);
  });
  await check("position during bitmap generation uses latest transform without another encode request", async () => {
    const p = page(); p.open(); mediaFixture(p); const r = deferred(); let calls = 0;
    p.context.fetch = (_, opt) => { calls++; assert.equal(JSON.parse(opt.body).visual?.headline?.position ?? null, null); return r.promise; };
    const work = p.run("refreshHeadline()"); const x = p.get("#headline_x"); x.value = "75"; p.context.field = x; p.run("handleVisualInput(field)");
    r.resolve(response(bitmapFixture())); await work;
    assert.equal(calls, 1); assert.equal(p.draft().visual.headline.position.x, 0.75); assert.equal(p.get("#editor_headline").style.left, "75%");
  });
  await check("headline pointer capture and keyboard arrows constrain the entire bitmap in the current rect", async () => {
    const p = page(); p.open(); mediaFixture(p); p.context.fetch = async () => response(bitmapFixture()); await p.run("refreshHeadline()");
    const frame = p.get("#editor_output"), head = p.get("#editor_headline"); frame.rect = {left: 40, top: 50, width: 216, height: 384};
    await p.emit(head, "pointerdown", {pointerId: 5, button: 0, clientX: 148, clientY: 126.8}); assert.equal(head.capture, 5);
    await p.emit(head, "pointermove", {pointerId: 5, clientX: -100, clientY: -100});
    assert.equal(p.draft().visual.headline.position.x, 400 / 1080 / 2); assert.equal(p.draft().visual.headline.position.y, 100 / 1920 / 2);
    await p.emit(head, "pointerup", {pointerId: 5}); assert.equal(head.capture, null);
    const x = p.draft().visual.headline.position.x; await p.emit(head, "keydown", {key: "ArrowRight", shiftKey: true}); assert.equal(p.draft().visual.headline.position.x, x + 10 / 1080);
  });
  await check("Preview consumes its queued attempt immediately and never calls persistent render", async () => {
    const p = page(); p.open(); setField(p, "hook", "Preview draft"); const calls = [];
    p.context.fetch = async (url, opt) => { calls.push({url, body: JSON.parse(opt.body)}); return response({attempt: {id: "p1", kind: "preview", status: "queued", revision: 8}, job: job("a")}); };
    await p.run('submitClipAttempt("preview")'); assert.equal(calls.length, 1); assert.equal(calls[0].url, "/api/jobs/a/clips/clip01/preview");
    assert.equal(calls[0].body.expected_revision, 7); assert.equal(action(p).attempt.id, "p1"); assert.equal(p.state.jobs[0].clips[0].file, "saved.mp4");
  });
  for (const [id, kind] of [["clip_preview_btn", "preview"], ["clip_remake_btn", "render"]]) {
    await check(`the actual ${id} click uses the immutable ${kind} endpoint and serializer`, async () => {
      const p = page(); p.open(); const calls = [];
      p.context.fetch = async (url, opt) => { calls.push({url, body: JSON.parse(opt.body)}); return response({attempt: {id: "click1", kind, status: "queued", revision: 8}, job: job("a")}); };
      const work = p.dispatch("click", p.get("#" + id)); await Promise.resolve();
      assert.equal(calls.length, 1); assert.equal(calls[0].url, `/api/jobs/a/clips/clip01/${kind}`); assert.equal(calls[0].body.expected_revision, 7);
      await work; assert.equal(action(p).attempt.id, "click1");
    });
  }
  await check("cached clip done cannot complete a different pending render attempt", async () => {
    const p = page(); p.open(); setField(p, "hook", "Keep me"); let get = false;
    p.context.fetch = async () => response(get ? {id: "r1", kind: "render", status: "done", revision: 8} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("render")'); get = true; await p.run('pollClipAttempt(clipKey("a","clip01"))');
    assert.notEqual(action(p).status, "done"); assert.equal(p.draft().hook, "Keep me"); assert.equal(p.notices.some(x => /ready/i.test(x.message)), false);
  });
  await check("even a fast terminal POST reply must pass exact attempt polling before success", async () => {
    const p = page(); p.open(); p.context.fetch = async () => response({attempt: {id: "r1", kind: "render", status: "done", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("render")'); assert.notEqual(action(p).status, "done"); assert.equal(p.notices.some(x => /ready|succeeded/i.test(x.message)), false);
  });
  await check("a replayed POST attempt at the existing artifact revision is never treated as a new Apply", async () => {
    const p = page(); p.open(); setField(p, "hook", "New unsaved hook"); const cached = job("a"); cached.clips[0].published_attempt = "old";
    p.context.fetch = async () => response({attempt: {id: "old", kind: "render", status: "done", revision: 7}, job: cached});
    await p.run('submitClipAttempt("render")'); assert.notEqual(action(p).status, "done"); assert.equal(p.draft().dirty, true); assert.equal(p.state.jobs[0].clips[0].hook, "Saved a");
  });
  await check("a different attempt ID is rejected even if its revision matches", async () => {
    const p = page(); p.open(); let get = false;
    p.context.fetch = async () => response(get ? {id: "other", kind: "preview", status: "done", revision: 8, file: "/media/a/other.mp4"} : {attempt: {id: "p1", kind: "preview", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("preview")'); get = true; await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.notEqual(action(p).status, "done");
  });
  await check("Apply requires both exact published attempt ID and artifact revision", async () => {
    const p = page(); p.open(); const published = job("a", 8); published.clips[0].published_attempt = "other";
    p.context.fetch = async url => response(url.includes("/attempts/") ? {id: "r1", kind: "render", status: "done", revision: 8} : url.startsWith("/api/sync") ? {v: "fixture.2.0", jobs: [published], creators: []} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: published});
    await p.run('submitClipAttempt("render")'); await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).status, "publishing");
    published.clips[0].published_attempt = "r1"; await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).status, "done");
  });
  await check("an older completed render settles as superseded instead of polling a newer artifact forever", async () => {
    const p = page(); p.open(); setField(p, "hook", "Keep old draft"); const newer = job("a", 9); newer.clips[0].published_attempt = "newer";
    p.context.fetch = async url => response(url.includes("/attempts/") ? {id: "r1", kind: "render", status: "done", revision: 8} : url.startsWith("/api/sync") ? {v: "fixture.2.0", jobs: [newer], creators: []} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("render")'); await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).status, "superseded"); assert.equal(p.draft().hook, "Keep old draft"); assert.equal(p.draft().dirty, true);
  });
  await check("successful Apply preserves edits made after submission and rebases only revision metadata", async () => {
    const p = page(); p.open(); setField(p, "hook", "Submitted hook");
    p.context.fetch = async url => response(url.includes("/attempts/") ? {id: "r1", kind: "render", status: "done", revision: 8} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("render")'); setField(p, "hook", "Newer dirty hook"); const published = job("a", 8); published.clips[0].published_attempt = "r1"; published.clips[0].hook = "Submitted hook"; p.state.jobs[0] = published;
    await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).status, "done"); assert.equal(p.draft().hook, "Newer dirty hook"); assert.equal(p.draft().dirty, true); assert.equal(p.draft().baseRevision, 8);
  });
  for (const terminal of ["error", "cancelled", "superseded"]) {
    await check(`${terminal} attempt leaves published media and dirty draft untouched`, async () => {
      const p = page(); p.open(); setField(p, "hook", "Keep dirty");
      p.context.fetch = async url => response(url.includes("/attempts/") ? {id: "r1", kind: "render", status: terminal, revision: 8, error: "Fixture failure"} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: job("a")});
      await p.run('submitClipAttempt("render")'); await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).status, terminal); assert.equal(p.state.jobs[0].clips[0].file, "saved.mp4"); assert.equal(p.draft().hook, "Keep dirty"); assert.equal(p.draft().dirty, true);
    });
  }
  await check("terminal cancellation stays busy and polled until physical cleanup finishes", async () => {
    const p = page(); p.open(); let cleanup = true;
    p.context.fetch = async url => response(url.includes("/attempts/") ? {id: "r1", kind: "render", status: "cancelled", revision: 8, cleanup_pending: cleanup} : {attempt: {id: "r1", kind: "render", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("render")'); await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(p.get("#clip_remake_btn").disabled, true);
    cleanup = false; await p.run('pollClipAttempt(clipKey("a","clip01"))'); assert.equal(action(p).attempt.cleanup_pending, false); assert.equal(p.get("#clip_remake_btn").disabled, false);
  });
  await check("poll failure stays reconnecting, not ready, while saved proof and draft remain", async () => {
    const p = page(); p.open(); setField(p, "hook", "Keep me"); const saved = p.get("#saved_clip_video"); let get = false;
    p.context.fetch = async () => { if (get) throw new Error("offline"); return response({attempt: {id: "p1", kind: "preview", status: "queued", revision: 8}, job: job("a")}); };
    await p.run('submitClipAttempt("preview")'); get = true; await p.run('pollClipAttempt(clipKey("a","clip01"))');
    assert.equal(action(p).reconnecting, true); assert.match(p.get("#editor_attempt_status").textContent, /reconnect/i); assert.equal(p.get("#saved_clip_video"), saved); assert.equal(p.draft().hook, "Keep me");
  });
  await check("temporary Preview done uses attempt media and never deletes the draft or publishes", async () => {
    const p = page(); p.open(); setField(p, "hook", "Preview only"); let get = false;
    p.context.fetch = async () => response(get ? {id: "p1", kind: "preview", status: "done", revision: 8, file: "/media/a/temp.mp4", cover: "/media/a/temp.jpg"} : {attempt: {id: "p1", kind: "preview", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("preview")'); get = true; await p.run('pollClipAttempt(clipKey("a","clip01"))');
    assert.equal(action(p).status, "done"); assert.equal(p.draft().hook, "Preview only"); assert.ok(p.draft().dirty); assert.equal(p.state.jobs[0].clips[0].file, "saved.mp4");
    assert.match(p.get("#rendered_preview_video").getAttribute("src"), /\/media\/a\/temp\.mp4/);
  });
  await check("a Preview completed while another modal is open remains available on reopening its own clip", async () => {
    const p = page(); p.open(); setField(p, "hook", "Kept preview draft"); let polling = false;
    p.context.fetch = async () => response(polling ? {id: "p1", kind: "preview", status: "done", revision: 8, file: "temp.mp4", cover: "temp.jpg"} : {attempt: {id: "p1", kind: "preview", status: "queued", revision: 8}, job: job("a")});
    await p.run('submitClipAttempt("preview")'); p.get("#clipdlg").close(); p.open("b");
    const saved = p.get("#saved_clip_video"); polling = true; await p.run('pollClipAttempt(clipKey("a","clip01"))');
    assert.equal(p.get("#saved_clip_video"), saved); assert.equal(p.draft("b").hook, "Saved b");
    p.get("#clipdlg").close(); p.open("a");
    assert.equal(p.draft("a").hook, "Kept preview draft"); assert.equal(p.get("#rendered_pane_btn").hidden, false);
    assert.match(p.get("#rendered_preview_video").getAttribute("src"), /\/media\/a\/temp\.mp4/);
  });
  await check("out-of-order POST responses do not overwrite a newer attempt or job", async () => {
    const p = page(); p.open(); const old = deferred(), recent = deferred(); let calls = 0;
    p.context.fetch = () => (++calls === 1 ? old.promise : recent.promise);
    const a = p.run('submitClipAttempt("preview")'), b = p.run('submitClipAttempt("render")');
    const newerJob = job("a", 9); recent.resolve(response({attempt: {id: "new", kind: "render", status: "queued", revision: 10}, job: newerJob})); await b;
    old.resolve(response({attempt: {id: "old", kind: "preview", status: "queued", revision: 8}, job: job("a")})); await a;
    assert.equal(action(p).attempt.id, "new"); assert.equal(p.state.jobs[0].clips[0].artifact_revision, 9);
  });
  await check("late voice replies cannot write into another job or a newer script generation", async () => {
    const p = page(); p.open(); const r = deferred(); p.context.fetch = () => r.promise;
    const work = p.run('requestVoiceScript("suggest")'); setField(p, "script", "Typed after request", true); p.get("#clipdlg").close(); p.open("b");
    r.resolve(response({script: "Late AI text"})); await work;
    assert.equal(p.draft("a").voScript, "Typed after request"); assert.equal(p.get("#clipdlg [data-vo='script']").value, "Saved narration b");
    assert.equal(p.run('PENDING_VO_ACTIONS[clipKey("b","clip01")]'), undefined);
  });
  await check("reset increments draft generation so older async results remain stale", async () => {
    const p = page(); p.open(); const r = deferred(); p.context.fetch = () => r.promise; const work = p.run('requestVoiceScript("suggest")');
    const generation = p.draft().generation; p.run("resetClipDraft()"); assert.ok(p.draft().generation > generation);
    r.resolve(response({script: "Discarded result"})); await work; assert.equal(p.draft().voScript, "Saved narration a");
  });
  await check("late sample playback failure cannot clear a newer voice sample", async () => {
    const p = page(); p.open(); const a = deferred(), b = deferred(), audios = []; let count = 0;
    p.context.Audio = function() { const audio = {play: () => (++count === 1 ? a.promise : b.promise), pause() {}}; audios.push(audio); return audio; };
    const first = p.run('playVoiceSample("en-US-AndrewNeural",document.querySelector("#vo_test_btn"))');
    const second = p.run('playVoiceSample("en-US-BrianNeural",document.querySelector("#vo_test_btn"))');
    a.reject(new Error("old playback failed")); await first; assert.equal(p.run("_previewAudio"), audios[1]);
    b.resolve(); await second; assert.equal(p.run('editorMedia(clipKey("a","clip01")).voiceSample.audio'), audios[1]);
  });
  await check("sync is single-flight and exposes poll failures instead of swallowing them", async () => {
    const p = page(), r = deferred(); let calls = 0;
    p.context.fetch = () => { calls++; return r.promise; };
    const a = p.run("tick()"), b = p.run("tick()"); assert.equal(calls, 1);
    r.reject(new Error("offline")); await Promise.all([a, b]); assert.match(p.get("#sync_status").textContent, /reconnect/i);
  });
  await check("sync rejects lower revisions and replies from a retired BOOT", async () => {
    const p = page(); const snapshots = ["old.10.0", "new.2.0", "new.1.0", "old.99.0"];
    p.context.fetch = async () => { const v = snapshots.shift(); return response({v, jobs: [job(v)], creators: []}); };
    await p.run("tick()"); await p.run("tick()"); const accepted = p.state.jobs[0].id;
    await p.run("tick()"); assert.equal(p.state.jobs[0].id, accepted); await p.run("tick()"); assert.equal(p.state.jobs[0].id, accepted);
  });
  await check("sync timeout aborts its transport and shows reconnecting", async () => {
    const p = page(); let signal;
    p.context.fetch = (_, opt) => { signal = opt.signal; return new Promise(() => {}); };
    const work = p.run("tick()"); const timeout = [...p.timers].find(([, t]) => t.delay === 15000);
    assert.ok(timeout, "bounded transport timer is installed"); timeout[1].fn(); await work; assert.equal(signal.aborted, true); assert.match(p.get("#sync_status").textContent, /reconnect/i);
  });
  await check("in-flight old sync cannot roll back the immediately consumed POST job", async () => {
    const p = page(); p.open(); const r = deferred(), newer = job("a", 9);
    p.context.fetch = async url => url.startsWith("/api/sync") ? r.promise : response({attempt: {id: "r2", kind: "render", status: "queued", revision: 10}, job: newer});
    const sync = p.run("tick()"); await p.run('submitClipAttempt("render")'); r.resolve(response({v: "old.1.0", jobs: [job("a")], creators: []})); await sync;
    assert.equal(p.state.jobs[0].clips[0].artifact_revision, 9); assert.equal(action(p).attempt.id, "r2"); assert.equal(p.get("#sync_status").textContent, "");
  });
  await check("progress-only ticks preserve keyed job, clip, progress, and focused button nodes", () => {
    const p = page(); p.state.jobs[0].status = "running"; p.state.jobs[0].progress = 0.1; p.state.jobs[0].progress_detail = {id: "work", phase: "encode", phase_attempt: 1, completed: 2, total: 30};
    p.run("drawJobs()"); const section = p.get('section[data-job="a"]'), progress = section.querySelector("[data-job-progress]"), shot = section.querySelector(".shot"); shot.focus(); const writes = section.writes;
    p.state.jobs[0].progress_detail.completed = 5; p.state.jobs[0].progress = 0.5; p.run("drawJobs()");
    assert.equal(p.get('section[data-job="a"]'), section); assert.equal(section.querySelector("[data-job-progress]"), progress); assert.equal(section.querySelector(".shot"), shot); assert.equal(section.writes, writes); assert.equal(p.document.activeElement, shot);
  });
  await check("source work protects delete/select without blocking a separate temporary Preview", () => {
    const p = page(); p.open(); const clip = p.state.jobs[0].clips[0]; clip.active_work = true; clip.source_attempt = {id: "s1", kind: "source", status: "running", revision: 8, progress: {phase: "source", completed: 2, total: 30}};
    p.run("drawJobs(); updateAttemptUI()"); const card = p.get('section[data-job="a"] article');
    assert.equal(card.querySelector('[data-act="delclip"]').disabled, true); assert.equal(p.get("#clip_preview_btn").disabled, false);
  });
  await check("an active editor attempt replaces the job's cached pipeline progress, not its published clip", () => {
    const p = page(), j = p.state.jobs[0]; j.active_work = true; j.outcome = "success"; j.progress_detail = {phase: "Published", completed: 1, total: 1};
    j.clips[0].attempt = {id: "r1", kind: "render", status: "running", revision: 8, progress: {phase: "encode", completed: 2, total: 100}};
    p.run("drawJobs()"); assert.match(p.get('section[data-job="a"] [data-progress-label]').textContent, /Rendering/i); assert.equal(j.clips[0].file, "saved.mp4");
  });
  await check("unknown phases keep percentages and running work cannot reach successful 100", () => {
    const p = page(); const unknown = p.run('progressView({phase:"ai",completed:0,total:null},{status:"running"})'); assert.ok(Number.isFinite(unknown.percent)); assert.match(unknown.label, /highlight/i);
    const running = p.run('progressView({phase:"encode",completed:999,total:1000},{status:"running"})'); assert.ok(running.percent > 0 && running.percent < 100);
    const partial = p.run('progressView({phase:"publish",completed:1,total:1},{status:"done",outcome:"partial",counts:{ready:1,failed:1}})'); assert.notEqual(partial.percent, 100); assert.match(partial.label, /attention/i);
  });
  await check("progress is monotonic for the whole attempt and resets only for a new request", () => {
    const p = page(), el = p.node(); p.document.body.append(el); p.context.progressNode = el;
    p.run('patchProgress(progressNode,{id:"x",phase:"encode",phase_attempt:1,completed:8,total:10},{id:"x",kind:"render",status:"running"})');
    const first = el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow");
    p.run('patchProgress(progressNode,{id:"x",phase:"encode",phase_attempt:1,completed:2,total:10},{id:"x",kind:"render",status:"running"})'); assert.equal(el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow"), first); assert.equal(el.querySelector("[data-progress-meta]"), null);
    p.run('patchProgress(progressNode,{id:"x",phase:"encode",phase_attempt:2,completed:1,total:10},{id:"x",kind:"render",status:"running"})'); assert.equal(el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow"), first);
    p.run('patchProgress(progressNode,{},{id:"new",kind:"render",status:"queued"})'); assert.equal(el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow"), "0");
  });
  await check("cleanup or partial outcomes cannot inherit a previous successful 100% fill", () => {
    const p = page(), el = p.node(); p.document.body.append(el); p.context.progressNode = el;
    p.run('patchProgress(progressNode,{id:"x",phase:"publish",completed:1,total:1},{status:"done",outcome:"success"})');
    p.run('patchProgress(progressNode,{id:"x",phase:"publish",completed:1,total:1},{status:"done",outcome:"success",active_work:1})');
    assert.notEqual(el.querySelector('[role="progressbar"]').getAttribute("aria-valuenow"), "100");
    p.run('patchProgress(progressNode,{id:"x",phase:"publish",completed:1,total:1},{status:"done",outcome:"partial"})'); assert.equal(el.querySelector('[role="progressbar"]').hidden, true);
  });
  await check("editor source uses global offset, stays muted, and rejects non-clean or external URLs", () => {
    const p = page(); assert.equal(p.run('editorSourceTime({offset:100,start:105,end:150},110)'), 10);
    assert.equal(p.run('cleanSourceUrl("a","/editor-media/a/token")'), "/editor-media/a/token");
    for (const url of ["/media/a/saved.mp4", "https://example.com/video.mp4", "/editor-media/b/token", "/editor-media/a/../secret"]) {
      p.context.sourceUrl = url; assert.equal(p.run('cleanSourceUrl("a",sourceUrl)'), "");
    }
    assert.match(script, /\.muted\s*=\s*true/);
  });
  await check("pending source polling rejects a different preparation attempt and keeps saved proof", async () => {
    const p = page(); p.open(); const m = mediaFixture(p), queued = {...m.source, status: "queued", url: null, attempt_id: "s1"}, resolved = m.resolved, catalog = m.catalog;
    m.sourceKey = null; m.source = null;
    p.context.fetch = async (url, opt) => response(url.endsWith("/headline") ? bitmapFixture() : {source: opt.method === "POST" ? queued : {...queued, status: "ready", url: "/editor-media/a/other", attempt_id: "other"}, visual: resolved, catalog, revision: 7});
    await p.run("refreshEditorMedia()"); const poll = [...p.timers.values()].find(t => t.delay === 2000); assert.ok(poll); await poll.fn();
    assert.notEqual(m.source.status, "ready"); assert.equal(p.get("#saved_clip_pane").hidden, false); assert.match(p.get("#editor_source_status").textContent, /reconnect|different|exact/i);
  });
  await check("shared pending sources poll by the clip request without a foreign cancellation ID", async () => {
    const p = page(); p.open(); const m = mediaFixture(p), resolved = m.resolved, catalog = m.catalog;
    const source = {...m.source, status: "queued", url: null, shared: true}; m.sourceKey = null; m.source = null;
    p.context.fetch = async (url, opt) => response(url.endsWith("/headline") ? bitmapFixture() : {source: opt.method === "POST" ? source : {...source, status: "ready", url: "/editor-media/a/shared"}, visual: resolved, catalog, revision: 7});
    await p.run("refreshEditorMedia()"); assert.equal(p.get("#editor_source_cancel").hidden, true);
    const poll = [...p.timers.values()].find(t => t.delay === 2000); assert.ok(poll); await poll.fn();
    assert.equal(m.source.status, "ready"); assert.equal(p.get("#editor_source_video").getAttribute("src"), "/editor-media/a/shared");
  });
  await check("clean legacy SAR preview matches its preserved coded-raster recipe while manual framing stays proportional", () => {
    const p = page(); p.open(); const m = mediaFixture(p); m.source.width = 3840;
    p.run("updateEditorVisual()");
    const scale = parseFloat(p.get("#editor_output").style.width) / 1080;
    assert.ok(Math.abs(parseFloat(p.get("#editor_source_video").style.height) - 608 * scale) < 1e-8);
  });
  await check("source preparation has exact cancellation and an explicit retry without touching the draft", async () => {
    const p = page(); p.open(); setField(p, "hook", "Keep source draft"); const m = mediaFixture(p), s = {id: "s1", kind: "source", status: "queued", revision: 8};
    m.source = {...m.source, status: "queued", url: null, attempt_id: "s1"}; p.state.jobs[0].clips[0].source_attempt = s; p.run("updateEditorVisual()");
    const button = p.get("#editor_source_cancel"); assert.ok(button); assert.equal(button.hidden, false); const calls = [];
    p.context.fetch = async (url, opt) => { calls.push(url); return response(url.endsWith("/cancel") ? {...s, status: "cancelled"} : {source: {...m.source, status: "error", error: "Cancelled"}, visual: m.resolved, catalog: m.catalog, revision: 7}); };
    await p.dispatch("click", button); assert.equal(calls[0], "/api/jobs/a/clips/clip01/attempts/s1/cancel"); assert.ok(p.get("#editor_source_retry")); assert.equal(p.get("#editor_source_retry").hidden, false);
    assert.equal(p.draft().hook, "Keep source draft"); assert.equal(p.state.jobs[0].clips[0].file, "saved.mp4");
  });
  await check("drawer and positioning controls expose labels, inert collapse, keyboard alternatives, and reduced motion", async () => {
    const p = page(); p.open(); const button = p.get("#more_opts_toggle_btn"), panel = p.get("#clip_more_opts_panel");
    assert.equal(button.getAttribute("aria-expanded"), "false"); assert.equal(panel.inert, true);
    await p.dispatch("click", button); assert.equal(button.getAttribute("aria-expanded"), "true"); assert.equal(panel.inert, false);
    const hook = p.get("#clipdlg [data-f='hook']"); assert.ok(p.get(`label[for="${hook.id}"]`));
    assert.ok(p.get("#headline_x")); assert.ok(p.get("#headline_y")); assert.ok(p.get("#crop_x")); assert.ok(p.get("#crop_width"));
    assert.match(script, /setPointerCapture/); assert.match(script, /ArrowLeft/); assert.match(html, /prefers-reduced-motion:\s*reduce/);
    assert.ok(p.get('label[for="vo_script"]')); p.get("#headline_x").focus(); await p.dispatch("click", button); assert.equal(p.document.activeElement, button); assert.equal(panel.inert, true);
  });
  console.log(`${checks - failures}/${checks} checks passed`); process.exitCode = failures ? 1 : 0;
})();
