"use strict";

const { t } = window.OCIHelperI18n;

const state = {
  token: sessionStorage.getItem("oci-helper-token") || "",
  view: "overview",
  users: [],
  sshKeys: [],
  selectedUser: null,
  selectedVcn: null,
};

const titles = {
  overview: "概览",
  configs: "OCI 配置",
  sshKeys: "SSH 公钥",
  tasks: "任务列表",
  logs: "执行日志",
  resources: "网络与存储",
  settings: "系统设置",
};

const descriptions = {
  overview: "查看 OCI 配置、任务和区域的运行概况。",
  configs: "管理 OCI API 凭据、实例与创建策略。",
  sshKeys: "创建、添加和命名开机任务使用的 SSH 公钥。",
  tasks: "跟踪实例创建任务的状态、重试与错误。",
  logs: "查看最新的任务调度、执行结果与错误日志。",
  resources: "集中管理网络、安全规则、引导卷和服务限额。",
  settings: "管理管理员账号、登录密码与通知渠道。",
};

const $ = (selector, root = document) => root.querySelector(selector);
const content = $("#content");
const loginView = $("#login-view");
const shell = $("#shell");
const modal = $("#modal");
const modalForm = $("#modal-form");

function node(tag, options = {}, ...children) {
  const item = document.createElement(tag);
  if (options.className) item.className = options.className;
  if (options.text !== undefined) item.textContent = String(options.text);
  if (options.type) item.type = options.type;
  if (options.title) item.title = options.title;
  for (const [name, value] of Object.entries(options.attrs || {})) {
    if (value !== undefined && value !== null) item.setAttribute(name, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    item.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return item;
}

function setAppVersion(version) {
  const normalizedVersion = String(version || "").trim().replace(/^v/i, "");
  if (normalizedVersion) $("#app-version").textContent = `v${normalizedVersion}`;
}

function setButtonLoading(item, isLoading, loadingText = "处理中…") {
  if (isLoading) {
    item.dataset.originalText = item.textContent;
    item.classList.add("is-loading");
    item.setAttribute("aria-busy", "true");
    item.replaceChildren(
      node("span", { className: "button-spinner", attrs: { "aria-hidden": "true" } }),
      node("span", { text: t(loadingText) }),
    );
    return;
  }
  item.textContent = item.dataset.originalText || "";
  delete item.dataset.originalText;
  item.classList.remove("is-loading");
  item.removeAttribute("aria-busy");
}

function button(text, onClick, kind = "small", loadingText = "") {
  const item = node("button", { className: kind, text: t(text), type: "button" });
  item.addEventListener("click", async () => {
    item.disabled = true;
    if (loadingText) setButtonLoading(item, true, loadingText);
    try {
      await onClick();
    } catch (error) {
      toast(error.message || String(error), true);
    } finally {
      if (loadingText) setButtonLoading(item, false);
      item.disabled = false;
    }
  });
  return item;
}

function badge(value) {
  const text = value || "UNKNOWN";
  let style = "badge";
  if (["FAILED", "TERMINATED", "cancelled", "failed"].includes(text)) style += " danger";
  else if (!["RUNNING", "AVAILABLE", "succeeded"].includes(text)) style += " warning";
  return node("span", { className: style, text: t(text) });
}

function actionGroup(...items) {
  return node("div", { className: "actions" }, items);
}

function panel(title, actions = null) {
  const root = node("section", { className: "panel" });
  root.append(node("div", { className: "panel-header" }, node("h3", { text: t(title) }), actions));
  return root;
}

function table(headers, rows) {
  if (!rows.length) return node("div", { className: "empty", text: t("暂无数据") });
  const head = node("thead", {}, node("tr", {}, headers.map((item) => node("th", { text: t(item) }))));
  const body = node("tbody");
  for (const row of rows) {
    const tr = node("tr");
    for (const value of row) {
      const td = node("td");
      if (Array.isArray(value)) td.append(...value);
      else td.append(value instanceof Node ? value : document.createTextNode(value ?? "—"));
      tr.append(td);
    }
    body.append(tr);
  }
  return node("div", { className: "table-wrap" }, node("table", {}, head, body));
}

function toast(message, isError = false) {
  const item = $("#toast");
  item.textContent = t(message);
  item.className = `toast${isError ? " error" : ""}`;
  item.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { item.hidden = true; }, 4200);
}

async function requestApi(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  let body = options.body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  return fetch(`/api${path}`, { ...options, headers, body });
}

async function readApiPayload(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(t("服务器返回了无法解析的响应 ({status})", { status: response.status }));
  }
  if (response.status === 401) {
    logout();
    throw new Error(t(payload.msg || "登录已过期"));
  }
  if (!response.ok || payload.success === false) {
    const detail = Array.isArray(payload.data)
      ? t("：{details}", { details: payload.data.map((item) => `${item.field} ${item.message}`).join(t("；")) })
      : "";
    throw new Error(`${t(payload.msg || "请求失败 ({status})", { status: response.status })}${detail}`);
  }
  return payload;
}

async function api(path, options = {}) {
  const response = await requestApi(path, options);
  return (await readApiPayload(response)).data;
}

async function downloadApi(path, options = {}, fallbackFilename = "download") {
  const response = await requestApi(path, options);
  const contentType = response.headers.get("Content-Type") || "";
  if (!response.ok || contentType.includes("application/json")) {
    await readApiPayload(response);
    throw new Error(t("服务器未返回下载文件"));
  }

  const blob = await response.blob();
  if (!blob.size) throw new Error(t("下载文件为空"));
  const disposition = response.headers.get("Content-Disposition") || "";
  const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
  const filename = filenameMatch?.[1] || fallbackFilename;
  const downloadUrl = URL.createObjectURL(blob);
  const link = node("a", {
    attrs: { href: downloadUrl, download: filename, hidden: "" },
  });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
}

function setAuthenticated(authenticated) {
  loginView.hidden = authenticated;
  shell.hidden = !authenticated;
}

function logout() {
  state.token = "";
  sessionStorage.removeItem("oci-helper-token");
  setAuthenticated(false);
}

