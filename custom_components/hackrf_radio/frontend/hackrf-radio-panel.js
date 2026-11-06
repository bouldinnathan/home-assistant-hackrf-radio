/* Native HA custom panel. No CDN dependencies, build step, or bridge token. */
class HackRFRadioPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._history = [];
    this._radios = [];
    this._generation = 0;
    this._dirty = false;
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this._started) this._start();
  }
  set narrow(value) { this.toggleAttribute("narrow", Boolean(value)); }
  set panel(value) { this._panel = value; }
  connectedCallback() { if (this._hass && !this._started) this._start(); }
  disconnectedCallback() {
    clearTimeout(this._timer);
    this._generation += 1;
    this._started = false;
    this._observer?.disconnect();
  }

  _start() {
    this._started = true;
    this._generation += 1;
    this._settingsKey = null;
    this._radios = [];
    this._dirty = false;
    this._acting = false;
    this._fwActing = false;
    this._firmwareKey = null;
    this._firmwarePlanId = null;
    this._mount();
    this._observer = new ResizeObserver(() => this._draw());
    this._observer.observe(this.shadowRoot.querySelector(".plots"));
    this._tick(this._generation);
  }

  _mount() {
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;height:100%;color:var(--primary-text-color,#e4eaf4);background:var(--primary-background-color,#101722);font:14px var(--paper-font-body1_-_font-family,system-ui,sans-serif)}
        *{box-sizing:border-box}button,input,select{font:inherit;color:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:wait}
        header{height:64px;display:flex;align-items:center;gap:14px;padding:0 24px;border-bottom:1px solid var(--divider-color,#293242);background:var(--card-background-color,#18202c)}
        header h1{font-size:19px;font-weight:600;margin:0}.menu{background:none;border:0;font-size:22px;padding:8px}header span{margin-left:auto;color:var(--secondary-text-color,#9aaabd);font-size:12px}
        main{max-width:1500px;margin:auto;padding:24px} .top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.eyebrow{color:var(--primary-color,#6bd6cc);font-size:11px;font-weight:700;letter-spacing:1.5px}h2{margin:6px 0;font-size:26px;font-weight:500}p{line-height:1.6;color:var(--secondary-text-color,#9aaabd);margin:6px 0}
        select,input{background:var(--primary-background-color,#101722);border:1px solid var(--divider-color,#344255);border-radius:7px;padding:10px;min-width:0;width:100%}#radio{width:min(360px,100%)}
        .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}.stat,.card{background:var(--card-background-color,#18202c);border:1px solid var(--divider-color,#293242);border-radius:12px}.stat{padding:16px}.stat small{color:var(--secondary-text-color,#9aaabd)}.value{font-size:24px;margin-top:8px;font-variant-numeric:tabular-nums}.value span{font-size:12px;color:var(--secondary-text-color,#9aaabd)}
        .layout{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:16px}.card{padding:20px}.card h3{font-size:15px;margin:0 0 14px;font-weight:600}.plothead{display:flex;align-items:center;justify-content:space-between;gap:12px}.badge{font-size:11px;color:var(--primary-color,#6bd6cc);background:var(--secondary-background-color,#20323a);padding:6px 9px;border-radius:20px}.plots{min-width:0}canvas{width:100%;display:block}#spectrum{height:290px}#waterfall{height:180px;background:#101725;border-radius:6px}.legend{font-size:11px;display:flex;justify-content:space-between;gap:10px;color:var(--secondary-text-color,#9aaabd);margin:10px 0 16px}
        .actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}button.action{border:1px solid var(--divider-color,#344255);border-radius:7px;background:var(--secondary-background-color,#253144);padding:10px 13px}.primary{background:var(--primary-color,#6bd6cc)!important;color:var(--text-primary-color,#102322)!important;border-color:transparent!important}#stop{border-color:#bb715a;color:var(--error-color,#efa68e)}
        .firmware-card{margin-top:16px}.firmware-card p,.firmware-card label{font-size:12px}.firmware-card pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;line-height:1.6}.check{display:flex;align-items:flex-start;gap:8px;margin:12px 0;line-height:1.5}.check input{width:auto;margin:2px 0 0}.firmware-card a{color:var(--primary-color,#6bd6cc)}
        form{display:grid;gap:14px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:grid;gap:7px;font-size:12px;color:var(--secondary-text-color,#9aaabd)}.help{font-size:12px}.safety{margin-top:16px;border-left:3px solid var(--primary-color,#6bd6cc);padding:12px 14px;background:var(--secondary-background-color,#20303b);border-radius:4px;font-size:12px;line-height:1.6}.safety strong{display:block}.safety a{color:var(--primary-color,#6bd6cc)}.details{margin-top:16px;font-size:12px;overflow-wrap:anywhere}.error{background:var(--error-color,#b74747);color:white;padding:12px 16px;border-radius:8px;margin-bottom:16px}.empty{padding:45px;text-align:center}.notice{color:var(--warning-color,#e7b969);font-size:12px;margin-top:10px}[hidden]{display:none!important}:focus-visible{outline:2px solid var(--primary-color,#6bd6cc);outline-offset:3px}
        @media(max-width:1000px){.layout{grid-template-columns:1fr}.settings{max-width:none}form{grid-template-columns:1fr 1fr}form h3,form .pair,form button,form .help{grid-column:1/-1}}
        @media(max-width:600px){main{padding:14px}header{padding:0 10px}header span{display:none}.top{align-items:flex-start;flex-direction:column}.stats{grid-template-columns:1fr 1fr}.stat{padding:12px}.value{font-size:21px}.card{padding:14px}#spectrum{height:220px}h2{font-size:23px}form{grid-template-columns:1fr}form>*{grid-column:1/-1}}
      </style>
      <header><button class="menu" aria-label="Open sidebar">☰</button><h1>HackRF Radio</h1><span>Local spectrum monitor</span></header>
      <main>
        <div class="top"><div><div class="eyebrow">RADIO WORKSPACE</div><h2>Explore the spectrum</h2><p>Receive sweeps, signal levels, and activity over time.</p></div><select id="radio" aria-label="Select radio"></select></div>
        <div id="error" class="error" role="alert" hidden></div>
        <div id="empty" class="card empty" hidden><h3>Add your HackRF</h3><p>Open Settings → Devices & services → Add integration → HackRF Radio.</p><p>Use the bridge for Home Assistant OS, or local USB where the HackRF tools are installed.</p><a href="/config/integrations/dashboard/add?domain=hackrf_radio">Set up HackRF Radio</a></div>
        <section id="workspace">
          <div class="stats">
            <div class="stat"><small>Receiver</small><div class="value" id="status">Connecting</div></div>
            <div class="stat"><small>Peak level</small><div class="value"><b id="peak">—</b> <span>dB</span></div></div>
            <div class="stat"><small>Peak frequency</small><div class="value"><b id="frequency">—</b> <span>MHz</span></div></div>
            <div class="stat"><small>Noise floor · median</small><div class="value"><b id="noise">—</b> <span>dB</span></div></div>
          </div>
          <div class="layout">
            <div class="card plots">
              <div class="plothead"><h3>Spectrum</h3><span class="badge">RX ONLY · AMP OFF · BIAS OFF</span></div>
              <canvas id="spectrum" role="img" aria-label="Received spectrum; peak and noise floor are listed above"></canvas>
              <div class="legend"><span id="range">Waiting for first sweep</span><span>Relative FFT power · uncalibrated dB</span></div>
              <h3>Waterfall</h3><canvas id="waterfall" role="img" aria-label="Recent sweeps, newest at the top"></canvas>
              <div class="legend"><span>Newest ↑ · up to 80 sweeps in this browser</span><span>Low ░ ▒ ▓ High</span></div>
              <div class="actions"><button class="action primary" id="sweep">Sweep now</button><button class="action" id="start">Start monitoring</button><button class="action" id="stop">Stop</button></div>
              <p class="help" id="updated" aria-live="polite">A sweep runs once. Monitoring repeats at your chosen interval.</p>
            </div>
            <aside>
              <div class="card settings"><form id="settings"><h3>Receive settings</h3>
                <label>Band preset<select id="preset"><option value="">Custom range</option></select></label>
                <div class="pair"><label>Start · MHz<input name="start_mhz" type="number" min="1" max="5999" step="1" required></label><label>End · MHz<input name="end_mhz" type="number" min="2" max="6000" step="1" required></label></div>
                <label>FFT bin width · Hz<input name="bin_width" type="number" min="2445" max="5000000" step="1" required></label>
                <div class="pair"><label>IF / LNA gain · dB<select name="lna_gain"></select></label><label>Baseband / VGA · dB<select name="vga_gain"></select></label></div>
                <label>Scan interval · seconds<input name="scan_interval" type="number" min="5" max="3600" step="1" required></label>
                <label>Signal threshold · dB<input name="threshold_db" type="number" min="-150" max="0" step="1" required></label>
                <button class="action" id="apply" type="submit">Apply settings</button><p class="help">Applying settings stops monitoring and clears the previous spectrum. Maximum sweep span: 100 MHz.</p>
              </form></div>
              <div class="safety"><strong>Protect the RF input</strong>Keep HackRF One input below −5 dBm. Use suitable attenuation near transmitters and a DC block where external bias may be present. The chart cannot measure or enforce the hardware input limit. <a href="https://hackrf.readthedocs.io/en/latest/hackrf_one.html#maximum-input-power" target="_blank" rel="noopener noreferrer">Hardware guidance ↗</a></div>
              <div class="details" id="device"></div><p class="notice" id="warning"></p>
              <div class="card firmware-card"><h3>Firmware maintenance</h3>
                <p id="firmware-status" role="status" aria-live="polite">Loading firmware capabilities…</p>
                <label>Reviewed release<select id="firmware-target" aria-label="Firmware release"></select></label>
                <div class="actions"><button class="action" id="firmware-prepare">Prepare update</button></div>
                <p class="help">Preparation downloads and checks the image. It does not write to the radio. Nightly/custom firmware needs its upstream update procedure.</p>
                <div id="firmware-review" hidden>
                  <pre id="firmware-plan"></pre>
                  <label>Type the full serial to confirm<input id="firmware-serial" autocomplete="off" spellcheck="false"></label>
                  <label class="check"><input id="firmware-power" type="checkbox">USB power is stable, RF equipment is disconnected, and I will keep the USB host running until completion.</label>
                  <label class="check"><input id="firmware-hardware" type="checkbox">I verified a standard 1 MiB HackRF One or 4 MiB Pro and its firmware family. This is not PortaRF or modified flash hardware. Stock is for a bare HackRF; Mayhem is for its matching PortaPack setup.</label>
                  <label class="check" id="firmware-sd-label"><input id="firmware-sd" type="checkbox">I have prepared the SD-card content matching this Mayhem release.</label>
                  <button class="action" id="firmware-install">Back up and install firmware</button>
                </div>
                <pre id="firmware-backup" hidden></pre>
                <button class="action" id="firmware-verify" hidden>Verify boot after manual reset</button>
                <p class="help">After installation starts, Stop does not interrupt flashing. Power loss or a forced host shutdown can require manual recovery. <a href="https://hackrf.readthedocs.io/en/latest/updating_firmware.html" target="_blank" rel="noopener noreferrer">Official recovery guide ↗</a></p>
              </div>
            </aside>
          </div>
        </section>
      </main>`;
    const root = this.shadowRoot;
    root.querySelector(".menu").onclick = () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    root.querySelector("#radio").onchange = (event) => {
      this._selected = event.target.value;
      this._history = []; this._lastTimestamp = null; this._settingsKey = null;
      this._dirty = false; this._data = null; this._draw();
      this._firmwareKey = null; this._firmwarePlanId = null;
      this._load(this._generation).catch((error) => this._error(error.message));
    };
    for (const action of ["sweep", "start", "stop"]) root.querySelector(`#${action}`).onclick = () => this._control(action);
    for (const action of ["prepare", "install", "verify"]) root.querySelector(`#firmware-${action}`).onclick = () => this._firmware(action);
    for (const id of ["firmware-serial", "firmware-power", "firmware-hardware", "firmware-sd"]) root.querySelector(`#${id}`).oninput = () => this._buttons();
    for (const [name, max, step] of [["lna_gain", 40, 8], ["vga_gain", 62, 2]]) {
      const select = root.querySelector(`[name=${name}]`);
      for (let value = 0; value <= max; value += step) select.add(new Option(String(value), String(value)));
    }
    root.querySelector("#settings").oninput = () => { this._dirty = true; };
    root.querySelector("#preset").onchange = (event) => {
      const range = this._data?.presets[event.target.value];
      if (!range) return;
      root.querySelector("[name=start_mhz]").value = range[0];
      root.querySelector("[name=end_mhz]").value = range[1];
      this._dirty = true;
    };
    root.querySelector("#settings").onsubmit = (event) => {
      event.preventDefault();
      const settings = Object.fromEntries([...new FormData(event.target)].map(([key, value]) => [key, Number(value)]));
      this._control("configure", settings);
    };
  }

  _error(message) {
    const box = this.shadowRoot.querySelector("#error");
    box.textContent = message || "";
    box.hidden = !message;
  }

  async _tick(generation) {
    try { await this._load(generation); }
    catch (error) { if (generation === this._generation) this._error(error.message || "Cannot load HackRF status"); }
    if (generation === this._generation && this.isConnected) this._timer = setTimeout(() => this._tick(generation), 3000);
  }

  async _load(generation) {
    const radios = await this._hass.callWS({ type: "hackrf_radio/list" });
    if (generation !== this._generation) return;
    const root = this.shadowRoot;
    const select = root.querySelector("#radio");
    if (JSON.stringify(radios) !== JSON.stringify(this._radios)) {
      this._radios = radios;
      select.replaceChildren(...radios.map((radio) => new Option(`${radio.name}${radio.loaded ? "" : " · unavailable"}`, radio.entry_id)));
    }
    if (!radios.some((radio) => radio.entry_id === this._selected)) this._selected = radios[0]?.entry_id;
    root.querySelector("#empty").hidden = Boolean(radios.length);
    root.querySelector("#workspace").hidden = !radios.length;
    select.hidden = !radios.length;
    if (!this._selected) return;
    select.value = this._selected;
    const entry = this._selected;
    let data;
    try {
      data = await this._hass.callWS({ type: "hackrf_radio/get", entry_id: entry });
    } catch (error) {
      if (generation === this._generation && entry === this._selected) {
        this._data = null;
        this._history = [];
        this._lastTimestamp = null;
        for (const id of ["peak", "frequency", "noise"]) root.querySelector(`#${id}`).textContent = "—";
        root.querySelector("#status").textContent = "Offline";
        this._buttons(); this._draw();
      }
      throw error;
    }
    if (generation === this._generation && entry === this._selected) this._update(data);
  }

  async _control(action, settings) {
    if (!this._selected || (this._acting && action !== "stop")) return;
    const generation = this._generation;
    const entry = this._selected;
    if (action !== "stop") this._acting = true;
    this._buttons(); this._error("");
    try {
      const data = await this._hass.callWS({ type: "hackrf_radio/control", entry_id: entry, action, ...(settings ? { settings } : {}) });
      if (generation !== this._generation || entry !== this._selected) return;
      if (action === "configure") this._dirty = false;
      this._update(data);
    } catch (error) {
      if (generation === this._generation) this._error(error.message || "Radio operation failed");
    } finally {
      if (generation === this._generation) { if (action !== "stop") this._acting = false; this._buttons(); }
    }
  }

  _buttons() {
    const root = this.shadowRoot;
    const firmware = this._data?.firmware || {};
    const blocked = ["backing_up", "writing", "verifying", "awaiting_reset", "recovery_required"].includes(firmware.phase);
    for (const action of ["sweep", "start", "apply"]) root.querySelector(`#${action}`).disabled = Boolean(!this._data || this._acting || this._fwActing || this._data.busy || blocked);
    root.querySelector("#stop").disabled = !this._selected || Boolean(firmware.active);
    root.querySelector("#firmware-prepare").disabled = Boolean(!this._data || this._acting || this._fwActing || blocked || !root.querySelector("#firmware-target").value);
    const plan = firmware.plan;
    const confirmed = plan && root.querySelector("#firmware-serial").value === plan.serial
      && root.querySelector("#firmware-power").checked && root.querySelector("#firmware-hardware").checked
      && (plan.family !== "mayhem" || root.querySelector("#firmware-sd").checked);
    root.querySelector("#firmware-install").disabled = Boolean(!confirmed || this._fwActing || blocked || this._acting);
    root.querySelector("#firmware-verify").disabled = Boolean(this._fwActing || firmware.active);
  }

  async _firmware(action) {
    if (!this._selected || this._fwActing) return;
    const root = this.shadowRoot;
    const entry = this._selected, generation = this._generation;
    const values = {};
    if (action === "prepare") values.target = root.querySelector("#firmware-target").value;
    if (action === "install") {
      const plan = this._data?.firmware?.plan;
      if (!plan || root.querySelector("#firmware-install").disabled) return;
      values.plan_id = plan.plan_id;
      values.confirmation = {
        serial: root.querySelector("#firmware-serial").value,
        stable_power: root.querySelector("#firmware-power").checked,
        hardware_confirmed: root.querySelector("#firmware-hardware").checked,
        sdcard_updated: root.querySelector("#firmware-sd").checked,
      };
    }
    this._fwActing = true; this._buttons(); this._error("");
    if (action === "prepare") root.querySelector("#firmware-status").textContent = "Downloading and validating the reviewed image…";
    try {
      const data = await this._hass.callWS({ type: "hackrf_radio/firmware", entry_id: entry, action, ...values });
      if (generation === this._generation && entry === this._selected) this._update(data);
    } catch (error) {
      if (generation === this._generation && entry === this._selected) this._error(error.message || "Firmware operation failed");
    } finally {
      if (generation === this._generation) { this._fwActing = false; this._buttons(); }
    }
  }

  _updateFirmware(data) {
    const root = this.shadowRoot;
    const firmware = data.firmware || { phase: "unavailable", catalog: [], active: false };
    root.querySelector("#firmware-status").textContent = `${firmware.phase.replaceAll("_", " ")} · ${firmware.message || "Use a reviewed update for the installed firmware family."}`;
    const current = data.device?.firmware || "";
    const family = /^\d{4}\.\d{2}\.\d+$/.test(current) ? "stock" : /^v?\d{1,2}\.\d+\.\d+$/.test(current) ? "mayhem" : "unknown";
    const releases = (firmware.catalog || []).filter((release) => release.family === family && release.boards.includes(data.device?.board_id));
    const key = JSON.stringify(releases);
    if (key !== this._firmwareKey) {
      this._firmwareKey = key;
      root.querySelector("#firmware-target").replaceChildren(...releases.map((release) => new Option(`${release.model} · ${release.version}`, release.id)));
    }
    const plan = firmware.plan;
    root.querySelector("#firmware-review").hidden = !plan || Boolean(firmware.active);
    if (plan && plan.plan_id !== this._firmwarePlanId) {
      this._firmwarePlanId = plan.plan_id;
      root.querySelector("#firmware-plan").textContent = `${plan.model}\n${plan.current_version} → ${plan.version}\nSerial: ${plan.serial}\nImage: ${plan.size} bytes\nSHA256: ${plan.sha256}\nPlan expires: ${new Date(plan.expires_at * 1000).toLocaleTimeString()}${plan.warning ? "\n" + plan.warning : ""}`;
      root.querySelector("#firmware-serial").value = "";
      for (const id of ["firmware-power", "firmware-hardware", "firmware-sd"]) root.querySelector(`#${id}`).checked = false;
      root.querySelector("#firmware-sd-label").hidden = plan.family !== "mayhem";
    }
    root.querySelector("#firmware-verify").hidden = firmware.phase !== "awaiting_reset";
    root.querySelector("#firmware-backup").hidden = !firmware.backup;
    root.querySelector("#firmware-backup").textContent = firmware.backup ? `Backup on USB host:\n${firmware.backup}\nSHA256: ${firmware.backup_sha256}` : "";
  }

  _update(data) {
    this._data = data;
    const root = this.shadowRoot;
    const set = (id, value) => { root.querySelector(`#${id}`).textContent = value; };
    this._error(data.error);
    set("status", !data.connected ? "Offline" : data.busy ? "Sweeping" : data.monitoring ? "Monitoring" : "Idle");
    const spectrum = data.spectrum;
    const reading = (key) => data.fresh && spectrum ? spectrum[key].toFixed(2) : "—";
    set("peak", reading("peak_db")); set("frequency", reading("peak_frequency_mhz")); set("noise", reading("noise_floor_db"));
    set("range", `${data.settings.start_mhz}–${data.settings.end_mhz} MHz · ${spectrum?.bin_count || 0} bins`);
    set("updated", spectrum ? `Last sweep: ${new Date(spectrum.timestamp).toLocaleString()}${data.fresh ? "" : " · stale, take a new sweep"}` : "Ready for your first receive sweep.");
    const key = JSON.stringify(data.settings);
    if (this._settingsKey !== key) {
      this._history = []; this._lastTimestamp = null; this._settingsKey = key;
      if (!this._dirty) for (const [name, value] of Object.entries(data.settings)) root.querySelector(`[name=${name}]`).value = String(value);
      const preset = root.querySelector("#preset");
      preset.replaceChildren(new Option("Custom range", ""), ...Object.keys(data.presets).map((name) => new Option(name, name)));
    }
    if (spectrum && spectrum.timestamp !== this._lastTimestamp) {
      this._lastTimestamp = spectrum.timestamp;
      this._history.unshift(spectrum);
      this._history.length = Math.min(80, this._history.length);
    }
    const device = data.device;
    set("device", device ? `${device.model} · ${device.revision}\nFirmware ${device.firmware} · API ${device.api}\nSerial ${data.serial}` : "Device details unavailable");
    set("warning", device?.warning || "");
    this._updateFirmware(data);
    this._buttons(); this._draw();
  }

  _context(id) {
    const canvas = this.shadowRoot.querySelector(`#${id}`);
    if (!canvas) return null;
    const width = Math.max(1, canvas.clientWidth), height = Math.max(1, canvas.clientHeight);
    const scale = window.devicePixelRatio || 1;
    canvas.width = width * scale; canvas.height = height * scale;
    const context = canvas.getContext("2d");
    context.scale(scale, scale);
    return { context, width, height };
  }

  _draw() {
    if (!this.isConnected || !this.shadowRoot.querySelector("#spectrum")) return;
    const { context: ctx, width, height } = this._context("spectrum");
    const spectrum = this._data?.spectrum;
    const foreground = getComputedStyle(this).getPropertyValue("--secondary-text-color").trim() || "#9aaabd";
    const accent = getComputedStyle(this).getPropertyValue("--primary-color").trim() || "#6bd6cc";
    const left = 42, right = width - 12, top = 12, bottom = height - 26;
    ctx.font = "11px system-ui"; ctx.lineWidth = 1;
    for (let level = -120; level <= 0; level += 30) {
      const y = bottom - ((level + 120) / 120) * (bottom - top);
      ctx.strokeStyle = "rgba(130,150,175,.16)"; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.fillStyle = foreground; ctx.fillText(String(level), 6, y + 4);
    }
    if (!spectrum) { ctx.fillStyle = foreground; ctx.fillText("Take a sweep to see the spectrum", left + 15, height / 2); }
    else {
      const { start_mhz: start, end_mhz: end } = spectrum.settings;
      const x = (hz) => left + ((hz / 1e6 - start) / (end - start)) * (right - left);
      const y = (power) => bottom - ((Math.min(0, Math.max(-120, power)) + 120) / 120) * (bottom - top);
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.beginPath();
      spectrum.frequencies.forEach((frequency, index) => { if (!index) ctx.moveTo(x(frequency), y(spectrum.powers[index])); else ctx.lineTo(x(frequency), y(spectrum.powers[index])); });
      ctx.stroke(); ctx.fillStyle = foreground; ctx.fillText(`${start} MHz`, left, height - 5); ctx.textAlign = "right"; ctx.fillText(`${end} MHz`, right, height - 5); ctx.textAlign = "left";
    }
    const { context: water, width: ww, height: wh } = this._context("waterfall");
    water.fillStyle = "#101725"; water.fillRect(0, 0, ww, wh);
    for (let row = 0; row < this._history.length; row++) {
      const capture = this._history[row];
      const { start_mhz: start, end_mhz: end } = capture.settings;
      capture.powers.forEach((power, index) => {
        const strength = Math.max(0, Math.min(1, (power + 100) / 75));
        water.fillStyle = `hsl(${230 - strength * 200} 85% ${9 + strength * 56}%)`;
        const x = ((capture.frequencies[index] / 1e6 - start) / (end - start)) * ww;
        const next = index + 1 < capture.frequencies.length ? ((capture.frequencies[index + 1] / 1e6 - start) / (end - start)) * ww : ww;
        water.fillRect(x, row * wh / 80, Math.max(1, next - x), Math.ceil(wh / 80));
      });
    }
  }
}
if (!customElements.get("hackrf-radio-panel")) customElements.define("hackrf-radio-panel", HackRFRadioPanel);
