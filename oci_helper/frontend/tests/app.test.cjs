"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// A small DOM fixture keeps these request and state regressions dependency-free.
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = "";
    this.text = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    this.elements = { namedItem: (name) => this.descendants().find((item) => item.name === name) };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((item) => item.textContent ?? item).join(""); }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  addEventListener(event, listener) { (this.listeners[event] ||= []).push(listener); }
  descendants() { return this.children.flatMap((item) => item instanceof Element ? [item, ...item.descendants()] : []); }
  querySelector(selector) {
    if (selector.startsWith(".")) return this.descendants().find((item) => item.className?.split(" ").includes(selector.slice(1)));
    return this.descendants().find((item) => item.tagName === selector.split("[")[0]);
  }
  reportValidity() { return true; }
  click() { this.clicked = true; }
  remove() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  reset() { for (const item of this.descendants()) if (item.name) item.value = ""; }
}

function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
}

function app(fetch) {
  const ids = new Map();
  const document = {
    querySelector: (selector) => {
      if (!ids.has(selector)) ids.set(selector, new Element("div"));
      return ids.get(selector);
    },
    querySelectorAll: () => [],
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => { const item = new Element("#text"); item.textContent = text; return item; },
    documentElement: new Element("html"),
    body: new Element("body"),
  };
  const context = vm.createContext({
    document, Node: Element, Headers, fetch, sessionStorage: storage(), localStorage: storage(),
    window: {
      OCIHelperI18n: { t: (message, values = {}) => String(message).replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`) },
      addEventListener() {},
    },
    FormData: class {
      constructor(form) { this.values = new Map(form?.descendants().filter((item) => item.name && !item.disabled).map((item) => [item.name, item.value])); }
      get(name) { return this.values.get(name) ?? null; }
      set(name, value) { this.values.set(name, value); }
    },
    setTimeout: () => 1, clearTimeout() {},
    URL: { createObjectURL: () => "blob:test-download", revokeObjectURL() {} },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app.js"), "utf8"), context);
  document.querySelector("#modal-form").append(document.querySelector("#modal-body"));
  return { run: (code) => vm.runInContext(code, context), content: document.querySelector("#content"), body: document.body, ids };
}

function response(data, status = 200) {
  return { ok: status < 400, status, json: async () => ({ success: status < 400, data, msg: status === 401 ? "登录已过期" : "OK" }) };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test("a slow earlier navigation cannot replace the current page", async () => {
  const overview = deferred();
  const tasks = deferred();
  const ui = app((url) => url.endsWith("/glance") ? overview.promise : tasks.promise);
  const oldNavigation = ui.run('navigate("overview")');
  const currentNavigation = ui.run('navigate("tasks")');
  tasks.resolve(response({ records: [], total: 0, current: 1, size: 100 }));
  await currentNavigation;
  overview.resolve(response({ users: 7, tasks: 2, regions: 1, days: 3 }));
  await oldNavigation;
  assert.equal(ui.run("state.view"), "tasks");
  assert.match(ui.content.textContent, /任务列表/);
  assert.doesNotMatch(ui.content.textContent, /使用提示/);
});

test("a stale request failure cannot replace the current navigation error", async () => {
  const overview = deferred();
  const ui = app((url) => url.endsWith("/glance") ? overview.promise : Promise.reject(new Error("tasks unavailable")));
  const oldNavigation = ui.run('navigate("overview")');
  await ui.run('navigate("tasks")');
  overview.reject(new Error("old overview failure"));
  await oldNavigation;
  assert.equal(ui.content.textContent, "tasks unavailable");
});

test("an old 401 received during JSON parsing does not log out a new session", async () => {
  const payload = deferred();
  const ui = app(async () => ({ ok: false, status: 401, json: () => payload.promise }));
  ui.run('state.token = "old-token"');
  const oldRequest = ui.run('api("/sys/glance")');
  const rejected = assert.rejects(oldRequest, { name: "Error", message: "" });
  await new Promise((resolve) => setImmediate(resolve));
  ui.run('state.sessionVersion += 1; state.token = "new-token"');
  payload.resolve({ success: false, msg: "登录已过期" });
  await rejected;
  assert.equal(ui.run("state.token"), "new-token");
});

test("a current unauthorized response logs out and cancels an open form", async () => {
  const ui = app(async () => response(null, 401));
  ui.run('state.token = "token"; state.users = [{id: "user"}]');
  const form = ui.run('openForm("Confirmation", [])');
  await assert.rejects(ui.run('api("/sys/glance")'), /登录已过期/);
  assert.equal(await form, null);
  assert.equal(ui.run("state.token"), "");
  assert.equal(ui.run("state.users.length"), 0);
  assert.equal(ui.ids.get("#modal").open, false);
});

test("a closed task form cannot change a newer modal when shape options resolve", async () => {
  const delayedImages = deferred();
  const delayedDomains = deferred();
  const ui = app(async (url, options) => {
    if (url.endsWith("/sshKey/list")) return response([]);
    const architecture = JSON.parse(options.body).architecture;
    if (architecture === "AMD") return url.endsWith("/imageOptions") ? delayedImages.promise : delayedDomains.promise;
    return response(url.endsWith("/imageOptions")
      ? [{ operatingSystem: "Ubuntu", operatingSystemVersion: "24.04", label: "Ubuntu" }]
      : [{ name: "AD-1", label: "AD-1" }]);
  });
  const task = ui.run('createTask({id: "user", username: "User"})');
  await new Promise((resolve) => setImmediate(resolve));
  const architecture = ui.ids.get("#modal-form").elements.namedItem("architecture");
  const oldImages = ui.ids.get("#modal-form").elements.namedItem("imageSelection");
  // Keep this true to verify lifecycle identity independently of DOM attachment.
  oldImages.isConnected = true;
  architecture.value = "AMD";
  const changed = architecture.listeners.change[0]();
  const newerForm = ui.run('openForm("New form", [])');
  await task;
  const submit = ui.ids.get("#modal-submit");
  submit.disabled = true;
  delayedImages.resolve(response([{ operatingSystem: "Oracle Linux", operatingSystemVersion: "9", label: "Oracle Linux" }]));
  delayedDomains.resolve(response([{ name: "AD-2", label: "AD-2" }]));
  await changed;
  assert.equal(ui.ids.get("#modal-title").textContent, "New form");
  assert.equal(submit.disabled, true);
  assert.doesNotMatch(oldImages.textContent, /Oracle Linux/);
  ui.ids.get("#modal-cancel").onclick();
  assert.equal(await newerForm, null);
});

test("an authorized key download continues across navigation and a new modal", async () => {
  const blob = deferred();
  const ui = app(async () => ({
    ok: true, status: 200,
    headers: new Headers({ "Content-Type": "application/zip" }),
    blob: () => blob.promise,
  }));
  const download = ui.run('downloadApi("/sshKey/generate", {method: "POST"}, "key.zip")');
  ui.run("beginRender()");
  const form = ui.run('openForm("New form", [])');
  blob.resolve({ size: 32 });
  await download;
  assert.equal(ui.body.children[0].download, "key.zip");
  assert.equal(ui.body.children[0].clicked, true);
  assert.equal(ui.ids.get("#modal-title").textContent, "New form");
  ui.ids.get("#modal-cancel").onclick();
  await form;
});

test("resource configuration selection includes users beyond the first 100", async () => {
  const requests = [];
  const ui = app(async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push(body.currentPage);
    const start = (body.currentPage - 1) * 100;
    const records = Array.from({ length: body.currentPage === 1 ? 100 : 1 }, (_, index) => ({ id: `user-${start + index}`, username: `Config ${start + index}`, region: "region" }));
    return response({ records, current: body.currentPage, total: 101, size: 100 });
  });
  await ui.run("renderResources()");
  assert.deepEqual(requests, [1, 2]);
  assert.equal(ui.run("state.users.length"), 101);
  assert.match(ui.content.textContent, /Config 100/);
});

for (const [renderer, endpoint] of [
  ["renderConfigs(2)", "/oci/userPage"],
  ["renderTasks(2)", "/oci/createTaskPage"],
  ['renderVcns({id: "user"}, 2, false)', "/vcn/page"],
  ['renderSecurityRules({id: "user"}, {id: "vcn", displayName: "VCN"}, 0, 2, false)', "/securityRule/page"],
  ['renderBootVolumes({id: "user"}, 2, false)', "/bootVolume/page"],
]) {
  test(`${renderer} loads the requested page and displays its bounds`, async () => {
    const ui = app(async (url, options) => {
      assert.equal(url, `/api${endpoint}`);
      const body = JSON.parse(options.body);
      assert.equal(body.currentPage, 2);
      assert.equal(body.pageSize, 100);
      if ("cleanReLaunch" in body) assert.equal(body.cleanReLaunch, false);
      return response({ records: [], total: 150, current: 2, size: 100 });
    });
    await ui.run(renderer);
    assert.match(ui.content.textContent, /共 150 条 · 第 2 \/ 2 页/);
    const pager = ui.content.querySelector(".pagination");
    assert.equal(pager.children[1].disabled, false);
    assert.equal(pager.children[2].disabled, true);
  });
}

test("a page removed by a concurrent deletion falls back to the final page", async () => {
  const requests = [];
  const ui = app(async (_url, options) => {
    const body = JSON.parse(options.body);
    requests.push(body.currentPage);
    return response({ records: [], total: 90, current: body.currentPage, size: 100 });
  });
  await ui.run("renderTasks(2)");
  assert.deepEqual(requests, [2, 1]);
  assert.match(ui.content.textContent, /第 1 \/ 1 页/);
});

test("saving Telegram settings preserves the chat and never silently clears both fields", async () => {
  const saved = [];
  const ui = app(async (url, options) => {
    if (url.endsWith("/getSysCfg")) return response({ adminAccount: "admin", tgBotConfigured: false, tgChatId: "" });
    saved.push(JSON.parse(options.body));
    return response(null);
  });
  await ui.run("renderSettings()");
  const forms = ui.content.descendants().filter((item) => item.tagName === "form");
  const telegram = forms[1];
  telegram.elements.namedItem("token").value = "bot-token";
  telegram.elements.namedItem("chat").value = "chat-id";
  const event = { preventDefault() {}, submitter: new Element("button") };
  await telegram.listeners.submit[0](event);
  assert.equal(telegram.elements.namedItem("token").value, "");
  assert.equal(telegram.elements.namedItem("token").placeholder, "已配置；修改时请重新输入");
  assert.equal(telegram.elements.namedItem("chat").value, "chat-id");
  await telegram.listeners.submit[0](event);
  assert.deepEqual(saved[1], { tgBotToken: null, tgChatId: "chat-id" });
});