async function navigate(view) {
  state.view = view;
  state.selectedUser = null;
  state.selectedVcn = null;
  $("#page-title").textContent = t(titles[view]);
  $("#page-description").textContent = t(descriptions[view]);
  document.querySelectorAll("#navigation button").forEach((item) => {
    const isActive = item.dataset.view === view;
    item.classList.toggle("active", isActive);
    if (isActive) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  content.replaceChildren(node("div", { className: "panel empty", text: t("正在加载…") }));
  try {
    if (view === "overview") await renderOverview();
    if (view === "configs") await renderConfigs();
    if (view === "sshKeys") await renderSshKeys();
    if (view === "tasks") await renderTasks();
    if (view === "logs") await renderTaskLogs();
    if (view === "resources") await renderResources();
    if (view === "settings") await renderSettings();
  } catch (error) {
    content.replaceChildren(node("div", { className: "panel empty", text: t(error.message) }));
    toast(error.message, true);
  }
}

function replaceSelectOptions(select, options, emptyLabel = "暂无可用选项") {
  select.replaceChildren();
  if (!options.length) {
    select.append(node("option", {
      text: t(emptyLabel),
      attrs: { value: "", disabled: "", selected: "" },
    }));
    return;
  }
  for (const option of options) {
    const value = typeof option === "string" ? option : option.value;
    const text = typeof option === "string" ? option : option.label;
    select.append(node("option", { text: t(text), attrs: { value } }));
  }
}

function openForm(title, fields, submitText = "确认", description = "") {
  return new Promise((resolve) => {
    let resolved = false;
    $("#modal-title").textContent = t(title);
    $("#modal-submit").textContent = t(submitText);
    $("#modal-submit").disabled = false;
    $("#modal-error").textContent = "";
    const body = $("#modal-body");
    body.replaceChildren();
    if (description) body.append(node("p", { className: "muted", text: t(description) }));
    for (const field of fields) {
      const label = node("label", { className: "field" }, node("span", { text: t(field.label) }));
      let input;
      if (field.type === "textarea") input = node("textarea");
      else if (field.type === "select") {
        input = node("select");
        replaceSelectOptions(input, field.options || [], field.emptyLabel);
      } else if (field.type === "checkbox") {
        input = node("input", { type: "checkbox" });
        input.classList.add("checkbox");
      } else input = node("input", { type: field.type || "text" });
      input.name = field.name;
      if (field.value !== undefined && field.type !== "file") {
        if (field.type === "checkbox") input.checked = Boolean(field.value);
        else input.value = field.value;
      }
      if (field.required) input.required = true;
      if (field.disabled) input.disabled = true;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      if (field.step !== undefined) input.step = field.step;
      if (field.pattern) input.pattern = field.pattern;
      if (field.maxLength !== undefined) input.maxLength = field.maxLength;
      if (field.placeholder) input.placeholder = t(field.placeholder);
      if (field.onChange) {
        input.addEventListener("change", async () => {
          $("#modal-error").textContent = "";
          try {
            await field.onChange(input.value, modalForm);
          } catch (error) {
            $("#modal-error").textContent = t(error.message || String(error));
          }
        });
      }
      label.append(input);
      if (field.help) label.append(node("small", { className: "muted", text: t(field.help) }));
      if (field.hidden) label.hidden = true;
      body.append(label);
    }

    const finish = (value) => {
      if (resolved) return;
      resolved = true;
      modal.close();
      body.replaceChildren();
      resolve(value);
    };
    $("#modal-close").onclick = () => finish(null);
    $("#modal-cancel").onclick = () => finish(null);
    modalForm.onsubmit = (event) => {
      event.preventDefault();
      if (!modalForm.reportValidity()) return;
      const data = {};
      const formData = new FormData(modalForm);
      for (const field of fields) {
        const input = modalForm.elements.namedItem(field.name);
        data[field.name] = field.type === "checkbox" ? input.checked : formData.get(field.name);
      }
      finish(data);
    };
    modal.oncancel = (event) => { event.preventDefault(); finish(null); };
    modal.showModal();
  });
}

async function confirmAction(title, message, confirmation = "确认") {
  const result = await openForm(title, [], confirmation, message);
  return result !== null;
}

async function ensureUsers() {
  const page = await api("/oci/userPage", { method: "POST", body: { currentPage: 1, pageSize: 100 } });
  state.users = page.records || [];
  return state.users;
}

async function ensureSshKeys() {
  state.sshKeys = await api("/sshKey/list", { method: "GET" });
  return state.sshKeys;
}

async function renderOverview() {
  const data = await api("/sys/glance", { method: "GET" });
  setAppVersion(data.currentVersion);
  const cards = node("div", { className: "cards" });
  for (const [label, value] of [
    ["OCI 配置", data.users], ["活动任务", data.tasks], ["覆盖区域", data.regions], ["运行天数", data.days],
  ]) {
    cards.append(node("article", { className: "metric" }, node("span", { text: t(label) }), node("strong", { text: value || "0" })));
  }
  const help = panel("使用提示");
  help.append(node("p", { className: "muted", text: t("先添加 OCI API 配置，再创建实例任务。终止实例或引导卷、删除 VCN 都需要 Telegram 验证码；VCN 级联删除仅允许本工具创建且未混入外部资源的网络。") }));
  content.replaceChildren(cards, help);
}

async function renderConfigs() {
  await ensureUsers();
  const actions = actionGroup(
    button("添加配置", addConfig, "primary"),
    button("刷新", renderConfigs, "secondary"),
  );
  const root = panel("OCI API 配置", actions);
  root.append(table(
    ["名称", "区域", "创建时间", "状态", "操作"],
    state.users.map((user) => [
      user.username || user.id,
      user.regionName || user.region,
      user.createTime,
      user.enableCreate ? badge("任务运行中") : badge("可用"),
      actionGroup(
        button("实例", () => renderConfigDetails(user)),
        button("创建任务", () => createTask(user), "small", "正在加载…"),
        button("重命名", () => renameConfig(user)),
        button("删除", () => removeConfig(user), "danger"),
      ),
    ]),
  ));
  content.replaceChildren(root);
}

async function addConfig() {
  const data = await openForm("添加 OCI 配置", [
    { name: "username", label: "配置名称", required: true },
    { name: "ociConfig", label: "OCI config 内容", type: "textarea", required: true, placeholder: "[DEFAULT]\nuser=...\ntenancy=...\nregion=...\nfingerprint=..." },
    { name: "keyFile", label: "PEM 私钥", type: "file", required: true },
  ], "验证并保存", "配置会先调用 OCI API 验证；私钥以 0600 权限保存。config 中的 key_file 不会被直接采用。 ");
  if (!data) return;
  const form = new FormData();
  form.set("username", data.username);
  form.set("ociCfgStr", data.ociConfig);
  form.set("file", data.keyFile);
  await api("/oci/addCfg", { method: "POST", body: form });
  toast("OCI 配置已添加");
  await renderConfigs();
}

async function renameConfig(user) {
  const data = await openForm("重命名配置", [
    { name: "name", label: "新名称", value: user.username || "", required: true },
  ]);
  if (!data) return;
  await api("/oci/updateCfgName", { method: "POST", body: { cfgId: user.id, updateCfgName: data.name } });
  toast("名称已更新");
  await renderConfigs();
}

async function removeConfig(user) {
  if (!await confirmAction("删除 OCI 配置", t("确定删除「{name}」吗？活动任务会被取消，托管私钥会被删除。", { name: user.username || user.id }), "删除")) return;
  await api("/oci/removeCfg", { method: "POST", body: { idList: [user.id] } });
  toast("配置已删除");
  await renderConfigs();
}

async function renderSshKeys() {
  const sshKeys = await ensureSshKeys();
  const root = panel("SSH 公钥管理", actionGroup(
    button("生成密钥对", generateSshKeyPair, "primary", "正在生成…"),
    button("添加公钥", addSshPublicKey, "secondary"),
    button("刷新", renderSshKeys, "secondary"),
  ));
  root.append(
    node("p", {
      className: "muted",
      text: t("这里只保存公钥。生成密钥对时，私钥仅在本次下载中提供，请妥善保管。"),
    }),
    table(
      ["名称", "指纹", "SSH 公钥", "创建时间", "操作"],
      sshKeys.map((sshKey) => [
        sshKey.name,
        sshKey.fingerprint,
        node("code", {
          className: "ssh-public-key",
          text: sshKey.publicKey,
          title: sshKey.publicKey,
        }),
        sshKey.createTime,
        button("重命名", () => renameSshPublicKey(sshKey)),
      ]),
    ),
  );
  content.replaceChildren(root);
}

async function saveSshPublicKey(name, publicKey) {
  const saved = await api("/sshKey/add", {
    method: "POST",
    body: { name, publicKey },
  });
  state.sshKeys = [saved, ...state.sshKeys.filter((item) => item.id !== saved.id)];
  return saved;
}

async function addSshPublicKey() {
  const data = await openForm("添加 SSH 公钥", [
    { name: "name", label: "公钥名称", required: true, maxLength: 128 },
    {
      name: "publicKey",
      label: "SSH 公钥",
      type: "textarea",
      required: true,
      maxLength: 16384,
      placeholder: "ssh-ed25519 AAAA...",
      help: "每条记录只保存一把 OpenSSH 公钥。",
    },
  ], "添加");
  if (!data) return;
  await saveSshPublicKey(data.name, data.publicKey);
  toast("SSH 公钥已添加");
  await renderSshKeys();
}

async function renameSshPublicKey(sshKey) {
  const data = await openForm("重命名 SSH 公钥", [
    { name: "name", label: "新名称", value: sshKey.name, required: true, maxLength: 128 },
  ], "更新");
  if (!data) return;
  await api("/sshKey/rename", {
    method: "POST",
    body: { id: sshKey.id, name: data.name },
  });
  toast("SSH 公钥名称已更新");
  await renderSshKeys();
}

async function generateSshKeyPair() {
  const data = await openForm("生成 SSH 密钥对", [
    { name: "name", label: "公钥名称", required: true, maxLength: 128 },
  ], "生成并下载", "将生成 Ed25519 密钥对。公钥会自动保存，私钥不会在服务器上留存；下载 ZIP 后请立即备份。");
  if (!data) return;
  await downloadApi(
    "/sshKey/generate",
    { method: "POST", body: { name: data.name } },
    "oci-helper-ssh-key.zip",
  );
  toast("密钥对已生成并开始下载");
  await renderSshKeys();
}

async function createTask(user) {
  const architectureOptions = [
    { value: "ARM", label: "ARM · VM.Standard.A1.Flex" },
    { value: "AMD", label: "AMD · VM.Standard.E2.1.Micro" },
    { value: "ARM_A2", label: "ARM · VM.Standard.A2.Flex" },
    { value: "AMD_E5", label: "AMD · VM.Standard.E5.Flex" },
  ];
  const imageOptionsCache = new Map();
  const availabilityDomainOptionsCache = new Map();
  const loadOptions = (cache, path, architecture) => {
    if (!cache.has(architecture)) {
      const request = api(path, {
        method: "POST",
        body: { ociCfgId: user.id, architecture },
      }).catch((error) => {
        cache.delete(architecture);
        throw error;
      });
      cache.set(architecture, request);
    }
    return cache.get(architecture);
  };
  const loadImageOptions = (architecture) => loadOptions(
    imageOptionsCache,
    "/oci/imageOptions",
    architecture,
  );
  const loadAvailabilityDomainOptions = (architecture) => loadOptions(
    availabilityDomainOptionsCache,
    "/oci/availabilityDomainOptions",
    architecture,
  );
  const asSelectOptions = (options) => options.map((option) => ({
    value: JSON.stringify([option.operatingSystem, option.operatingSystemVersion]),
    label: option.label,
  }));
  const asAvailabilityDomainOptions = (options) => [
    { value: "", label: "自动轮换（推荐）" },
    ...options.map((option) => ({ value: option.name, label: option.label })),
  ];
  const initialArchitecture = architectureOptions[0].value;
  const [initialImageOptions, initialAvailabilityDomainOptions, sshKeys] = await Promise.all([
    loadImageOptions(initialArchitecture),
    loadAvailabilityDomainOptions(initialArchitecture),
    ensureSshKeys(),
  ]);
  if (!initialImageOptions.length) {
    throw new Error(t("当前区域没有适用于 {architecture} 的 Linux 镜像", { architecture: architectureOptions[0].label }));
  }
  if (!initialAvailabilityDomainOptions.length) {
    throw new Error(t("当前区域没有支持 {architecture} 的可用域", { architecture: architectureOptions[0].label }));
  }
  let activeArchitecture = initialArchitecture;
  const newSshKeyValue = "__new_ssh_key__";
  const hasSavedSshKeys = sshKeys.length > 0;
  const sshKeyOptions = [
    ...sshKeys.map((sshKey) => ({
      value: sshKey.id,
      label: `${sshKey.name} · ${sshKey.fingerprint}`,
    })),
    { value: newSshKeyValue, label: "添加新公钥…" },
  ];
  const toggleNewSshKeyFields = (form, selection) => {
    const isNewKey = selection === newSshKeyValue;
    for (const fieldName of ["sshKeyName", "sshPublicKey"]) {
      const input = form.elements.namedItem(fieldName);
      input.disabled = !isNewKey;
      input.required = isNewKey;
      input.closest("label").hidden = !isNewKey;
    }
  };

  const data = await openForm(t("创建实例任务 · {name}", { name: user.username || user.id }), [
    {
      name: "instanceName",
      label: "实例名称",
      maxLength: 251,
      placeholder: "例如 web-server",
      help: "留空时自动生成；批量创建时会自动追加 -001、-002 等序号。",
    },
    {
      name: "architecture",
      label: "架构 / Shape",
      type: "select",
      options: architectureOptions,
      onChange: async (architecture, form) => {
        activeArchitecture = architecture;
        const imageSelect = form.elements.namedItem("imageSelection");
        const availabilityDomainSelect = form.elements.namedItem("availabilityDomain");
        const submit = $("#modal-submit");
        imageSelect.disabled = true;
        availabilityDomainSelect.disabled = true;
        submit.disabled = true;
        replaceSelectOptions(imageSelect, [], "正在查询可用镜像…");
        replaceSelectOptions(availabilityDomainSelect, [], "正在查询可用域…");
        let ready = false;
        try {
          let imageOptions;
          let availabilityDomainOptions;
          try {
            [imageOptions, availabilityDomainOptions] = await Promise.all([
              loadImageOptions(architecture),
              loadAvailabilityDomainOptions(architecture),
            ]);
          } catch (error) {
            if (activeArchitecture !== architecture) return;
            replaceSelectOptions(imageSelect, [], "镜像加载失败，请重新选择 Shape 重试");
            replaceSelectOptions(
              availabilityDomainSelect,
              [],
              "可用域加载失败，请重新选择 Shape 重试",
            );
            throw error;
          }
          if (activeArchitecture !== architecture) return;
          replaceSelectOptions(
            imageSelect,
            asSelectOptions(imageOptions),
            "当前 Shape 没有可用 Linux 镜像",
          );
          replaceSelectOptions(
            availabilityDomainSelect,
            availabilityDomainOptions.length
              ? asAvailabilityDomainOptions(availabilityDomainOptions)
              : [],
            "当前 Shape 没有支持的可用域",
          );
          if (!imageOptions.length) {
            throw new Error(t("当前区域和 Shape 没有可用的 Linux 镜像"));
          }
          if (!availabilityDomainOptions.length) {
            throw new Error(t("当前区域没有支持所选 Shape 的可用域"));
          }
          ready = true;
        } finally {
          if (activeArchitecture === architecture && imageSelect.isConnected) {
            imageSelect.disabled = false;
            availabilityDomainSelect.disabled = false;
            submit.disabled = !ready;
          }
        }
      },
    },
    { name: "ocpus", label: "OCPU", type: "number", value: 1, min: 1, max: 4, step: 1, required: true },
    { name: "memory", label: "内存（GB）", type: "number", value: 6, min: 1, max: 24, step: 1, required: true },
    { name: "disk", label: "引导卷（GB）", type: "number", value: 50, min: 50, max: 200, required: true },
    { name: "bootVolumeVpusPerGB", label: "引导卷性能（VPU/GB）", type: "number", value: 20, min: 10, max: 120, step: 1, required: true, help: "支持 10（均衡）、20（高性能）或 30 至 120（超高性能）。" },
    {
      name: "imageSelection",
      label: "操作系统",
      type: "select",
      options: asSelectOptions(initialImageOptions),
      required: true,
      help: "选项来自当前 OCI 区域中与所选 Shape 兼容的 AVAILABLE Linux 镜像。",
    },
    {
      name: "availabilityDomain",
      label: "可用域",
      type: "select",
      options: asAvailabilityDomainOptions(initialAvailabilityDomainOptions),
      help: "选项来自当前 OCI 区域；选择自动轮换时，会在支持当前 Shape 的可用域之间轮换重试。",
    },
    {
      name: "sshKeySelection",
      label: "SSH 公钥",
      type: "select",
      options: sshKeyOptions,
      required: true,
      onChange: async (selection, form) => toggleNewSshKeyFields(form, selection),
      help: "可选择已保存的公钥，也可添加一把新公钥。",
    },
    {
      name: "sshKeyName",
      label: "新公钥名称",
      required: true,
      maxLength: 128,
      hidden: hasSavedSshKeys,
      disabled: hasSavedSshKeys,
    },
    {
      name: "sshPublicKey",
      label: "新 SSH 公钥",
      type: "textarea",
      required: true,
      maxLength: 16384,
      placeholder: "ssh-ed25519 AAAA...",
      help: "新公钥会自动保存到公钥列表；Ubuntu 默认用户为 ubuntu，Oracle Linux/CentOS 默认用户为 opc。",
      hidden: hasSavedSshKeys,
      disabled: hasSavedSshKeys,
    },
    { name: "createNumbers", label: "创建数量", type: "number", value: 1, min: 1, max: 100, required: true },
    { name: "interval", label: "最短创建间隔（秒）", type: "number", value: 5, min: 5, max: 86400, required: true },
    { name: "intervalMax", label: "最长创建间隔（秒）", type: "number", value: 30, min: 5, max: 86400, required: true, help: "每次创建或重试后，会在最短与最长间隔之间随机等待。" },
    { name: "maxAttempts", label: "最大尝试次数", type: "number", value: 1000, min: 0, max: 100000, required: true, help: "按实际调用 OCI 创建接口的次数统计；0 表示不限次数。" },
  ], "提交任务", "实例仅启用 SSH Key 登录；可固定可用域，也可自动轮换，并使用随机创建间隔。");
  if (!data) return;
  let sshPublicKey;
  if (data.sshKeySelection === newSshKeyValue) {
    const saved = await saveSshPublicKey(data.sshKeyName, data.sshPublicKey);
    sshPublicKey = saved.publicKey;
  } else {
    const selectedKey = sshKeys.find((sshKey) => sshKey.id === data.sshKeySelection);
    if (!selectedKey) throw new Error(t("所选 SSH 公钥不存在，请刷新后重试"));
    sshPublicKey = selectedKey.publicKey;
  }
  const [operationSystem, operationSystemVersion] = JSON.parse(data.imageSelection);
  const bootVolumeVpusPerGB = Number(data.bootVolumeVpusPerGB);
  if (bootVolumeVpusPerGB !== 10 && bootVolumeVpusPerGB !== 20
      && (bootVolumeVpusPerGB < 30 || bootVolumeVpusPerGB > 120)) {
    throw new Error("引导卷 VPU/GB 只能是 10、20 或 30 至 120");
  }
  await api("/oci/createInstance", { method: "POST", body: {
    userId: user.id,
    instanceName: data.instanceName || null,
    architecture: data.architecture,
    ocpus: Number(data.ocpus),
    memory: data.memory,
    disk: Number(data.disk),
    bootVolumeVpusPerGB,
    operationSystem,
    operationSystemVersion,
    availabilityDomain: data.availabilityDomain || null,
    createNumbers: Number(data.createNumbers),
    interval: Number(data.interval),
    intervalMax: Number(data.intervalMax),
    maxAttempts: Number(data.maxAttempts),
    sshPublicKey,
  } });
  toast("创建任务已提交");
  await renderConfigs();
}

async function renderConfigDetails(user, clean = false) {
  state.selectedUser = user;
  content.replaceChildren(node("div", { className: "panel empty", text: t("正在读取 OCI 资源…") }));
  const data = await api("/oci/details", { method: "POST", body: { cfgId: user.id, cleanReLaunchDetails: clean } });
  const top = panel(user.username || user.id, actionGroup(
    button("返回", renderConfigs, "secondary"),
    button("刷新", () => renderConfigDetails(user, true), "secondary"),
    button("放行全部安全规则", async () => {
      if (!await confirmAction("放行安全规则", "这会把该配置下所有 VCN 的默认安全列表改为允许全部 IPv4/IPv6 流量。确认继续？", "放行")) return;
      await api("/oci/releaseSecurityRule", { method: "POST", body: { cfgId: user.id } });
      toast("安全列表已更新");
    }, "danger"),
  ));
  top.append(node("p", { className: "muted", text: `${data.region || "—"} · ${data.tenantId || "—"}` }));
  const grid = node("div", { className: "instance-grid" });
  for (const instance of data.instanceList || []) grid.append(instanceCard(user, instance));
  if (!grid.childElementCount) grid.append(node("div", { className: "panel empty", text: t("当前配置没有活动实例") }));
  content.replaceChildren(top, grid);
}

function instanceCard(user, instance) {
  const card = node("article", { className: "instance-card" });
  card.append(node("div", { className: "row-between" }, node("h4", { text: instance.name || instance.ocId }), badge(instance.state)));
  const facts = node("dl", { className: "facts" });
  for (const [label, value] of [
    ["Shape", instance.shape], ["配置", `${instance.ocpus || "—"} OCPU / ${instance.memory || "—"} GB`],
    ["公网 IP", (instance.publicIp || []).join(", ") || "—"], ["可用域", instance.availabilityDomain], ["创建时间", instance.createTime],
  ]) facts.append(node("dt", { text: t(label) }), node("dd", { text: value || "—" }));
  const actions = actionGroup(
    ...["START", "STOP", "RESET"].map((action) => button(action, () => instanceAction(user, instance, action))),
    button("更换 IP", () => changeIp(user, instance)),
    button("改配置", () => updateInstanceConfig(user, instance)),
    button("改名称", () => renameInstance(user, instance)),
    button("IPv6", () => createIpv6(user, instance)),
    button("流量", () => showTraffic(user, instance)),
    button("控制台连接", () => createConsole(user, instance)),
    button("终止", () => terminateInstance(user, instance), "danger"),
  );
  card.append(facts, actions);
  return card;
}

async function instanceAction(user, instance, action) {
  if (!await confirmAction("实例操作", t("确定对「{name}」执行 {action}？", { name: instance.name || instance.ocId, action }))) return;
  await api("/oci/updateInstanceState", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, action } });
  toast("操作已提交");
  await renderConfigDetails(user, true);
}

