/* Shared helpers for the kiosk and admin screens. Plain browser JavaScript, no build step. */
(function () {
  "use strict";

  const store = {
    get(key, fallback = "") {
      try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; }
    },
    set(key, value) {
      try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    },
    getSession(key) {
      try { return sessionStorage.getItem(key) || ""; } catch (e) { return ""; }
    },
    setSession(key, value) {
      try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (e) { /* ignore */ }
    },
  };

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data || {};
    }
  }

  // crypto.randomUUID() needs HTTPS; iPads reach the main computer over plain http on the LAN.
  function uuid() {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function api(method, path, body, options = {}) {
    const headers = { "Accept": "application/json" };
    const station = store.get("checkin.station");
    if (station) headers["X-Station-Token"] = station;
    const token = options.token !== undefined ? options.token : store.get("checkin.session");
    if (token) headers["Authorization"] = "Bearer " + token;
    let payload;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 8000);
    let response;
    try {
      response = await fetch(path, { method, headers, body: payload, signal: controller.signal, cache: "no-store" });
    } catch (err) {
      throw new ApiError("Cannot reach the main computer.", 0, { network: true });
    } finally {
      clearTimeout(timer);
    }
    const type = response.headers.get("Content-Type") || "";
    if (options.raw) {
      if (!response.ok) throw new ApiError("Request failed", response.status);
      return response;
    }
    const data = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      throw new ApiError((data && data.error) || `Error ${response.status}`, response.status, data && data.data);
    }
    return data;
  }

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === "class") el.className = value;
        else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
        else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
        else if (key === "html") el.innerHTML = value;
        else if (key in el && typeof value !== "string") el[key] = value;
        else el.setAttribute(key, value === true ? "" : value);
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // A labelled field. The label points at the input (or the first control in a group), so screen readers
  // name it and tapping the label focuses it.
  let fieldCount = 0;
  function field(labelText, input, hint) {
    const control = input instanceof Element
      ? (input.matches("input, select, textarea") ? input : input.querySelector("input, select, textarea")) : null;
    const label = h("label", null, labelText, hint ? h("span", { class: "hint" }, " " + hint) : null);
    if (control) {
      if (!control.id) control.id = "field-" + ++fieldCount;
      label.htmlFor = control.id;
    }
    return h("div", null, label, input);
  }

  // One of the line icons in icons.svg, drawn in the text colour. Decorative: the words beside it say what it means.
  function icon(name) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", "/icons.svg#" + name);
    svg.append(use);
    return svg;
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  let toastWrap;
  function toast(message, kind = "", ms = 3200) {
    if (!toastWrap) {
      toastWrap = h("div", { class: "toast-wrap" });
      document.body.append(toastWrap);
    }
    const el = h("div", { class: "toast " + kind }, message);
    toastWrap.append(el);
    setTimeout(() => el.remove(), ms);
  }

  function download(content, filename, type = "text/csv;charset=utf-8") {
    const blob = content instanceof Blob ? content : new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // iOS Safari shows :active (the pressed look) only once the page listens for touches.
  document.addEventListener("touchstart", () => {}, { passive: true });

  window.HB = { store, api, ApiError, uuid, h, field, icon, clear, toast, download, fileToBase64, esc };
})();
