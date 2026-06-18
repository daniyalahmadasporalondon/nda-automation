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
//   * highlighting a folder + "Use this folder" fills the id field, captures the
//     real name, and closes the modal;
//   * a Drive-disconnected (409) response surfaces the error in the picker status
//     without throwing;
//   * the manual paste-an-ID + Save flow is untouched (no picker dependency).
//
// Folder UX improvements covered here too:
//   (A) the manual "Root folder name" input is GONE — the controller no longer
//       takes driveFolderNameInput, yet a picker-based Save still posts the
//       captured folder_name; a hand-typed id posts without a stale folder_name;
//   (B) "+ New folder" POSTs {parent, name}; on {id, name} it adds + selects the
//       new folder (filling the id field) and surfaces create errors inline.

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
  const driveFolderSaveButton = new FakeElement("button");

  const driveBrowseButton = new FakeElement("button");
  const drivePickerBackdrop = new FakeElement("div");
  drivePickerBackdrop.hidden = true;
  const drivePickerClose = new FakeElement("button");
  const drivePickerCancel = new FakeElement("button");
  const drivePickerSelect = new FakeElement("button");
  const drivePickerList = new FakeElement("ul");
  const drivePickerBreadcrumb = new FakeElement("nav");
  const drivePickerBack = new FakeElement("button");
  // Mirrors the shipped markup: Back starts disabled (My Drive root).
  drivePickerBack.disabled = true;
  const drivePickerStatus = new FakeElement("p");
  const drivePickerSelection = new FakeElement("span");

  // "+ New folder" controls.
  const drivePickerNewToggle = new FakeElement("button");
  const drivePickerNewRow = new FakeElement("div");
  drivePickerNewRow.hidden = true;
  const drivePickerNewInput = new FakeElement("input");
  const drivePickerNewCreate = new FakeElement("button");
  const drivePickerNewCancel = new FakeElement("button");
  const drivePickerNewError = new FakeElement("p");
  drivePickerNewError.hidden = true;

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
    driveFolderSaveButton,
    driveBrowseButton,
    drivePickerBackdrop,
    drivePickerClose,
    drivePickerCancel,
    drivePickerSelect,
    drivePickerList,
    drivePickerBreadcrumb,
    drivePickerBack,
    drivePickerStatus,
    drivePickerSelection,
    drivePickerNewToggle,
    drivePickerNewRow,
    drivePickerNewInput,
    drivePickerNewCreate,
    drivePickerNewCancel,
    drivePickerNewError,
    reviewErrorFromPayload,
  });

  return {
    controller,
    state,
    driveCard,
    folderMessage,
    driveFolderForm,
    driveFolderIdInput,
    driveFolderSaveButton,
    driveBrowseButton,
    drivePickerBackdrop,
    drivePickerClose,
    drivePickerCancel,
    drivePickerSelect,
    drivePickerList,
    drivePickerBreadcrumb,
    drivePickerBack,
    drivePickerStatus,
    drivePickerSelection,
    drivePickerNewToggle,
    drivePickerNewRow,
    drivePickerNewInput,
    drivePickerNewCreate,
    drivePickerNewCancel,
    drivePickerNewError,
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

  await test('"← Back" is disabled at My Drive root, enabled after drilling, and navigates up one level', async () => {
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
    // At My Drive root there is nowhere up to go: Back is disabled.
    assert.equal(ui.drivePickerBack.disabled, true, "Back disabled at root");

    // Drill into a subfolder; Back becomes available.
    const clientsBtn = folderButton(ui, "Clients");
    await openSpanOf(clientsBtn).dispatchEvent({ type: "click", target: openSpanOf(clientsBtn), stopPropagation() {} });
    await flush();
    assert.equal(ui.drivePickerBack.disabled, false, "Back enabled after drilling in");

    // Clicking Back re-lists the parent (root) and pops the breadcrumb tail.
    const callsBefore = calls.length;
    await ui.drivePickerBack.click();
    await flush();
    assert.equal(
      calls[calls.length - 1].url,
      "/api/admin/drive-folders?parent=root",
      "Back re-lists the parent folder via the same drive-folders call"
    );
    assert.ok(calls.length > callsBefore, "Back triggered a folder list fetch");
    const crumbButtons = ui.drivePickerBreadcrumb.collectDescendants().filter((n) => n.tagName === "BUTTON");
    assert.deepEqual(crumbButtons.map((b) => b.textContent), ["My Drive"], "breadcrumb popped back to root");
    // Root content is shown again and Back is disabled once more.
    const names = ui.drivePickerList.collectDescendants().filter((n) => n.classList.contains("drive-picker-folder-name")).map((n) => n.textContent);
    assert.deepEqual(names, ["Clients"], "parent (root) folders re-rendered");
    assert.equal(ui.drivePickerBack.disabled, true, "Back disabled again at root");
  });

  await test('"← Back" at root is a no-op (defensive guard, no fetch)', async () => {
    const ui = mountController();
    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [] } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();
    const before = calls.length;
    // Even if invoked while disabled, the handler must not navigate above root.
    await ui.drivePickerBack.click();
    await flush();
    assert.equal(calls.length, before, "Back at root issues no additional fetch");
  });

  await test('selecting + "Use this folder" fills the id, captures the name (no name input), and saving posts folder_name', async () => {
    const ui = mountController();
    // The manual name field was removed: the controller must not require it.
    assert.equal(ui.driveFolderNameInput, undefined, "no manual name input in the harness");

    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [{ id: "f_clients", name: "Clients" }] } };
      }
      if (url === "/api/admin/drive-settings") {
        return { payload: { drive: { enabled: true, folder_id: "f_clients", folder_name: "Clients" } } };
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
    assert.equal(ui.drivePickerBackdrop.hidden, true, "modal closed after select");

    // Saving carries the captured name even though there is no name input.
    await ui.driveFolderForm.submit();
    await flush();
    const saveCall = calls.find((c) => c.url === "/api/admin/drive-settings");
    assert.ok(saveCall, "posted to /api/admin/drive-settings");
    assert.deepEqual(saveCall.body, { folder_id: "f_clients", folder_name: "Clients" }, "picker-captured id+name posted");
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

  await test("manual paste + Save still works and omits a stale folder_name", async () => {
    const ui = mountController();
    // A hand-typed id has no captured name: the save posts only folder_id, and
    // the banner resolves the real name server-side.
    ui.driveFolderIdInput.value = "1pasted_id";
    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-settings") {
        return { payload: { drive: { enabled: true, folder_id: "1pasted_id", folder_name: "Resolved" } } };
      }
      return {};
    });

    await ui.driveFolderForm.submit();
    await flush();

    const saveCall = calls.find((c) => c.url === "/api/admin/drive-settings");
    assert.ok(saveCall, "posted to /api/admin/drive-settings");
    assert.deepEqual(saveCall.body, { folder_id: "1pasted_id" }, "manual id posted without a stale name");
  });

  await test('"+ New folder" POSTs {parent, name}, then adds + selects + fills the form', async () => {
    const ui = mountController();
    const calls = installFetch((url, body) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [{ id: "f_clients", name: "Clients" }] } };
      }
      if (url === "/api/admin/drive-folders") {
        // The create POST. Echo back a created id + name.
        return { payload: { id: "f_new", name: body.name } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();

    // Reveal the inline input, type a name, click Create.
    await ui.drivePickerNewToggle.click();
    await flush();
    assert.equal(ui.drivePickerNewRow.hidden, false, "new-folder input revealed");
    ui.drivePickerNewInput.value = "Project X";
    await ui.drivePickerNewCreate.click();
    await flush();

    const createCall = calls.find((c) => c.url === "/api/admin/drive-folders" && c.method === "POST");
    assert.ok(createCall, "POSTed to /api/admin/drive-folders");
    assert.deepEqual(createCall.body, { parent: "root", name: "Project X" }, "posted {parent, name}");

    // The new folder is added to the list, selected, and the id field filled.
    const newBtn = folderButton(ui, "Project X");
    assert.ok(newBtn, "new folder rendered in the list");
    assert.ok(newBtn.classList.contains("selected"), "new folder highlighted");
    assert.equal(ui.driveFolderIdInput.value, "f_new", "id field filled with the new folder id");
    assert.equal(ui.drivePickerNewRow.hidden, true, "new-folder input closed");

    // And the captured name flows into a subsequent save.
    const saveCalls = installFetch((url) => {
      if (url === "/api/admin/drive-settings") {
        return { payload: { drive: { enabled: true, folder_id: "f_new", folder_name: "Project X" } } };
      }
      return {};
    });
    await ui.driveFolderForm.submit();
    await flush();
    const saveCall = saveCalls.find((c) => c.url === "/api/admin/drive-settings");
    assert.deepEqual(saveCall.body, { folder_id: "f_new", folder_name: "Project X" }, "created folder name carried into save");
  });

  await test('"+ New folder" surfaces a create error inline without throwing', async () => {
    const ui = mountController();
    installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [] } };
      }
      if (url === "/api/admin/drive-folders") {
        return { ok: false, status: 409, payload: { error: "Google Drive is not connected." } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();
    await ui.drivePickerNewToggle.click();
    await flush();
    ui.drivePickerNewInput.value = "Doomed";
    await ui.drivePickerNewCreate.click();
    await flush();

    assert.match(ui.drivePickerNewError.textContent, /not connected/i, "create error shown inline");
    assert.equal(ui.drivePickerNewError.hidden, false, "error visible");
    // The input row stays open so the admin can retry; nothing was filled/closed.
    assert.equal(ui.drivePickerNewRow.hidden, false, "new-folder input stays open on error");
    assert.equal(ui.driveFolderIdInput.value, "", "id field untouched on error");
    assert.equal(ui.drivePickerBackdrop.hidden, false, "modal stays open on error");
  });

  await test('"+ New folder" rejects an empty name without POSTing', async () => {
    const ui = mountController();
    const calls = installFetch((url) => {
      if (url === "/api/admin/drive-folders?parent=root") {
        return { payload: { parent: "root", folders: [] } };
      }
      return {};
    });

    await ui.driveBrowseButton.click();
    await flush();
    await ui.drivePickerNewToggle.click();
    await flush();
    ui.drivePickerNewInput.value = "   ";
    await ui.drivePickerNewCreate.click();
    await flush();

    assert.ok(!calls.some((c) => c.url === "/api/admin/drive-folders" && c.method === "POST"), "no create POST for an empty name");
    assert.match(ui.drivePickerNewError.textContent, /name/i, "empty-name error shown");
  });

  process.stdout.write(`\nadmin-drive-picker.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nadmin-drive-picker.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