async function changeIp(user, instance) {
  const vnics = instance.vnicList || [];
  if (!vnics.length) throw new Error("该实例没有可用 VNIC");
  const data = await openForm("更换公网 IP", [
    { name: "vnicId", label: "VNIC", type: "select", options: vnics.map((item) => ({ value: item.vnicId, label: item.name || item.vnicId })) },
    { name: "cidrs", label: "目标 CIDR（可选，一行一个）", type: "textarea", placeholder: "203.0.113.0/24" },
  ], "提交任务", "未填写 CIDR 时只更换一次；填写后会重试到匹配或达到最大次数。 ");
  if (!data) return;
  const cidrList = data.cidrs.split(/\s+/).map((item) => item.trim()).filter(Boolean);
  await api("/oci/changeIp", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, vnicId: data.vnicId, cidrList } });
  toast("换 IP 任务已提交");
  await renderConfigDetails(user, true);
}

async function updateInstanceConfig(user, instance) {
  const data = await openForm("调整实例配置", [
    { name: "ocpus", label: "OCPU", type: "number", value: instance.ocpus || 1, min: .1, step: .1, required: true },
    { name: "memory", label: "内存（GB）", type: "number", value: instance.memory || 6, min: 1, step: 1, required: true },
  ], "更新", "仅 Flex Shape 支持直接调整 OCPU 和内存。 ");
  if (!data) return;
  await api("/oci/updateInstanceCfg", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, ocpus: data.ocpus, memory: data.memory } });
  toast("实例配置更新已提交");
  await renderConfigDetails(user, true);
}

