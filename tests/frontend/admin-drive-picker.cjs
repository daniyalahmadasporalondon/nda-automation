"use strict";

// Frontend unit test for the "Browse Drive" folder picker in admin-drive.js.
//
// admin-drive.js is a browser IIFE assigned to a global that also exposes a
// CommonJS export behind a `typeof module` guard (a no-op in the page). We
// require it here and drive the REAL controller through a minimal fake DOM + a
// stubbed fetch, so the shipped picker wiring (open, breadcrumb drill, select,
// confirm-fills-the-fields, disconnected error) runs exactly as in the browser.
//
// Coverage (the picker's behavioural contract):
//   * "Browse Drive" opens the modal and GETs /api/admin/drive-folders?parent=root;
//   * the returned folders render as a clickable list;
//   * clicking a folder's "Open >" drills in (GET ?parent=<id>) and pushes a crumb;
//   * a breadcrumb click jumps back up the trail;
//   * highlighting a folder + "Use this folder" fills BOTH the id and name fields
//     and closes the modal;
//   * a Drive-disconnected (409) response surfaces the error in the picker status
//     without throwing;
//   * the manual paste-an-ID + Save flow is untouched (no picker dependency).

const assert = require("node:assert/strict");
const path = require("node:path");

// --- Minimal fake DOM -------------------------------------------------------
class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  toggle(name, force) {
    const on = force === undefined ? !this._set.has(name) : Boolean(force);
    if (on) this._set.add(name);
    else this._set.delete(name);
    return on;
  }
  contains(name) {
    return this._set.has(name);
  }
  add(name) {
    this._set.add(name);
  }
  remove(name) {
    this._set.delete(name);
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.children = [];
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.title = "";
    this._textContent = "";
    this._innerHTML = "";
    this.isConnected = true;
    this._listeners = {};
    this.parentNode = null;
  }
  get textContent() {
    return this._textContent;
  }
  set textContent(value) {
    this._textContent = String(value);
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    if (value === "") this.children = [];
  }
  set className(value) {
    this._className = String(value);
    this.classList = new FakeClassList();
    for (const token of String(value).split(/\s+/).filter(Boolean)) this.classList.add(token);
  }
  get className() {
    return this._className || "";
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this._listeners[type] || [];
    this._listeners[type] = list.filter((fn) => fn !== handler);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  querySelector(selector) {
    const data = /^\[data-admin-drive="(.+)"\]$/.exec(selector);
    if (data) return this._findByData("adminDrive", data[1]) || null;
    const matches = this.querySelectorAll(selector);
    return matches.length ? matches[0] : null;
  }
  querySelectorAll(selector) {
    const cls = /^\.([\w-]+)$/.exec(selector);
    if (cls) {
      const className = cls[1];
      return this.collectDescendants().filter((node) => node.classList.contains(className));
    }
    return [];
  }
  _findByData(key, value) {
    for (const child of this.collectDescendants()) {
      if (child.dataset && child.dataset[key] === value) return child;
    }
    return null;
  }
  collectDescendants() {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  closest() {
    return null;
  }
  async dispatchEvent(event) {
    const handlers = this._listeners[event.type] || [];
    for (const handler of handlers) {
      // eslint-disable-next-line no-await-in-loop
      await handler.call(this, event);
    }
    return true;
  }
  async click() {
    await this.dispatchEvent({ type: "click", target: this, preventDefault() {} });
  }
  async submit() {
    await this.dispatchEvent({ type: "submit", target: this, preventDefault() {} });
  }
}

// A tiny document shim so the controller's createElement/appendChild work.
global.document = {
  createElement(tag) {
    return new FakeElement(tag);
  },
};

// AuthExpired.parseOkJson shim mirroring the real one's contract: throw on
// non-ok, otherwise return the parsed JSON.
global.window = {
  AuthExpired: {
    async parseOkJson(response, fallback, reviewErrorFromPayload) {
      if (!response.ok) {
        let payload = {};
        try {
          payload = await response.json();
        } catch {
          throw new Error(`${fallback} (HTTP ${response.status})`);
        }
        throw reviewErrorFromPayload(payload, fallback);
      }
      return response.json();
    },
  },
};

function copySpan(key) {
  const node = new FakeElement("span");
  node.dataset.adminDrive = key;
  return node;
}

// --- Test scaffolding -------------------------------------------------------
const { createAdminDriveController } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-drive.js")
);

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

