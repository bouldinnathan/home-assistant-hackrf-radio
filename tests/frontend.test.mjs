import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";

const code = await readFile(new URL("../custom_components/hackrf_radio/frontend/hackrf-radio-panel.js", import.meta.url), "utf8");
const settings = { start_mhz: 430, end_mhz: 440, bin_width: 100000, lna_gain: 16, vga_gain: 16, scan_interval: 15, threshold_db: -40 };
const sample = {
  entry_id: "radio1", name: "Workbench", serial: "a".repeat(32), connected: true, monitoring: false, fresh: true,
  settings, busy: false, error: null, presets: { "433 MHz": [430, 440], "915 MHz": [902, 928] },
  device: { board_id: 2, model: "HackRF One", revision: "r10", firmware: "n_250606", api: "1.08", warning: "" },
  spectrum: { timestamp: new Date().toISOString(), settings, frequencies: [430500000, 431500000], powers: [-65, -30], peak_db: -30, noise_floor_db: -65, peak_frequency_mhz: 431.5, bin_count: 2 },
};
const flush = () => new Promise(setImmediate);

async function mount(t, overrides = {}) {
  const window = new Window({ url: "http://localhost/" });
  // Happy DOM omits the browser's legacy Option constructor.
  window.Option = function(text, value) {
    const option = window.document.createElement("option");
    option.textContent = text;
    option.value = value;
    return option;
  };
  const context = new Proxy({}, { get: () => () => {} });
  window.HTMLCanvasElement.prototype.getContext = () => context;
  window.eval(code);
  const panel = window.document.createElement("hackrf-radio-panel");
  const calls = [];
  const callWS = async (message) => {
    calls.push(message);
    if (overrides.callWS) return overrides.callWS(message);
    if (message.type.endsWith("/list")) return [{ entry_id: "radio1", name: overrides.name || "Workbench", loaded: true }];
    return structuredClone(overrides.data || sample);
  };
  panel.hass = { callWS };
  window.document.body.append(panel);
  await flush();
  t.after(() => { panel.remove(); window.happyDOM.abort(); });
  return { window, panel, root: panel.shadowRoot, calls };
}

test("panel loads receive controls, settings, and escaped device data", async (t) => {
  const attack = '<img src=x onerror="alert(1)">';
  const data = structuredClone(sample);
  data.device.model = attack;
  data.device.warning = attack;
  const { root, calls } = await mount(t, { data, name: attack });
  assert.equal(root.querySelector("#peak").textContent, "-30.00");
  assert.equal(root.querySelector("[name=lna_gain]").value, "16");
  assert.equal(root.querySelector("#radio option").textContent, attack);
  assert.equal(root.querySelector("#warning").textContent, attack);
  assert.equal(root.querySelector("img"), null);
  assert.match(root.querySelector(".badge").textContent, /RX ONLY.*AMP OFF.*BIAS OFF/);
  assert.ok(calls.every((message) => !JSON.stringify(message).includes("token")));
});

test("stop remains usable during an in-flight sweep", async (t) => {
  let resolveSweep;
  const { panel, calls, root } = await mount(t, { callWS: async (message) => {
    if (message.type.endsWith("/list")) return [{ entry_id: "radio1", name: "Test", loaded: true }];
    if (message.action === "sweep") return new Promise((resolve) => { resolveSweep = resolve; });
    return structuredClone(sample);
  } });
  const sweep = panel._control("sweep");
  assert.equal(root.querySelector("#sweep").disabled, true);
  assert.equal(root.querySelector("#stop").disabled, false);
  await panel._control("stop");
  assert.equal(calls.at(-1).action, "stop");
  resolveSweep(structuredClone(sample));
  await sweep;
  assert.equal(root.querySelector("#sweep").disabled, false);
});

test("preset and form submit send numeric settings without power overrides", async (t) => {
  const { root, window, calls } = await mount(t);
  const preset = root.querySelector("#preset");
  preset.value = "915 MHz";
  preset.dispatchEvent(new window.Event("change"));
  assert.equal(root.querySelector("[name=start_mhz]").value, "902");
  root.querySelector("#settings").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await flush();
  const sent = calls.find((message) => message.action === "configure");
  assert.equal(sent.settings.start_mhz, 902);
  assert.equal(sent.settings.end_mhz, 928);
  assert.deepEqual(Object.keys(sent.settings).sort(), Object.keys(settings).sort());
  assert.ok(Object.values(sent.settings).every(Number.isInteger));
});

test("empty and disconnected radios have clear states", async (t) => {
  const { root } = await mount(t, { callWS: async () => [] });
  assert.equal(root.querySelector("#empty").hidden, false);
  assert.equal(root.querySelector("#workspace").hidden, true);
});