async function renameInstance(user, instance) {
  const data = await openForm("修改实例名称", [{ name: "name", label: "名称", value: instance.name || "", required: true }]);
  if (!data) return;
  await api("/oci/updateInstanceName", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, name: data.name } });
  toast("实例名称已更新");
  await renderConfigDetails(user, true);
}

async function createIpv6(user, instance) {
  if (!await confirmAction("创建 IPv6", "将为该实例主 VNIC 创建 IPv6 地址。确认继续？")) return;
  const result = await api("/oci/createIpv6", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId } });
  toast(t("IPv6 已创建：{address}", { address: result.ipv6 }));
}

async function createConsole(user, instance) {
  const data = await openForm("创建控制台连接", [
    { name: "publicKey", label: "SSH 公钥", type: "textarea", required: true, maxLength: 16384, placeholder: "ssh-rsa AAAA...", help: "请使用本机 RSA 密钥对的 OpenSSH 公钥；连接时需要对应私钥。私钥不会上传。" },
  ], "创建", "OCI 控制台连接仅用于故障排查。已有活动连接时，请先在 OCI 控制台中删除旧连接。");
  if (!data) return;
  const result = await api("/oci/startVnc", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, publicKey: data.publicKey } });
  await openForm("控制台连接命令", [], "关闭", result.vncConnectionString || "OCI 未返回连接命令");
}