async function flush(turns = 20) {
  for (let i = 0; i < turns; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function installFetch(responder) {
  const calls = [];
  global.fetch = async (url, options = {}) => {
    let body;
    try {
      body = options.body ? JSON.parse(options.body) : undefined;
    } catch {
      body = options.body;
    }
    calls.push({ url, method: options.method || "GET", body });
    const result = responder(url, body) || {};
    const ok = result.ok !== false;
    return {
      ok,
      status: result.status || (ok ? 200 : 400),
      statusText: result.statusText || (ok ? "OK" : "Bad Request"),
      async json() {
        return result.payload || {};
      },
    };
  };
  return calls;
}

function mountController() {
  const driveCard = new FakeElement("article");
  const folderMessage = copySpan("folder-message");
  driveCard.appendChild(folderMessage);

  const driveFolderForm = new FakeElement("form");
  const driveFolderIdInput = new FakeElement("input");
  const driveFolderNameInput = new FakeElement("input");
  const driveFolderSaveButton = new FakeElement("button");

  const driveBrowseButton = new FakeElement("button");
  const drivePickerBackdrop = new FakeElement("div");
  drivePickerBackdrop.hidden = true;
  const drivePickerClose = new FakeElement("button");
  const drivePickerCancel = new FakeElement("button");
  const drivePickerSelect = new FakeElement("button");
  const drivePickerList = new FakeElement("ul");
  const drivePickerBreadcrumb = new FakeElement("nav");
  const drivePickerStatus = new FakeElement("p");
  const drivePickerSelection = new FakeElement("span");

  const state = { driveStatus: {} };
  const reviewErrorFromPayload = (payload, fallback) =>
    new Error((payload && payload.error) || fallback);

  const controller = createAdminDriveController({
    state,
    driveCard,
    driveFacts: driveCard,
    driveOverall: new FakeElement("span"),
    driveRefreshButton: new FakeElement("button"),
    driveConnectPanel: new FakeElement("div"),
    driveEnabledToggle: new FakeElement("button"),
    driveFolderForm,
    driveFolderIdInput,
    driveFolderNameInput,
    driveFolderSaveButton,
    driveBrowseButton,
    drivePickerBackdrop,
    drivePickerClose,
    drivePickerCancel,
    drivePickerSelect,
    drivePickerList,
    drivePickerBreadcrumb,
    drivePickerStatus,
    drivePickerSelection,
    reviewErrorFromPayload,
  });

  return {
    controller,
    state,
    driveCard,
    folderMessage,
    driveFolderForm,
    driveFolderIdInput,
    driveFolderNameInput,
    driveFolderSaveButton,
    driveBrowseButton,
    drivePickerBackdrop,
    drivePickerClose,
    drivePickerCancel,
    drivePickerSelect,
    drivePickerList,
    drivePickerBreadcrumb,
    drivePickerStatus,
    drivePickerSelection,
  };
}

// Find the folder <button> for a given display name within the rendered list.
function folderButton(ui, name) {
  return ui.drivePickerList
    .collectDescendants()
    .find((node) => node.classList.contains("drive-picker-folder")
      && node.collectDescendants().some((c) => c.classList.contains("drive-picker-folder-name") && c.textContent === name));
}

function openSpanOf(folderBtn) {
  return folderBtn.collectDescendants().find((c) => c.classList.contains("drive-picker-open"));
}

(async () => {
  await test("Browse opens the modal and lists My Drive root folders", async () => {
    const ui = mountController();
    installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [
          { id: "f_archive", name: "Archive" },
          { id: "f_clients", name: "Clients" },
        ] } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();

    assert.equal(ui.drivePickerBackdrop.hidden, false, "modal opened");
    const names = ui.drivePickerList
      .collectDescendants()
      .filter((n) => n.classList.contains("drive-picker-folder-name"))
      .map((n) => n.textContent);
    assert.deepEqual(names, ["Archive", "Clients"], "root folders rendered");
    // Breadcrumb starts at My Drive.
    const crumbButtons = ui.drivePickerBreadcrumb.collectDescendants().filter((n) => n.tagName === "BUTTON");
    assert.equal(crumbButtons[0].textContent, "My Drive");
  });

  await test('"Open >" drills into a subfolder and pushes a breadcrumb', async () => {
    const ui = mountController();
    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [{ id: "f_clients", name: "Clients" }] } };
      }
      if (url === "/api/admin/drive-folders?parent=f_clients") {
        return { payload: { parent: "f_clients", folders: [{ id: "f_acme", name: "Acme Corp" }] } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();
    const clientsBtn = folderButton(ui, "Clients");
    await openSpanOf(clientsBtn).dispatchEvent({ type: "click", target: openSpanOf(clientsBtn), stopPropagation() {} });
    await flush();

    assert.ok(calls.some((c) => c.url === "/api/admin/drive-folders?parent=f_clients"), "drilled into f_clients");
    const crumbButtons = ui.drivePickerBreadcrumb.collectDescendants().filter((n) => n.tagName === "BUTTON");
    assert.deepEqual(crumbButtons.map((b) => b.textContent), ["My Drive", "Clients"], "breadcrumb pushed");
    // The drilled-into folder is now listed.
    const names = ui.drivePickerList.collectDescendants().filter((n) => n.classList.contains("drive-picker-folder-name")).map((n) => n.textContent);
    assert.deepEqual(names, ["Acme Corp"]);
  });

  await test('selecting + "Use this folder" fills BOTH id and name and closes', async () => {
    const ui = mountController();
    installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [{ id: "f_clients", name: "Clients" }] } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();
    const clientsBtn = folderButton(ui, "Clients");
    // A plain click on the folder body (not the "Open >") selects it.
    await clientsBtn.dispatchEvent({ type: "click", target: clientsBtn, stopPropagation() {} });
    await flush();

    assert.equal(ui.drivePickerSelect.disabled, false, "select enabled after highlight");
    assert.match(ui.drivePickerSelection.textContent, /Clients/, "selection label shows folder");

    await ui.drivePickerSelect.click();
    await flush();

    assert.equal(ui.driveFolderIdInput.value, "f_clients", "id field filled");
    assert.equal(ui.driveFolderNameInput.value, "Clients", "name field filled");
    assert.equal(ui.drivePickerBackdrop.hidden, true, "modal closed after select");
  });

  await test("Drive-disconnected (409) surfaces a status, does not throw", async () => {
    const ui = mountController();
    installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { ok: false, status: 409, payload: { error: "Google Drive is not connected.", needs_connect: true } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();

    assert.equal(ui.drivePickerBackdrop.hidden, false, "modal still opened");
    assert.match(ui.drivePickerStatus.textContent, /not connected/i, "error shown in picker status");
    assert.equal(ui.drivePickerStatus.hidden, false);
  });

  await test("manual paste + Save still works (no picker dependency)", async () => {
    const ui = mountController();
    ui.driveFolderIdInput.value = "1pasted_id";
    ui.driveFolderNameInput.value = "Pasted Folder";
    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-settings") {
        return { payload: { drive: { enabled: true, folder_id: "1pasted_id", folder_name: "Pasted Folder" } } };
      }
      return {};
    });

    await ui.driveFolderForm.submit();
    await flush();

    const saveCall = calls.find((c) => c.url === "/api/admin/drive-settings");
    assert.ok(saveCall, "posted to /api/admin/drive-settings");
    assert.deepEqual(saveCall.body, { folder_id: "1pasted_id", folder_name: "Pasted Folder" }, "manual id+name posted");
  });

  process.stdout.write(`\nadmin-drive-picker.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nadmin-drive-picker.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
