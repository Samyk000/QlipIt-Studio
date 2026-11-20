/* Offline UI regressions. Run with node tests/test_homepage.js; no server or user data is used. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const boot = script.lastIndexOf("\n(async () => {");
assert.ok(boot > 0, "the network bootstrap must be excluded from offline tests");

function page(width = 1440, savedMini = "1") {
  const listeners = new Map(), nodes = new Map(), stored = new Map([["mini", savedMini]]);
  const document = {
    hidden: false,
    querySelector: selector => nodes.get(selector) || null,
    querySelectorAll: () => [],
    addEventListener: (name, fn) => {
      const key = "document:" + name;
      listeners.set(key, [...(listeners.get(key) || []), fn]);
    },
  };
  function node(dataset = {}) {
    const classes = new Set();
    const el = {
      dataset, tagName: "BUTTON", hidden: true, innerHTML: "", style: {},
      classList: {
        contains: name => classes.has(name),
        add: name => classes.add(name),
        remove: name => classes.delete(name),
        toggle: (name, force) => {
          const on = force ?? !classes.has(name);
          if (on) classes.add(name); else classes.delete(name);
          return on;
        },
      },
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      setAttribute: () => {},
      contains: other => {
        for (let current = other; current; current = current.parentElement) if (current === el) return true;
        return false;
      },
      closest: selector => {
        for (let current = el; current; current = current.parentElement) {
          if (selector === "[data-act]" && current.dataset.act) return current;
          if (selector === ".ai-warning" && current.classList.contains("ai-warning")) return current;
        }
        return null;
      },
      scrollTo: () => {},
      focus: () => { document.activeElement = el; },
    };
    return el;
  }
  for (const id of ["app", "main", "clipdlg", "file", "bellpop", "bellBtn", "bellDot",
                    "nav", "foot", "topbarAiPill", "tpic", "utabs", "title", "subtitle", "actions",
                    "view", "ov", "ov-hero", "ov-dash", "usage", "jobs", "selbar", "vbanner"]) nodes.set("#" + id, node());
  nodes.set(".topbar", node());
  const window = {addEventListener: (name, fn) => {
    const key = "window:" + name;
    listeners.set(key, [...(listeners.get(key) || []), fn]);
  }};
  const context = vm.createContext({
    document, window, innerWidth: width,
    addEventListener: window.addEventListener,
    localStorage: {
      getItem: key => stored.get(key) || null,
      setItem: (key, value) => stored.set(key, String(value)),
    },
    requestAnimationFrame: fn => fn(), setTimeout: () => 1, clearTimeout: () => {},
    fetch: () => { throw new Error("Unexpected API call in an offline test"); },
    location: {pathname: "/app", hostname: "127.0.0.1"},
  });
  context.history = {pushState: (_, __, url) => { context.location.pathname = url; },
                     replaceState: (_, __, url) => { context.location.pathname = url; }};
  // Execute the actual page, including startup preferences, but not its API bootstrap.
  vm.runInContext(script.slice(0, boot), context);
  return {
    context, nodes, stored, node,
    state: vm.runInContext("S", context),
    run: code => vm.runInContext(code, context),
    dispatch: (name, event) => Promise.all((listeners.get(name) || []).map(fn => fn(event))),
  };
}

const quota = "Gemini says the key has hit its limit or has no credit left. Check the account with Gemini, or try again later.";
const unavailable = 'Gemini does not know the model "gemini-old". Find models & pick another <model>.';
function warningPage(problem = quota) {
  const p = page();
  Object.assign(p.state.status, {
    accounts: false, user: {is_admin: true, role: "admin", verified: true},
    ai: {ready: true, provider: "Gemini", model: "gemini-old", problem, today_calls: 0, today_tokens: 0},
  });
  p.run("drawShell()");
  return p;
}
const banner = p => p.nodes.get("#vbanner").innerHTML;
const hasAIWarning = p => banner(p).includes('class="vb ai-warning"');
async function dismiss(p) {
  assert.ok(hasAIWarning(p), "an AI warning must be present before dismissal");
  const button = p.node({act: "dismissai"}), warning = p.node();
  warning.classList.add("ai-warning");
  if (hasAIWarning(p)) { warning.parentElement = p.nodes.get("#vbanner"); button.parentElement = warning; }
  await p.dispatch("document:click", {target: button});
}

let failures = 0, checks = 0;
async function check(name, test) {
  checks++;
  try { await test(); console.log("PASS " + name); }
  catch (error) { failures++; console.error("FAIL " + name + ": " + error.message); }
}

(async () => {
  await check("all inline JavaScript parses", () => { new vm.Script(script); });
  for (const saved of ["1", "0", "true", ""]) {
    await check(`desktop ignores the old mini preference (${JSON.stringify(saved)})`, () => {
      const p = page(1440, saved);
      assert.equal(p.nodes.get("#app").classList.contains("mini"), false);
      assert.equal(p.stored.get("mini"), saved, "the old preference is not rewritten");
    });
  }
  await check("crossing from the mobile breakpoint to desktop expands the sidebar", async () => {
    const p = page(900);
    assert.equal(p.nodes.get("#app").classList.contains("mini"), true);
    p.context.innerWidth = 901;
    await p.dispatch("window:resize");
    assert.equal(p.nodes.get("#app").classList.contains("mini"), false);
    assert.equal(p.stored.get("mini"), "1");
  });
  await check("desktop cannot be collapsed through a stale action", async () => {
    const p = page(1440, "");
    await p.dispatch("document:click", {target: p.node({act: "collapse"})});
    assert.equal(p.nodes.get("#app").classList.contains("mini"), false);
    assert.equal(p.stored.get("mini"), "");
  });
  await check("the mobile navigation drawer still opens and closes", async () => {
    const p = page(390);
    const event = {target: p.node({act: "menu"})};
    await p.dispatch("document:click", event);
    assert.equal(p.nodes.get("#app").classList.contains("open"), true);
    await p.dispatch("document:click", event);
    assert.equal(p.nodes.get("#app").classList.contains("open"), false);
  });
  await check("Homepage omits the summary strip but keeps its dashboard and chart total", () => {
    const p = page();
    p.state.creators = [{id: "creator-1", name: "Example", auto_watch: true}];
    p.state.overview = {
      recording: 1,
      creators: [{id: "creator-1", name: "Example", state: "recording", clips_today: 2, clips_total: 9}],
      daily: [{day: "2026-11-18", clips: 3}, {day: "2026-11-19", clips: 4}],
    };
    const hero = p.nodes.get("#ov-hero");
    hero.innerHTML = "existing composer";
    p.run("drawOverview()");
    const dashboard = p.nodes.get("#ov-dash").innerHTML;
    assert.equal(dashboard.includes('class="metrics-strip"'), false, "Homepage has no summary strip");
    for (const text of ["Clip activity", "Creators Fleet", "AI INTELLIGENCE", "NEURAL SPEECH ENGINE", "Next steps"])
      assert.ok(dashboard.includes(text), text + " stays on Homepage");
    assert.match(dashboard, /<b>7<\/b><span>Clips made<\/span>/);
    assert.match(dashboard, /Clips made per day, last 14 days/);
    assert.equal(hero.innerHTML, "existing composer");
  });
  await check("Usage keeps its shared metrics strip", () => {
    const p = page();
    p.state.usage = {ready: true, today_calls: 3, today_tokens: 1200, calls_total: 8, tokens_total: 2500,
                     daily: [{day: "2026-11-19", tokens: 1200, calls: 3}]};
    p.run("drawUsage()");
    const usage = p.nodes.get("#usage").innerHTML;
    assert.match(usage, /class="metrics-strip"/);
    for (const text of ["Calls today", "Tokens today", "Calls total", "Tokens total"])
      assert.ok(usage.includes(text), text + " stays on Usage");
  });
  await check("sidebar uses the full logo at 80px with STUDIO, without duplicate QlipIt text", () => {
    const brand = html.match(/<a href="\/" class="brand-link"[\s\S]*?<\/a>/)[0];
    assert.equal(brand.includes('class="brand-name"'), false, "the image already contains the wordmark");
    assert.ok(brand.includes('class="brand-badge">STUDIO</span>'));
    assert.match(html, /\.brand-icon\{[^}]*width:80px;height:80px;[^}]*object-fit:contain/);
  });
  await check("Home and All videos use the 80px logo only in their main empty states", () => {
    const p = page();
    p.state.overview = {creators: [], daily: []};
    p.run("drawOverview()");
    assert.ok(p.nodes.get("#ov-dash").innerHTML.includes('class="empty brand-empty"'), "Home logo is sized for its empty state");
    p.state.view = "library";
    p.run("drawJobs()");
    assert.ok(p.nodes.get("#jobs").innerHTML.includes('class="empty brand-empty"'), "All videos logo is sized for its empty state");
    assert.match(html, /\.empty\.brand-empty img\{width:80px;height:80px;object-fit:contain/);
    p.state.view = "c:example";
    p.run("drawJobs()");
    assert.equal(p.nodes.get("#jobs").innerHTML.includes("brand-empty"), false, "creator empty states are not restyled");
  });
  for (const problem of [quota, unavailable]) {
    await check(`${problem === quota ? "credit-limit" : "unavailable-model"} warning is compact, escaped, and accessible`, () => {
      const p = warningPage(problem);
      assert.ok(hasAIWarning(p), "the AI-only warning is rendered");
      assert.match(banner(p), /<b>AI unavailable\.<\/b> Clips use loudness\./);
      assert.match(banner(p), /data-act="go" data-v="settings" data-tab="ai">AI settings<\/button>/);
      assert.match(banner(p), /type="button"[^>]*data-act="dismissai"[^>]*aria-label="Dismiss AI warning"/);
      const escaped = p.run("esc(S.status.ai.problem)");
      assert.ok(banner(p).includes(`title="${escaped}"`), "the complete escaped reason is available as a tooltip");
      assert.equal(banner(p).replace(/title="[^"]*"/g, "").includes(escaped), false, "the reason is not an inline paragraph");
    });
  }
  await check("dismissal is page-memory only and restores focus to the persistent AI action", async () => {
    const p = warningPage(), stored = [...p.stored];
    await dismiss(p);
    assert.equal(hasAIWarning(p), false);
    assert.deepEqual([...p.stored], stored);
    assert.equal(p.state.status.ai.problem, quota, "dismissal does not change AI health or fallback");
    assert.ok(p.nodes.get("#topbarAiPill") === p.context.document.activeElement, "focus returns to AI settings");
  });
  await check("actual sync polling preserves dismissal across AI object replacement and usage changes", async () => {
    const p = warningPage();
    await dismiss(p);
    p.state.view = "library";
    p.context.fetch = async url => {
      assert.ok(url.startsWith("/api/sync?"));
      return {ok: true, status: 200, json: async () => ({v: "fixture-2", jobs: [], creators: [], unread: 0,
        ai: {...p.state.status.ai, today_calls: 99}})};
    };
    await p.run("tick()");
    assert.equal(p.state.status.ai.today_calls, 99, "the full sync was applied");
    assert.equal(hasAIWarning(p), false);
  });
  await check("SPA navigation and full status replacement keep the same issue dismissed", async () => {
    const p = warningPage();
    await dismiss(p);
    p.run('go("library")');
    assert.equal(p.context.location.pathname, "/app/videos");
    assert.equal(hasAIWarning(p), false);
    p.state.status = {...p.state.status, ai: {...p.state.status.ai}};
    for (const view of ["overview", "usage", "library"]) {
      p.state.view = view; p.run("drawShell()");
      assert.equal(hasAIWarning(p), false, view + " keeps the issue hidden");
    }
  });
  await check("a full page refresh shows a previously dismissed issue again", async () => {
    const old = warningPage(); await dismiss(old);
    assert.ok(hasAIWarning(warningPage()));
  });
  await check("Usage responses also observe recovery before the same issue fails again", async () => {
    const p = warningPage(); await dismiss(p);
    p.state.view = "usage";
    let problem = "";
    p.context.fetch = async url => {
      assert.equal(url, "/api/usage");
      return {ok: true, status: 200, json: async () => ({...p.state.status.ai, problem})};
    };
    await p.run("loadUsage()");
    assert.equal(p.state.status.ai.problem, "", "the existing usage payload reports recovery");
    problem = quota;
    await p.run("loadUsage()");
    assert.ok(hasAIWarning(p), "a new failure after observed recovery is visible");
  });
  await check("the AI settings action selects the existing AI tab without testing the provider", async () => {
    const p = warningPage();
    p.run("drawSettings = async () => {}");
    await p.dispatch("document:click", {target: p.node({act: "go", v: "settings", tab: "ai"})});
    assert.equal(p.state.view, "settings");
    assert.equal(p.state.stab, "ai");
    assert.equal(p.context.location.pathname, "/app/settings/ai");
    assert.equal(hasAIWarning(p), false);
  });
  for (const field of ["problem", "provider", "model"]) {
    await check(`a changed AI ${field} is not hidden by the previous dismissal`, async () => {
      const p = warningPage(); await dismiss(p);
      p.state.status.ai[field] = field === "problem" ? unavailable : "different-" + field;
      p.run("drawShell()");
      assert.ok(hasAIWarning(p));
    });
  }
  for (const hiddenBy of ["none", "settings", "disk", "ffmpeg"]) {
    await check(`an observed recovery resets dismissal even while hidden by ${hiddenBy}`, async () => {
      const p = warningPage(); await dismiss(p);
      p.state.view = hiddenBy === "settings" ? "settings" : "overview";
      p.state.status.disk_full = hiddenBy === "disk";
      p.state.status.no_ffmpeg = hiddenBy === "ffmpeg";
      p.state.status.ai.problem = ""; p.run("drawShell()");
      p.state.status.ai.problem = quota; p.run("drawShell()");
      p.state.view = "overview"; p.state.status.disk_full = false; p.state.status.no_ffmpeg = false;
      p.run("drawShell()");
      assert.ok(hasAIWarning(p));
    });
  }
  await check("AI settings action keeps its admin boundary and warning priority", () => {
    const p = warningPage();
    p.state.status.update = {latest: "fixture", notes: []};
    p.state.status.user.verified = false;
    p.run("drawShell()");
    assert.ok(hasAIWarning(p), "AI warning precedes update/account banners");
    p.state.status.disk_full = true; p.run("drawShell()");
    assert.equal(hasAIWarning(p), false);
    assert.ok(banner(p).includes("almost full"));
    p.state.status.no_ffmpeg = true; p.run("drawShell()");
    assert.ok(banner(p).includes("ffmpeg"));
    p.state.status.no_ffmpeg = false; p.state.status.disk_full = false; p.state.status.update = null;
    p.state.status.user.is_admin = false; p.run("drawShell()");
    assert.equal(hasAIWarning(p), false);
    assert.ok(banner(p).includes("Confirm your email"));
    p.state.status.user.is_admin = true; p.state.view = "settings"; p.run("drawShell()");
    assert.equal(hasAIWarning(p), false, "the issue is not repeated on Settings");
  });
  for (const kind of ["disk", "ffmpeg", "account", "setup"]) {
    await check(`the AI close action cannot dismiss a ${kind} warning`, async () => {
      const p = warningPage("");
      p.state.status.disk_full = kind === "disk";
      p.state.status.no_ffmpeg = kind === "ffmpeg";
      p.state.status.user.verified = kind !== "account";
      p.state.status.ai.ready = kind !== "setup";
      p.run("drawShell()");
      const before = banner(p);
      assert.ok(before);
      const forged = p.node({act: "dismissai"});
      forged.parentElement = p.nodes.get("#vbanner");
      await p.dispatch("document:click", {target: forged});
      assert.equal(banner(p), before);
      assert.equal(hasAIWarning(p), false);
    });
  }
  console.log(`${checks - failures}/${checks} checks passed`);
  process.exitCode = failures ? 1 : 0;
})();