async function showTraffic(user, instance) {
  const result = await api("/traffic/data", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId } });
  const rows = (result.labels || []).map((label, index) => [label, result.ingress[index], result.egress[index]]);
  const root = panel(t("最近一小时流量 · {name}", { name: instance.name || instance.ocId }), button("返回实例", () => renderConfigDetails(user), "secondary"));
  root.append(table(["时间", "入站 MB", "出站 MB"], rows));
  content.replaceChildren(root);
}

async function terminateInstance(user, instance) {
  if (!await confirmAction("终止实例", "实例终止后无法恢复。验证码会发送至已配置的 Telegram。", "发送验证码")) return;
  await api("/oci/sendCaptcha", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId } });
  const data = await openForm("输入终止验证码", [
    { name: "captcha", label: "6 位验证码", required: true },
    { name: "preserve", label: "保留引导卷", type: "checkbox", value: true },
  ], "终止实例");
  if (!data) return;
  await api("/oci/terminateInstance", { method: "POST", body: { ociCfgId: user.id, instanceId: instance.ocId, preserveBootVolume: data.preserve ? 1 : 0, captcha: data.captcha } });
  toast("终止命令已提交");
  await renderConfigDetails(user, true);
}

async function renderTasks() {
  const page = await api("/oci/createTaskPage", { method: "POST", body: { currentPage: 1, pageSize: 100 } });
  const root = panel("任务列表", button("刷新", renderTasks, "secondary"));
  root.append(table(
    ["实例名称", "配置", "区域 / 可用域", "规格", "剩余", "尝试", "间隔 / 上限", "状态", "错误", "操作"],
    (page.records || []).map((task) => {
      const domain = task.availabilityDomain || t("自动轮换");
      const attemptLimit = task.maxAttempts === 0 ? t("不限") : t("{count} 次", { count: task.maxAttempts });
      return [
        task.instanceName || t("自动生成"), task.username, `${task.region} · ${domain}`, `${task.architecture} · ${task.ocpus}/${task.memory}/${task.disk}GB · ${task.bootVolumeVpusPerGB ?? 10} VPU/GB`,
        task.createNumbers, task.counts, t("{min}–{max} 秒 · {limit}", { min: task.interval, max: task.intervalMax, limit: attemptLimit }),
        badge(task.status), task.lastError || "—",
        ["pending", "running", "paused"].includes(task.status)
          ? actionGroup(
            task.status === "paused"
              ? button("恢复", () => taskCommand("/oci/resumeCreateBatch", task.id))
              : button("暂停", () => taskCommand("/oci/pauseCreateBatch", task.id)),
            button("停止", () => stopTask(task), "danger"),
          )
          : "—",
      ];
    }),
  ));
  content.replaceChildren(root);
}