test("reattaching restores form and radio choices", async (t) => {
  const { window, panel, root } = await mount(t);
  panel.remove();
  window.document.body.append(panel);
  await flush();
  assert.equal(root.querySelector("#radio").value, "radio1");
  assert.equal(root.querySelector("[name=start_mhz]").value, "430");
});

test("stale readings remain visibly stale and disconnect clears active values", async (t) => {
  const { panel, root } = await mount(t, { data: { ...structuredClone(sample), fresh: false } });
  assert.equal(root.querySelector("#peak").textContent, "—");
  assert.match(root.querySelector("#updated").textContent, /stale/);
  panel._hass.callWS = async (message) => {
    if (message.type.endsWith("/list")) return [{ entry_id: "radio1", name: "Test", loaded: false }];
    throw new Error("Radio is not loaded");
  };
  await assert.rejects(panel._load(panel._generation), /not loaded/);
  assert.equal(root.querySelector("#status").textContent, "Offline");
  assert.equal(root.querySelector("#sweep").disabled, true);
});

function firmwareSample(family = "stock") {
  const data = structuredClone(sample);
  data.device.firmware = family === "stock" ? "2024.02.1" : "v2.3.2";
  data.firmware = {
    phase: "idle", active: false,
    catalog: [{ id: "reviewed-one", family, model: "HackRF One", version: "2026.01.3", boards: [2, 4] }],
    plan: { plan_id: "plan1", serial: data.serial, family, model: "HackRF One", current_version: data.device.firmware,
      version: "2026.01.3", size: 45720, sha256: "b".repeat(64), expires_at: Date.now() / 1000 + 600 },
  };
  return data;
}

test("firmware installation requires exact serial and explicit hardware/power review", async (t) => {
  const data = firmwareSample();
  const { root, panel, calls } = await mount(t, { data });
  assert.equal(root.querySelector("#firmware-review").hidden, false);
  assert.equal(root.querySelector("#firmware-install").disabled, true);
  await panel._firmware("install");
  assert.equal(calls.filter((message) => message.action === "install").length, 0);
  root.querySelector("#firmware-serial").value = data.serial;
  root.querySelector("#firmware-power").checked = true;
  panel._buttons();
  assert.equal(root.querySelector("#firmware-install").disabled, true);
  root.querySelector("#firmware-hardware").checked = true;
  panel._buttons();
  assert.equal(root.querySelector("#firmware-install").disabled, false);
  await panel._firmware("install");
  const command = calls.find((message) => message.action === "install");
  assert.equal(command.type, "hackrf_radio/firmware");
  assert.equal(command.plan_id, "plan1");
  assert.equal(command.confirmation.serial, data.serial);
  assert.equal(command.confirmation.hardware_confirmed, true);
});

test("Mayhem requires SD-card preparation and resets consent on a new plan", async (t) => {
  const data = firmwareSample("mayhem");
  const { root, panel } = await mount(t, { data });
  root.querySelector("#firmware-serial").value = data.serial;
  root.querySelector("#firmware-power").checked = root.querySelector("#firmware-hardware").checked = true;
  panel._buttons();
  assert.equal(root.querySelector("#firmware-sd-label").hidden, false);
  assert.equal(root.querySelector("#firmware-install").disabled, true);
  root.querySelector("#firmware-sd").checked = true;
  panel._buttons();
  assert.equal(root.querySelector("#firmware-install").disabled, false);
  data.firmware.plan.plan_id = "replacement-plan";
  panel._update(data);
  assert.equal(root.querySelector("#firmware-serial").value, "");
  assert.equal(root.querySelector("#firmware-sd").checked, false);
  assert.equal(root.querySelector("#firmware-install").disabled, true);
});

test("active flashing blocks receive actions and exposes boot verification afterward", async (t) => {
  const data = firmwareSample();
  data.firmware.phase = "writing"; data.firmware.active = true; data.firmware.plan = null;
  const { root, panel } = await mount(t, { data });
  for (const id of ["sweep", "start", "stop", "firmware-prepare"]) assert.equal(root.querySelector(`#${id}`).disabled, true);
  assert.equal(root.querySelector("#firmware-verify").hidden, true);
  data.firmware.phase = "awaiting_reset"; data.firmware.active = false;
  panel._update(data);
  assert.equal(root.querySelector("#firmware-verify").hidden, false);
  assert.equal(root.querySelector("#sweep").disabled, true);
});

test("firmware preparation sends only catalog ID and never starts installation", async (t) => {
  const { panel, calls } = await mount(t, { data: firmwareSample() });
  await panel._firmware("prepare");
  const command = calls.find((message) => message.type === "hackrf_radio/firmware");
  assert.equal(command.action, "prepare");
  assert.equal(command.target, "reviewed-one");
  assert.ok(!("confirmation" in command));
  assert.equal(calls.some((message) => message.action === "install"), false);
});