async function renderTaskLogs(lineCount = 300) {
  const data = await api("/sys/taskLogs", { method: "POST", body: { lines: lineCount } });
  const lineSelect = node("select", { attrs: { "aria-label": t("显示日志行数") } });
  for (const count of [100, 300, 500, 1000, 2000]) {
    lineSelect.append(node("option", { text: t("最新 {count} 行", { count }), attrs: { value: count } }));
  }
  lineSelect.value = String(lineCount);
  lineSelect.addEventListener("change", () => renderTaskLogs(Number(lineSelect.value)));

  const root = panel("任务执行日志", actionGroup(
    lineSelect,
    button("刷新", () => renderTaskLogs(lineCount), "secondary", "正在刷新…"),
  ));
  const lineTotal = data.lineCount || 0;
  const meta = data.updatedAt
    ? t("最后写入：{updatedAt} · 显示 {lineTotal} 行{truncated}", {
      updatedAt: data.updatedAt,
      lineTotal,
      truncated: data.truncated ? t(" · 已截取最新内容") : "",
    })
    : t("日志文件尚未生成；任务开始执行后可在此查看。");
  const output = node("pre", {
    className: "code log-output",
    text: (data.lines || []).join("\n") || t("暂无任务执行日志。"),
    attrs: { tabindex: "0", "aria-label": t("任务执行日志内容") },
  });
  root.append(node("p", { className: "muted", text: meta }), output);
  content.replaceChildren(root);
  output.scrollTop = output.scrollHeight;
}

async function taskCommand(path, id) {
  await api(path, { method: "POST", body: { idList: [id] } });
  toast("任务状态已更新");
  await renderTasks();
}

async function stopTask(task) {
  if (!await confirmAction("停止任务", t("确定停止任务 {id}？", { id: task.id }), "停止")) return;
  await api("/oci/stopCreate", { method: "POST", body: { taskId: task.id } });
  toast("任务已停止");
  await renderTasks();
}

async function renderResources() {
  await ensureUsers();
  if (!state.users.length) {
    content.replaceChildren(node("div", { className: "panel empty", text: t("请先添加 OCI 配置") }));
    return;
  }
  const selectedId = state.selectedUser?.id || state.users[0].id;
  state.selectedUser = state.users.find((item) => item.id === selectedId) || state.users[0];
  const select = node("select");
  for (const user of state.users) select.append(node("option", { text: `${user.username || user.id} · ${user.region}`, attrs: { value: user.id } }));
  select.value = state.selectedUser.id;
  select.addEventListener("change", () => { state.selectedUser = state.users.find((item) => item.id === select.value); renderResources(); });
  const root = panel("资源配置", select);
  root.append(node("div", { className: "resource-tabs" },
    button("VCN", () => renderVcns(state.selectedUser), "secondary"),
    button("引导卷", () => renderBootVolumes(state.selectedUser), "secondary"),
    button("服务限额", () => renderLimits(state.selectedUser), "secondary"),
    button("租户信息", () => showTenant(state.selectedUser), "secondary"),
  ), node("p", { className: "muted", text: t("选择上方资源类型开始查询。所有 OCI SDK 调用均在工作线程执行，不阻塞 API 事件循环。") }));
  content.replaceChildren(root);
}

async function showTenant(user) {
  const data = await api("/tenant/info", { method: "POST", body: { ociCfgId: user.id } });
  const root = panel("租户信息", button("返回", renderResources, "secondary"));
  const facts = node("dl", { className: "facts" });
  for (const [label, value] of [["租户", data.tenantName], ["主区域", data.homeRegion], ["订阅区域", (data.subscribedRegions || []).join(", ")]]) {
    facts.append(node("dt", { text: t(label) }), node("dd", { text: value || "—" }));
  }
  root.append(facts);
  content.replaceChildren(root);
}

async function renderVcns(user) {
  const page = await api("/vcn/page", { method: "POST", body: { ociCfgId: user.id, currentPage: 1, pageSize: 100, cleanReLaunch: true } });
  const root = panel("VCN", actionGroup(button("返回", renderResources, "secondary"), button("刷新", () => renderVcns(user), "secondary")));
  root.append(table(["名称", "状态", "可见性", "创建时间", "操作"], (page.records || []).map((vcn) => [
    vcn.displayName, badge(vcn.status), vcn.visibility, vcn.createTime,
    actionGroup(
      button("安全规则", () => renderSecurityRules(user, vcn, 0)),
      button("删除", () => removeVcn(user, vcn), "danger"),
    ),
  ])));
  content.replaceChildren(root);
}

async function removeVcn(user, vcn) {
  if (!await confirmAction("删除 VCN", "仅空 VCN 或完全由本工具创建且未混入外部资源的网络可被删除。确认继续？", "删除")) return;
  const captcha = await requestDestructiveCaptcha(user);
  if (!captcha) return;
  await api("/vcn/remove", { method: "POST", body: { ociCfgId: user.id, vcnIds: [vcn.id], captcha } });
  toast("VCN 已删除");
  await renderVcns(user);
}

async function renderSecurityRules(user, vcn, direction) {
  state.selectedVcn = vcn;
  const page = await api("/securityRule/page", { method: "POST", body: { ociCfgId: user.id, vcnId: vcn.id, type: direction, currentPage: 1, pageSize: 100, cleanReLaunch: true } });
  const root = panel(t("安全规则 · {name}", { name: vcn.displayName }), actionGroup(
    button("返回 VCN", () => renderVcns(user), "secondary"),
    button(direction === 0 ? "查看出站" : "查看入站", () => renderSecurityRules(user, vcn, direction === 0 ? 1 : 0), "secondary"),
    button("添加规则", () => addSecurityRule(user, vcn, direction), "primary"),
  ));
  root.append(table(["协议", "来源 / 目标", "源端口", "目标端口", "说明", "操作"], (page.records || []).map((rule) => [
    rule.protocol, rule.sourceOrDestination, rule.sourcePort, rule.destinationPort, rule.description || "—",
    button("删除", () => removeSecurityRule(user, vcn, direction, rule.id), "danger"),
  ])));
  content.replaceChildren(root);
}

async function addSecurityRule(user, vcn, direction) {
  const data = await openForm(direction === 0 ? "添加入站规则" : "添加出站规则", [
    { name: "protocol", label: "协议", type: "select", options: [{ value: "all", label: "全部" }, { value: "6", label: "TCP" }, { value: "17", label: "UDP" }, { value: "1", label: "ICMP" }] },
    { name: "cidr", label: direction === 0 ? "来源 CIDR" : "目标 CIDR", value: "0.0.0.0/0", required: true },
    { name: "sourcePort", label: "源端口（可选）", placeholder: "20-22" },
    { name: "destinationPort", label: "目标端口（可选）", placeholder: "443" },
    { name: "description", label: "说明（可选）" },
    { name: "stateless", label: "无状态规则", type: "checkbox" },
  ]);
  if (!data) return;
  const common = {
    protocol: data.protocol, isStateless: data.stateless,
    sourcePort: data.sourcePort || null, destinationPort: data.destinationPort || null,
    description: data.description || null,
  };
  const body = direction === 0
    ? { ociCfgId: user.id, vcnId: vcn.id, inboundRule: { ...common, sourceType: "CIDR_BLOCK", source: data.cidr } }
    : { ociCfgId: user.id, vcnId: vcn.id, outboundRule: { ...common, destinationType: "CIDR_BLOCK", destination: data.cidr } };
  await api(direction === 0 ? "/securityRule/addIngress" : "/securityRule/addEgress", { method: "POST", body });
  toast("安全规则已添加");
  await renderSecurityRules(user, vcn, direction);
}

async function removeSecurityRule(user, vcn, direction, id) {
  if (!await confirmAction("删除安全规则", "确定删除这条规则？", "删除")) return;
  await api("/securityRule/remove", { method: "POST", body: { ociCfgId: user.id, vcnId: vcn.id, type: direction, ruleIds: [id] } });
  toast("安全规则已删除");
  await renderSecurityRules(user, vcn, direction);
}

async function renderBootVolumes(user) {
  const page = await api("/bootVolume/page", { method: "POST", body: { ociCfgId: user.id, currentPage: 1, pageSize: 100, cleanReLaunch: true } });
  const root = panel("引导卷", actionGroup(button("返回", renderResources, "secondary"), button("刷新", () => renderBootVolumes(user), "secondary")));
  root.append(table(["名称", "状态", "容量", "VPU/GB", "挂载", "创建时间", "操作"], (page.records || []).map((volume) => [
    volume.displayName, badge(volume.status), `${volume.sizeInGBs} GB`, volume.vpusPerGB, t(volume.attached ? "是" : "否"), volume.createTime,
    actionGroup(button("调整", () => updateBootVolume(user, volume)), !volume.attached ? button("终止", () => terminateBootVolume(user, volume), "danger") : null),
  ])));
  content.replaceChildren(root);
}

async function updateBootVolume(user, volume) {
  const data = await openForm("调整引导卷", [
    { name: "size", label: "容量（GB，只能增加）", type: "number", value: volume.sizeInGBs, min: volume.sizeInGBs, required: true },
    { name: "vpu", label: "VPU/GB", type: "number", value: volume.vpusPerGB ?? 10, min: 10, max: 120, step: 1, required: true, help: "支持 10（均衡）、20（高性能）或 30 至 120（超高性能）。" },
  ]);
  if (!data) return;
  const vpu = Number(data.vpu);
  if (vpu !== 10 && vpu !== 20 && (vpu < 30 || vpu > 120)) {
    throw new Error("引导卷 VPU/GB 只能是 10、20 或 30 至 120");
  }
  await api("/bootVolume/update", { method: "POST", body: { ociCfgId: user.id, bootVolumeId: volume.id, bootVolumeSize: Number(data.size), bootVolumeVpu: vpu } });
  toast("引导卷配置已更新");
  await renderBootVolumes(user);
}

async function terminateBootVolume(user, volume) {
  if (!await confirmAction("终止引导卷", "引导卷数据将永久删除。确认继续？", "终止")) return;
  const captcha = await requestDestructiveCaptcha(user);
  if (!captcha) return;
  await api("/bootVolume/terminate", { method: "POST", body: { ociCfgId: user.id, bootVolumeIds: [volume.id], captcha } });
  toast("引导卷终止命令已提交");
  await renderBootVolumes(user);
}

async function requestDestructiveCaptcha(user) {
  await api("/oci/sendCaptcha", { method: "POST", body: { ociCfgId: user.id } });
  const data = await openForm("Telegram 验证码", [
    { name: "captcha", label: "6 位验证码", required: true, pattern: "[0-9]{6}", maxLength: 6 },
  ], "验证", "验证码已发送到已配置的 Telegram 会话，5 分钟内有效。");
  return data?.captcha || null;
}

async function renderLimits(user) {
  const services = await api(`/limits/services?ociCfgId=${encodeURIComponent(user.id)}`, { method: "GET" });
  const select = node("select");
  select.append(node("option", { text: t("全部服务"), attrs: { value: "" } }));
  for (const service of services || []) select.append(node("option", { text: service, attrs: { value: service } }));
  const root = panel("服务限额", actionGroup(button("返回", renderResources, "secondary"), select, button("查询", async () => {
    const data = await api("/limits/query", { method: "POST", body: { ociCfgId: user.id, serviceName: select.value || null } });
    root.querySelector(".limits-result")?.remove();
    const result = node("div", { className: "limits-result" }, table(["服务", "限额", "范围", "可用域", "上限", "已用", "可用"], (data.items || []).map((item) => [
      item.serviceName, item.limitName, item.scopeType, item.availabilityDomain, item.serviceLimit, item.used, item.available,
    ])));
    root.append(result);
  }, "primary")));
  root.append(node("p", { className: "muted", text: t("选择服务后查询可减少 OCI Limits API 调用数量。") }));
  content.replaceChildren(root);
}

async function renderSettings() {
  const config = await api("/sys/getSysCfg", { method: "POST", body: {} });

  const adminRoot = panel("管理员账号");
  const adminForm = node("form");
  const accountLabel = node("label", { className: "field" }, node("span", { text: t("管理员用户名") }), node("input", { attrs: { name: "account", autocomplete: "username", required: "", maxlength: 128 } }));
  const currentPasswordLabel = node("label", { className: "field" }, node("span", { text: t("当前密码") }), node("input", { type: "password", attrs: { name: "currentPassword", autocomplete: "current-password", required: "", maxlength: 256 } }));
  const newPasswordLabel = node("label", { className: "field" }, node("span", { text: t("新密码（可选）") }), node("input", { type: "password", attrs: { name: "newPassword", autocomplete: "new-password", minlength: 12, maxlength: 256 } }));
  const confirmPasswordLabel = node("label", { className: "field" }, node("span", { text: t("确认新密码") }), node("input", { type: "password", attrs: { name: "confirmPassword", autocomplete: "new-password", minlength: 12, maxlength: 256 } }));
  const adminError = node("p", { className: "form-error", attrs: { role: "alert" } });
  $("input", accountLabel).value = config.adminAccount || "";
  adminForm.append(
    accountLabel,
    currentPasswordLabel,
    newPasswordLabel,
    confirmPasswordLabel,
    adminError,
    actionGroup(node("button", { className: "primary", text: t("保存管理员账号"), type: "submit" })),
  );
  adminForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!adminForm.reportValidity()) return;
    const submit = event.submitter;
    const values = new FormData(adminForm);
    const account = String(values.get("account") || "").trim();
    const currentPassword = String(values.get("currentPassword") || "");
    const newPassword = String(values.get("newPassword") || "");
    const confirmPassword = String(values.get("confirmPassword") || "");
    adminError.textContent = "";
    if (!account) {
      adminError.textContent = t("管理员用户名不能为空");
      return;
    }
    if (newPassword !== confirmPassword) {
      adminError.textContent = t("两次输入的新密码不一致");
      return;
    }
    if (account === config.adminAccount && !newPassword) {
      adminError.textContent = t("请修改管理员用户名或填写新密码");
      return;
    }
    submit.disabled = true;
    try {
      await api("/sys/updateAdminCredentials", {
        method: "POST",
        body: { account, currentPassword, newPassword: newPassword || null },
      });
      adminForm.reset();
      logout();
      const loginForm = $("#login-form");
      loginForm.reset();
      loginForm.elements.namedItem("account").value = account;
      $("#login-error").textContent = t("管理员账号已更新，请使用新凭据重新登录。");
    } catch (error) {
      adminError.textContent = t(error.message);
    } finally {
      submit.disabled = false;
    }
  });
  adminRoot.append(
    node("p", { className: "muted", text: t("修改前需要验证当前密码。新密码至少 12 个字符；留空时仅修改用户名。保存后所有已登录会话都会失效。") }),
    adminForm,
  );

  const telegramRoot = panel("Telegram 通知");
  const telegramForm = node("form");
  const tokenLabel = node("label", { className: "field" }, node("span", { text: "Bot Token" }), node("input", { type: "password", attrs: { name: "token", autocomplete: "off" } }));
  const chatLabel = node("label", { className: "field" }, node("span", { text: "Chat ID" }), node("input", { attrs: { name: "chat" } }));
  $("input", tokenLabel).placeholder = t(config.tgBotConfigured ? "已配置；修改时请重新输入" : "请输入 Bot Token");
  $("input", chatLabel).value = config.tgChatId || "";
  telegramForm.append(tokenLabel, chatLabel, actionGroup(node("button", { className: "primary", text: t("验证并保存"), type: "submit" }), button("发送测试消息", async () => {
    const data = await openForm("发送 Telegram 测试消息", [{ name: "message", label: "消息", value: "OCI Helper 通知测试", required: true }], "发送");
    if (!data) return;
    await api("/sys/sendMsg", { method: "POST", body: { message: data.message } });
    toast("测试消息已发送");
  }, "secondary")));
  telegramForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    submit.disabled = true;
    try {
      const values = new FormData(telegramForm);
      await api("/sys/updateSysCfg", { method: "POST", body: { tgBotToken: values.get("token") || null, tgChatId: values.get("chat") || null } });
      telegramForm.reset();
      toast("Telegram 配置已保存");
    } catch (error) { toast(error.message, true); }
    finally { submit.disabled = false; }
  });
  telegramRoot.append(node("p", { className: "muted", text: t("出于安全考虑，已保存的 Token 不会回传。保存前会调用 Telegram getMe 验证配置；修改时请重新输入 Token 和 Chat ID，清空两个字段可停用通知。") }), telegramForm);
  content.replaceChildren(node("div", { className: "grid-2" }, adminRoot, telegramRoot));
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  $("#login-error").textContent = "";
  try {
    const values = new FormData(form);
    const data = await api("/sys/login", { method: "POST", body: { account: values.get("account"), password: values.get("password") } });
    form.reset();
    state.token = data.token;
    setAppVersion(data.currentVersion);
    sessionStorage.setItem("oci-helper-token", data.token);
    setAuthenticated(true);
    await navigate("overview");
  } catch (error) {
    $("#login-error").textContent = t(error.message);
  } finally {
    submit.disabled = false;
  }
});

$("#navigation").addEventListener("click", (event) => {
  const target = event.target.closest("button[data-view]");
  if (target) navigate(target.dataset.view);
});
$("#logout").addEventListener("click", logout);
$("#theme-toggle").addEventListener("click", () => {
  document.documentElement.classList.toggle("dark");
  localStorage.setItem("oci-helper-theme", document.documentElement.classList.contains("dark") ? "dark" : "light");
});

window.addEventListener("oci-helper:languagechange", () => {
  $("#login-error").textContent = "";
  $("#toast").hidden = true;
  if (state.token) navigate(state.view);
});

if (localStorage.getItem("oci-helper-theme") === "dark") document.documentElement.classList.add("dark");
setAuthenticated(Boolean(state.token));
if (state.token) navigate("overview");
