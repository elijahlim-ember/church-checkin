/* Kiosk screen: key in number -> pick who is here -> Check in & print. Runs on the iPads and the main computer. */
(function () {
  "use strict";
  const { api, h, field, icon, clear, toast, store, uuid } = HB;
  const { t, tn, sv } = KS;   // the words are all in kiosk-strings.js
  const CODE_KEYS = "ACDEFGHJKMNPQRTWXY34679".split("");
  // The reason is recorded in English in the audit log whatever language the screen is in; t("undo_r1".."undo_r4") shows it.
  const UNDO_REASONS = ["Wrong child selected", "Wrong family", "Child went home / not staying", "Test check-in"];

  const S = {
    status: null,
    matched: {},
    online: true,
    fails: 0,
    lastOk: 0,
    screen: "boot",
    sessionId: Number(store.get("checkin.sessionId")) || null,
    digits: "",
    household: null,
    householdId: null,
    selected: new Set(),
    txnId: null,
    busy: false,
    lastActivity: Date.now(),
    changeSeq: -1,
    householdSignature: null,
    pending: readPending(),
    pollToken: 0,
    authEpoch: 0,
  };

  const main = document.getElementById("main");
  const bar = document.getElementById("bar");
  let barKey = "";

  function readPending() {
    try { return JSON.parse(store.get("checkin.pending") || "null"); } catch (e) { return null; }
  }
  function setPending(value) {
    S.pending = value;
    store.set("checkin.pending", value ? JSON.stringify(value) : null);
  }
  function settings() { return (S.status && S.status.settings) || {}; }
  // individual (default): everyone checks in, only the person who searched starts ticked.
  // family: everyone checks in, the whole family starts ticked. children: children only.
  function mode() { return settings().checkin_mode || (settings().checkin_everyone === false ? "children" : "individual"); }
  function everyone() { return mode() !== "children"; }
  // Pickup codes, parent tags and check-out are off unless switched on in Admin → Settings.
  function pickupOn() { return settings().pickup_codes === true; }
  function session() {
    const list = (S.status && S.status.sessions) || [];
    return list.find((s) => s.id === S.sessionId && s.status === "open") || null;
  }
  function stationPrinter() { return S.status && S.status.station ? S.status.station.printer : null; }
  function printers() { return (S.status && S.status.printers) || []; }

  // ---------------------------------------------------------------------------
  // Screen plumbing
  // ---------------------------------------------------------------------------
  function show(name, ...content) {
    if (["lock", "register", "first-run", "setup"].includes(name)) barKey = "";
    S.screen = name;
    S.pollToken++;
    S.keyHandler = null;
    clear(main).append(h("div", { class: "screen screen-" + name }, ...content));
    main.scrollTop = 0;
    renderBar();
  }

  function dialog(title, body, buttons) {
    const overlay = h("div", { class: "overlay" });
    const close = () => overlay.remove();
    const box = h("div", { class: "dialog" }, h("h2", null, title), body,
      h("div", { class: "btn-row" }, buttons.map((b) => h("button", {
        class: "btn big " + (b.class || ""),
        onclick: async (e) => {
          if (e.currentTarget.disabled) return;
          e.currentTarget.disabled = true;
          try {
            if (b.onclick) {
              const keep = await b.onclick(close);
              if (keep === true) { e.currentTarget.disabled = false; return; }
            }
            close();
          } catch (err) {
            e.currentTarget.disabled = false;
            throw err;
          }
        },
      }, b.label))));
    overlay.append(box);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.body.append(overlay);
    return close;
  }

  // The main computer files a child with no class under "Unassigned" and adults under "Adults": words it makes up,
  // not class names someone typed, so they are shown in the chosen language.
  function className(name) {
    return name === "Unassigned" ? t("unassigned") : name === "Adults" ? t("adults") : name;
  }

  function nodes(...items) {
    return items.filter((x) => x !== null && x !== undefined && x !== false);
  }

  function errorText(err) {
    return sv((err && err.message) || "Something went wrong.");
  }

  // ---------------------------------------------------------------------------
  // Status bar and heartbeat
  // ---------------------------------------------------------------------------
  function renderBar() {
    const st = S.status;
    const sess = session();
    const mine = stationPrinter();
    const problems = st ? st.print_problems || 0 : 0;
    const renderedPrinters = st ? printers().filter((p) => p.name === mine || p.ok === false).map((p) =>
      [p.name, p.ok, p.state, p.message]) : [];
    const key = JSON.stringify([st && st.station && st.station.name, st && st.user, sess && [sess.name, sess.start_time],
      S.online, renderedPrinters, st && st.internet && st.internet.ok, problems, KS.lang(), settings().chinese_button]);
    if (key === barKey) return;
    barKey = "";
    clear(bar);
    barKey = key;
    bar.append(h("span", { class: "brand" }, h("img", { class: "brand-mark", src: "/brand-mark.png", alt: "" }), t("brand")));
    if (!st) return;
    // Shows the other language's name. The choice is kept on this device (store "checkin.lang"), not reset when the kiosk idles.
    if (settings().chinese_button === true) {
      bar.append(h("button", { class: "chip lang", lang: KS.lang() === "zh" ? "en" : "zh-Hans", "aria-label": t("lang_switch_label"),
        onclick: switchLanguage }, t("lang_switch")));
    }
    if (st.station) bar.append(h("span", { class: "chip hide-narrow" }, icon("pin"), st.station.name));
    if (st.user) {
      bar.append(h("button", { class: "chip" + (sess ? "" : " warn"), onclick: chooseSession },
        sess ? `${sess.name}${sess.start_time ? " " + sess.start_time : ""}` : t("no_service_open")));
    }
    const status = h("div", { class: "bar-status" });
    status.append(h("span", { class: "chip" + (S.online ? "" : " bad") },
      h("span", { class: "dot " + (S.online ? "ok" : "bad") }), S.online ? t("server") : t("offline")));
    for (const p of printers()) {
      if (p.name !== mine && p.ok !== false) continue;
      const cls = !S.online ? "" : p.ok === false ? "bad" : p.ok ? "ok" : "warn";
      status.append(h("span", { class: "chip" + (p.ok === false ? " bad" : ""), title: p.message },
        h("span", { class: "dot " + cls }), p.name, p.ok === false ? ": " + shortState(p) : ""));
    }
    if (st.internet && st.internet.ok !== null) {
      status.append(h("span", { class: "chip hide-narrow", title: t("internet_hint") },
        h("span", { class: "dot " + (st.internet.ok ? "ok" : "") }), st.internet.ok ? t("internet") : t("no_internet")));
    }
    bar.append(status);
    bar.append(h("span", { class: "chip clock" }, new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    if (st.user) {
      const problems = st.print_problems || 0;
      bar.append(h("button", { class: "chip" + (problems ? " warn" : ""), onclick: openMenu },
        problems ? [icon("alert"), tn("print_problem", problems)] : [icon("menu"), t("menu")]));
    }
  }

  function shortState(p) {
    const key = { no_media: "ps_no_media", cover_open: "ps_cover_open", offline: "ps_offline", media_mismatch: "ps_media_mismatch",
      cutter_jam: "ps_cutter_jam" }[p.state];
    return key ? t(key) : p.message || t("ps_problem");
  }

  async function heartbeat() {
    const epoch = S.authEpoch;
    try {
      const st = await api("GET", "/api/status", undefined, { timeout: 3500 });
      S.fails = 0;
      S.lastOk = Date.now();
      setOnline(true);
      // Ignore a reply that was requested before a login/logout happened.
      if (epoch === S.authEpoch) onStatus(st);
    } catch (err) {
      if (err.status === 401 && err.data && err.data.login === "station") {
        S.status = null;
        setOnline(true);
        if (!["first-run", "register"].includes(S.screen)) startDeviceSetup();
      } else if (err.status === 0) {
        S.fails++;
        if (S.fails >= 2) setOnline(false);
      } else if (!(err instanceof HB.ApiError)) {
        console.error("kiosk error", err);
      }
    }
    setTimeout(heartbeat, 1000);
  }

  function setOnline(ok) {
    const was = S.online;
    S.online = ok;
    let overlay = document.getElementById("offline");
    if (!ok && !overlay) {
      overlay = h("div", { class: "offline", id: "offline" },
        h("h1", null, t("off_title")),
        h("p", null, t("off_p1")),
        h("p", null, t("off_p2")),
        h("p", { id: "offline-pending" }));
      document.body.append(overlay);
    }
    if (!ok) {
      const note = document.getElementById("offline-pending");
      if (note) note.textContent = S.pending
        ? t("off_pending", { family: S.pending.family })
        : "";
    }
    if (ok && overlay) overlay.remove();
    if (ok && !was) {
      renderBar();
      if (S.pending) resendPending();
    }
    if (was !== ok) renderBar();
  }

  function onStatus(st) {
    const firstStatus = !S.status;
    S.status = st;
    // The Chinese button is switched on in Admin. Off: English on every device. A change redraws the screen.
    const languageChanged = KS.allow(settings().chinese_button === true);
    const open = (st.sessions || []).filter((s) => s.status === "open");
    if (!open.find((s) => s.id === S.sessionId)) {
      S.sessionId = st.default_session_id;
      store.set("checkin.sessionId", S.sessionId ? String(S.sessionId) : null);
    }
    renderBar();
    if (!st.user) {
      if (S.screen !== "lock" || languageChanged) showLock();
      return;
    }
    if (languageChanged && S.screen === "search") showSearch();   // other screens pick it up when next drawn
    if (S.screen === "lock" || S.screen === "boot" || firstStatus && S.screen !== "search") {
      S.pending ? resendPending() : (session() ? showSearch() : showNoSession());
      return;
    }
    if (!session() && !["no-session", "success", "menu"].includes(S.screen)) {
      showNoSession();
      return;
    }
    if (S.screen === "no-session" && session()) showSearch();
    if (S.changeSeq !== st.change_seq) {
      const changed = S.changeSeq !== -1;
      S.changeSeq = st.change_seq;
      if (changed && S.screen === "household" && !S.busy) refreshHousehold();
    }
  }

  // ---------------------------------------------------------------------------
  // Device setup: first run on the main computer, or registering an iPad
  // ---------------------------------------------------------------------------
  async function startDeviceSetup() {
    document.querySelectorAll(".overlay, .drawer").forEach((el) => el.remove());
    let ping;
    try { ping = await api("GET", "/api/ping"); } catch (e) { return; }
    if (ping.setup_required) showFirstRun(ping.church_name); else showRegister();
  }

  function showFirstRun(orgName) {
    const f = {
      church: h("input", { value: orgName || "Your Church" }),
      admin: h("input", { value: "Admin", autocomplete: "username" }),
      password: h("input", { type: "password", autocomplete: "new-password" }),
      pin: h("input", { inputmode: "numeric", pattern: "[0-9]*", maxlength: 8 }),
      passphrase: h("input", { type: "password" }),
      station: h("input", { value: "Main desk" }),
      event: h("input", { value: "Sunday Service" }),
    };
    const restoreFile = h("input", { type: "file", accept: ".checkinbak" });
    const restorePass = h("input", { type: "password" });
    show("first-run", h("div", { class: "form" },
      h("h1", null, "Set up the check-in system"),
      h("p", { class: "hint" }, "Do this once, on the main computer. Write the password, PIN and backup passphrase down somewhere safe."),
      field("Church name", f.church),
      h("div", { class: "row" },
        field("Administrator name", f.admin),
        field("Administrator password", f.password, "at least 8 characters")),
      h("div", { class: "row" },
        field("Volunteer PIN", f.pin, "4 to 8 digits, used every Sunday"),
        field("Name of this check-in desk", f.station)),
      field("Backup passphrase", f.passphrase, "at least 10 characters. Needed to restore on another computer."),
      field("Event name in the church database", f.event),
      h("div", { class: "btn-row", style: { justifyContent: "flex-start" } },
        h("button", { class: "btn big green", onclick: async (e) => {
          e.target.disabled = true;
          try {
            const res = await api("POST", "/api/setup", {
              church_name: f.church.value, admin_name: f.admin.value, admin_password: f.password.value,
              volunteer_pin: f.pin.value, backup_passphrase: f.passphrase.value, station_name: f.station.value,
              breeze_event_name: f.event.value,
            }, { timeout: 30000 });
            store.set("checkin.station", res.station_token);
            S.authEpoch++;
            toast("Setup complete. Printers: " + (res.printers.join(", ") || "none"));
            S.screen = "boot";
          } catch (err) { toast(errorText(err), "error", 5000); e.target.disabled = false; }
        } }, "Finish setup")),
      h("h2", { style: { marginTop: "36px" } }, "Or restore from a backup"),
      h("p", { class: "hint" }, "Moving to a replacement computer? Choose the .checkinbak file and enter its passphrase."),
      field("Backup file", restoreFile), field("Backup passphrase", restorePass),
      h("div", { class: "btn-row", style: { justifyContent: "flex-start" } },
        h("button", { class: "btn big outline-blue", onclick: async (e) => {
          const file = restoreFile.files[0];
          if (!file) { toast("Choose the backup file first.", "error"); return; }
          e.target.disabled = true;
          try {
            const data = await HB.fileToBase64(file);
            const res = await api("POST", "/api/setup/restore", { data_b64: data, passphrase: restorePass.value }, { timeout: 120000 });
            toast(`Restored backup from ${res.restored.created_at}. Now register this desk as a station.`, "", 6000);
            showRegister();
          } catch (err) { toast(errorText(err), "error", 6000); e.target.disabled = false; }
        } }, "Restore backup"))));
  }

  function showRegister() {
    const name = h("input", { autocomplete: "username", value: "Admin", onfocus: (e) => e.target.select() });
    const password = h("input", { type: "password", autocomplete: "current-password",
      onkeydown: (e) => { if (e.key === "Enter") signInBtn.click(); } });
    let signInBtn = null;
    let joinCode = "";
    try { joinCode = new URLSearchParams(location.search).get("join") || ""; } catch (e) { /* old browser */ }
    if (joinCode) history.replaceState(null, "", location.pathname);
    const code = h("input", { value: joinCode, autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
      maxlength: 9, placeholder: "6 letters", style: { fontSize: "28px", letterSpacing: "4px", textTransform: "uppercase" } });
    code.addEventListener("keydown", (e) => { if (e.key === "Enter") joinBtn.click(); });
    const joinBtn = h("button", { class: "btn big green", onclick: async () => {
      joinBtn.disabled = true;
      try {
        const res = await api("POST", "/api/stations/join", { code: code.value }, { token: "" });
        store.set("checkin.station", res.station_token);
        store.set("checkin.session", null);
        S.authEpoch++;
        toast(`This device is now “${res.station.name}” until the end of today. Unlock with the volunteer PIN.`, "", 6000);
        S.screen = "boot";
      } catch (err) { toast(errorText(err), "error", 5000); joinBtn.disabled = false; }
    } }, "Join for today");
    const codePanel = h("div", { class: "code-panel hidden", id: "code-panel" },
      h("h2", null, "Got a code from a volunteer?"),
      h("p", { class: "hint" }, "A volunteer at a check-in desk taps Menu → Add a one-time device. Type the code here and this device works until the end of today."),
      Object.assign(field("One-time code", code), { className: "wide" }),
      h("div", { class: "btn-row" }, joinBtn));
    const adminPanel = h("div", { class: "admin-panel" },
      h("h2", null, "Set up a permanent station"),
      h("p", { class: "hint" }, "An administrator signs in once. After that, volunteers only need the PIN."),
      h("div", { class: "row" }, field("Administrator name", name), field("Password", password)),
      h("div", { class: "btn-row", style: { justifyContent: "flex-start" } },
        signInBtn = h("button", { class: "btn big blue", onclick: async (e) => {
          e.target.disabled = true;
          try {
            const login = await api("POST", "/api/admin/login", { name: name.value, password: password.value }, { token: "" });
            await chooseStation(login.token);
          } catch (err) { toast(errorText(err), "error"); e.target.disabled = false; }
        } }, "Sign in")));
    const codeToggle = h("button", { class: "btn big ghost code-toggle", type: "button", "aria-controls": "code-panel",
      "aria-expanded": "false", onclick: () => {
        const open = codePanel.classList.contains("hidden");
        codePanel.classList.toggle("hidden", !open);
        adminPanel.classList.toggle("hidden", open);
        codeToggle.setAttribute("aria-expanded", String(open));
        codeToggle.textContent = open ? "Back to administrator sign-in" : "Log in with one-time code";
      } }, "Log in with one-time code");
    show("register", h("div", { class: "form" },
      h("h1", null, "Set up this device as a check-in station"),
      adminPanel, codePanel, codeToggle));
    if (joinCode) {
      adminPanel.classList.add("hidden");
      codePanel.classList.remove("hidden");
      codeToggle.setAttribute("aria-expanded", "true");
      codeToggle.textContent = "Back to administrator sign-in";
      joinBtn.click();
    }
  }

  async function chooseStation(adminToken) {
    const [printerData, stations] = await Promise.all([
      api("GET", "/api/admin/printers", undefined, { token: adminToken }),
      api("GET", "/api/admin/stations", undefined, { token: adminToken }),
    ]);
    const names = printerData.printers.map((p) => p.name);
    const options = (selected, blank) => [blank ? h("option", { value: "" }, blank) : null,
      ...names.map((n) => h("option", { value: n, selected: n === selected }, n))];
    const stationName = h("input", { placeholder: "for example iPad Left" });
    const printer = h("select", null, options(names[0]));
    const fallback = h("select", null, options(names[1] || "", "(none)"));
    const existing = h("select", { onchange: () => {
      const st = stations.find((s) => String(s.id) === existing.value);
      if (st) { stationName.value = st.name; printer.value = st.printer || ""; fallback.value = st.fallback_printer || ""; }
    } }, h("option", { value: "" }, "New station…"), stations.map((s) => h("option", { value: s.id }, s.name)));
    show("register", h("div", { class: "form" },
      h("h1", null, "Which desk is this?"),
      stations.length ? field("Replace an existing station", existing, "for example after clearing Safari data") : null,
      field("Station name", stationName),
      h("div", { class: "row" },
        field("Prints to", printer, "the printer at this desk"),
        field("Backup printer", fallback, "used if the first one fails")),
      h("div", { class: "btn-row", style: { justifyContent: "flex-start" } },
        h("button", { class: "btn big green", onclick: async (e) => {
          e.target.disabled = true;
          try {
            const res = await api("POST", "/api/admin/stations/register", {
              name: stationName.value, printer: printer.value, fallback_printer: fallback.value,
            }, { token: adminToken });
            store.set("checkin.station", res.station_token);
            S.authEpoch++;
            api("POST", "/api/logout", {}, { token: adminToken }).catch(() => {});
            toast(`This device is now “${res.station.name}”.`);
            S.screen = "boot";
          } catch (err) { toast(errorText(err), "error"); e.target.disabled = false; }
        } }, "Save station"))));
  }

  // ---------------------------------------------------------------------------
  // Lock screen (volunteer PIN)
  // ---------------------------------------------------------------------------
  function showLock() {
    let pin = "";
    const dots = h("div", { class: "pin-dots" });
    const paint = () => { clear(dots).append(...Array.from({ length: Math.max(4, pin.length) }, (_, i) => h("span", { class: i < pin.length ? "on" : "" }))); };
    const submit = async () => {
      if (pin.length < 4) return;
      try {
        const res = await api("POST", "/api/login", { pin }, { token: "" });
        store.set("checkin.session", res.token);
        S.authEpoch++;
        S.screen = "boot";
        toast(t("unlocked", { name: res.user.name }));
      } catch (err) {
        toast(errorText(err), "error");
        pin = "";
        paint();
      }
    };
    const press = (k) => {
      if (k === "back") pin = pin.slice(0, -1);
      else if (k === "ok") return submit();
      else if (pin.length < 8) pin += k;
      paint();
    };
    paint();
    show("lock", h("div", { class: "lock" },
      h("h1", null, settings().church_name || t("lock_title")),
      h("div", { class: "hint" }, S.status && S.status.station ? S.status.station.name : ""),
      h("p", { style: { fontSize: "22px", fontWeight: 650, marginBottom: 0 } }, t("lock_enter")),
      dots,
      keypad(["1", "2", "3", "4", "5", "6", "7", "8", "9", "back", "0", "ok"], press)));
    S.keyHandler = (e) => {
      if (/^[0-9]$/.test(e.key)) press(e.key);
      else if (e.key === "Backspace") press("back");
      else if (e.key === "Enter") press("ok");
    };
  }

  // Keys drawn as icons, with what they do for screen readers: back deletes a digit, ok unlocks.
  const KEY_ICONS = { back: ["backspace", "key_delete"], ok: ["check", "key_unlock"] };

  function keypad(keys, onPress, cls = "") {
    return h("div", { class: "keypad " + cls }, keys.map((k) => h("button", {
      class: k === "ok" ? "go" : ["back", "Clear", "Search"].includes(k) ? "fn" : "",
      "aria-label": KEY_ICONS[k] ? t(KEY_ICONS[k][1]) : null,
      onclick: () => onPress(k),
    }, KEY_ICONS[k] ? icon(KEY_ICONS[k][0]) : k === "Clear" ? t("key_clear") : k)));
  }

  // ---------------------------------------------------------------------------
  // No session yet today
  // ---------------------------------------------------------------------------
  async function showNoSession() {
    show("no-session", h("div", { class: "center-col" }, h("span", { class: "spinner" })));
    let services = [];
    try { services = await api("GET", "/api/services"); } catch (err) { /* shown below */ }
    // Today's services come first as the main buttons. The rest stay one tap away (a one-off event on another
    // day), marked with the day they normally run so no one starts Sunday's service on a Wednesday by mistake.
    const today = weekdayToday();
    const todays = services.filter((x) => x.weekday === null || x.weekday === undefined || x.weekday === today);
    const others = services.filter((x) => !todays.includes(x));
    const label = (svc) => t("start_service", { name: svc.name }) + (svc.start_time ? " · " + svc.start_time : "");
    show("no-session", h("div", { class: "center-col" },
      h("h1", null, t("ns_title")),
      h("p", { class: "hint", style: { fontSize: "20px" } }, todays.length || !others.length
        ? t("ns_pick") : t("ns_none_today", { day: t("day_" + today) })),
      h("div", { class: "results", style: { maxWidth: "560px", margin: "20px auto" } },
        todays.map((x) => h("button", { class: "btn huge green", onclick: () => openService(x.id) }, label(x))),
        others.length ? h("div", { class: "prompt other-days" }, todays.length ? t("other_days") : t("other_days_services")) : null,
        others.map((x) => h("button", { class: "btn big", onclick: () => openService(x.id) },
          label(x), h("span", { class: "hint" }, t("on_days", { day: t("day_" + x.weekday) })))),
        services.length ? null
          : h("div", { class: "empty-state" }, t("no_services")))));
  }

  // Monday is 0, as the main computer stores it. Uses the main computer's date, not the iPad's.
  function weekdayToday() {
    const iso = S.status && S.status.date;
    const day = iso ? new Date(iso + "T12:00:00Z").getUTCDay() : new Date().getDay();
    return (day + 6) % 7;
  }

  async function openService(id) {
    try {
      const sess = await api("POST", "/api/sessions/open", { service_id: id });
      S.sessionId = sess.id;
      store.set("checkin.sessionId", String(sess.id));
      S.status = await api("GET", "/api/status");
      showSearch();
    } catch (err) { toast(errorText(err), "error"); }
  }

  function chooseSession() {
    const list = ((S.status && S.status.sessions) || []).filter((s) => s.status === "open");
    dialog(t("which_service"), h("div", { class: "results" },
      list.map((s) => h("button", {
        class: "btn big " + (s.id === S.sessionId ? "green" : ""),
        onclick: () => {
          S.sessionId = s.id;
          store.set("checkin.sessionId", String(s.id));
          document.querySelector(".overlay")?.remove();
          showSearch();
        },
      }, `${s.name}${s.start_time ? " · " + s.start_time : ""}  ${t("checked_in_n", { n: s.checked_in })}`)),
      h("button", { class: "btn big", onclick: () => { document.querySelector(".overlay")?.remove(); showNoSession(); } }, t("start_another"))),
    [{ label: t("close") }]);
  }

  // ---------------------------------------------------------------------------
  // Search
  // ---------------------------------------------------------------------------
  function showSearch(message) {
    S.digits = "";
    S.household = null;
    S.householdId = null;
    const kids = !everyone();
    const hour = new Date().getHours();
    const greeting = hour < 12 ? t("good_morning") : hour < 18 ? t("good_afternoon") : t("good_evening");
    const display = h("div", { class: "digits placeholder" }, "• • • •");
    // Before a search, the right half says hello and how check-in works; matches replace it, and Clear brings it back.
    const welcome = () => [
      h("img", { class: "welcome-mark", src: "/brand-mark.png", alt: `${settings().church_name || "Your Church"} logo` }),
      h("h2", { class: "welcome-title" }, t("welcome_title", { greeting, name: settings().church_name || "Your Church" })),
      h("ol", { class: "steps" },
        h("li", null, kids ? t("step1_kids") : t("step1_adult")),
        h("li", null, kids ? t("step2_kids")
          : mode() === "family" ? t("step2_family")
            : t("step2_individual")),
        h("li", null, kids ? t("step3_kids") : t("step3_adult"))),
    ];
    const results = h("div", { class: "results" },
      message ? h("div", { class: "print-box ok" }, message) : null, ...welcome());
    const paint = () => {
      display.textContent = S.digits || "• • • •";
      display.classList.toggle("placeholder", !S.digits);
      display.classList.toggle("long", S.digits.length > 8);
    };
    // A search only shows its answer if the digits haven't changed since it was sent. Typing a full number used to
    // search its first 4 digits as if they were the last 4; on a slow iPad that answer came back after the full-number
    // search and replaced it, or opened the wrong family (the main computer answers at once, so it seemed fine there).
    let timer;
    const run = async () => {
      clearTimeout(timer);
      const query = S.digits;
      // results.isConnected: this search screen is still the one showing, not a later copy with the same digits.
      if (query.length < 4 || !results.isConnected) return;
      await runSearch(query, results, false, () => results.isConnected && S.digits === query);
    };
    const press = (k) => {
      clearTimeout(timer);
      if (k === "back") S.digits = S.digits.slice(0, -1);
      else if (k === "Clear") { S.digits = ""; clear(results).append(...welcome()); }
      else if (k === "Search") return run();
      else if (S.digits.length < 12) S.digits += k;
      paint();
      // The digits changed while a search was out, so its answer will be dropped: don't leave "Searching…" up.
      if (results.querySelector(".spinner")) {
        clear(results).append(...(S.digits.length > 4
          ? [h("div", { class: "empty-state" }, t("rest_of_number"))] : welcome()));
      }
      // Last 4 digits: search after a short pause, so someone keying a full number isn't stopped at the 4th digit.
      // A full Singapore mobile (8 digits) searches straight away.
      if (S.digits.length === 4) timer = setTimeout(run, 900);
      else if (S.digits.length === 8) run();
    };
    const newcomer = settings().allow_volunteer_visitors !== false
      ? h("button", { class: "newcomer", onclick: () => showVisitor() },
        h("span", { class: "newcomer-icon" }, icon("plus")),
        h("span", null,
          h("span", { class: "newcomer-title" }, kids ? t("new_family_q") : t("first_time", { name: settings().church_name || "Your Church" })),
          h("span", { class: "newcomer-sub" }, kids ? t("reg_family") : t("reg_you"))))
      : null;
    // Members on the left (the keypad), newcomers on the right, each with its own big answer. The newcomer button sits
    // at the foot of the right half and never scrolls away; matches scroll above it.
    show("search", h("div", { class: "search-grid" },
      h("section", { class: "pad", "aria-label": t("pad_aria") },
        h("h2", { class: "pad-title" }, kids ? t("pad_title_kids") : t("pad_title")),
        h("p", { class: "prompt" }, kids ? t("step1_kids") : t("step1_adult")),
        display,
        keypad(["1", "2", "3", "4", "5", "6", "7", "8", "9", "Clear", "0", "back"], press),
        h("div", { class: "side-actions" },
          h("button", { class: "btn big", onclick: () => showNameSearch("") }, t("search_name")),
          h("button", { class: "btn big", onclick: () => run() }, t("search_full")),
          pickupOn() ? h("button", { class: "btn big outline-purple wide", onclick: () => showCheckout() }, t("check_out")) : null)),
      h("section", { class: "welcome", "aria-live": "polite" }, results, newcomer)));
    S.keyHandler = (e) => {
      if (e.target && e.target.tagName === "INPUT") return;
      if (/^[0-9]$/.test(e.key)) press(e.key);
      else if (e.key === "Backspace") press("back");
      else if (e.key === "Enter") press("Search");
      else if (e.key === "Escape") press("Clear");
      else if (/^[a-zA-Z]$/.test(e.key)) { showNameSearch(e.key); e.preventDefault(); }
    };
  }

  // isCurrent says whether the screen still wants this answer; an older search that comes back late is dropped.
  async function runSearch(query, results, isName, isCurrent = () => true) {
    clear(results).append(h("div", { class: "empty-state" }, h("span", { class: "spinner" }), " " + t("searching")));
    let data;
    try {
      data = await api("GET", "/api/search?q=" + encodeURIComponent(query));
    } catch (err) {
      if (isCurrent()) clear(results).append(h("div", { class: "print-box bad" }, errorText(err)));
      return;
    }
    if (!isCurrent()) return;
    const list = data.results;
    for (const fam of list) S.matched[fam.id] = fam.matched || [];
    if (list.length === 1 && !isName) {
      openHousehold(list[0].id);
      return;
    }
    clear(results);
    // On a phone the answer lands under the keypad, out of sight: once it's drawn, scroll it up (with the newcomer
    // button too when no one matched).
    const reveal = (el) => { if (!isName && window.matchMedia("(max-width: 560px)").matches) el.scrollIntoView({ block: "nearest" }); };
    if (!list.length) {
      results.append(h("div", { class: "empty-state" },
        h("strong", null, isName ? t("none_name", { q: query }) : t("none_digits", { q: query.slice(-4) })),
        isName ? t("check_spelling") : t("check_digits"),
        // The search screen has the newcomer button under this; the name search screen gets its own.
        settings().allow_volunteer_visitors === false ? null
          : isName ? h("div", { class: "btn-row" }, h("button", { class: "btn big green", onclick: () => showVisitor() }, icon("plus"),
            everyone() ? t("first_time", { name: settings().church_name || "Your Church" }) : t("new_family_q")))
          : h("span", { class: "newcomer-hint" }, everyone() ? t("hint_first") : t("hint_new"))));
      reveal(results.closest(".welcome") || results);
      return;
    }
    if (list.length > 1 && !isName) {
      results.append(h("div", { class: "prompt" }, t("families_match", { n: list.length })));
    }
    for (const fam of list) {
      const members = fam.members || [];
      const single = everyone() && members.length <= 1;
      results.append(h("button", { class: "result", onclick: () => openHousehold(fam.id) },
        h("div", null,
          h("div", { class: "fam" }, fam.title || fam.name || t("family_default"), fam.visitor ? h("span", { class: "pill blue", style: { marginLeft: "10px" } }, t("visitor_pill")) : null),
          single ? null : h("div", { class: "kids" }, everyone() ? members.join(", ")
            : fam.children.length ? t("children_prefix", { names: fam.children.join(", ") }) : t("no_children_listed")),
          everyone() ? null : h("div", { class: "who" }, fam.guardians.join(" · ") || "-")),
        h("div", { class: "phone" }, fam.phone)));
    }
    reveal(results);
  }

  function showNameSearch(initial) {
    const input = h("input", { value: initial || "", placeholder: everyone() ? t("ph_name") : t("ph_name_kids"), autocomplete: "off", autocapitalize: "words", spellcheck: false });
    const results = h("div", { class: "results" });
    const search = (q) => runSearch(q, results, true, () => results.isConnected && input.value.trim() === q);
    let timer;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      // Too short to search: clear now, or a dropped answer would leave "Searching…" up.
      if (input.value.trim().length < 2) { clear(results); return; }
      timer = setTimeout(() => search(input.value.trim()), 250);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) search(input.value.trim());
      if (e.key === "Escape") showSearch();
    });
    show("name-search", h("div", { style: { maxWidth: "900px", margin: "0 auto" } },
      h("div", { class: "hh-head" }, h("button", { class: "btn big", onclick: () => showSearch() }, icon("back"), t("back")),
        h("h1", null, t("search_name"))),
      h("div", { class: "namebox" }, input),
      results));
    input.focus();
    if (initial) setTimeout(() => input.dispatchEvent(new Event("input")), 50);
  }

  // ---------------------------------------------------------------------------
  // Household
  // ---------------------------------------------------------------------------
  async function openHousehold(id, preselect) {
    S.householdId = id;
    S.txnId = uuid();
    S.selected = new Set();
    show("household", h("div", { class: "empty-state" }, h("span", { class: "spinner" }), " " + t("loading_family")));
    await refreshHousehold(true, preselect);
  }

  async function refreshHousehold(first, preselect) {
    const id = S.householdId;
    if (!id) return;
    let hh;
    try {
      hh = await api("GET", `/api/households/${id}?session_id=${S.sessionId || ""}`);
    } catch (err) {
      toast(errorText(err), "error");
      if (first) showSearch();
      return;
    }
    if (S.householdId !== id || (S.screen !== "household")) return;
    const signature = JSON.stringify({ people: hh.people, transactions: hh.transactions, session_id: S.sessionId });
    if (!first && signature === S.householdSignature) { S.household = hh; return; }
    S.household = hh;
    S.householdSignature = signature;
    const available = hh.people.filter((c) => c.status === "not_in");
    if (first) {
      const wanted = preselect || (mode() === "individual" ? S.matched[id] || []
        : settings().preselect_children !== false ? available.map((c) => c.id) : []);
      S.selected = new Set(available.filter((c) => wanted.includes(c.id)).map((c) => c.id));
    } else {
      S.selected = new Set([...S.selected].filter((pid) => available.some((c) => c.id === pid)));
    }
    try {
      renderHousehold();
    } catch (err) {
      console.error("household render failed", err);
      show("household", h("div", { class: "center-col" }, h("div", { class: "print-box bad" }, t("could_not_show")),
        h("div", { class: "btn-row" }, h("button", { class: "btn big", onclick: () => openHousehold(id) }, t("retry")),
          h("button", { class: "btn big", onclick: () => showSearch() }, t("back")))));
    }
  }

  function renderHousehold() {
    const hh = S.household;
    const button = h("button", { class: "btn huge green", onclick: doCheckin });
    const updateButton = () => {
      const n = S.selected.size;
      button.disabled = n === 0 || S.busy || !session();
      button.textContent = n ? `${t("checkin_print")} (${n})` : t("tap_names");
      if (toggleAll) toggleAll.textContent = S.selected.size === available.length ? t("clear_all") : t("select_all");
    };
    const available = hh.people.filter((c) => c.status === "not_in");
    const toggleAll = available.length > 1 ? h("button", { class: "btn big", onclick: () => {
      S.selected = S.selected.size === available.length ? new Set() : new Set(available.map((c) => c.id));
      main.querySelectorAll(".kid[data-id]").forEach((el) => {
        el.classList.toggle("selected", S.selected.has(Number(el.dataset.id)));
        el.setAttribute("aria-pressed", String(S.selected.has(Number(el.dataset.id))));
      });
      updateButton();
    } }) : null;
    const cards = hh.people.map((c) => {
      const done = c.status !== "not_in";
      const card = h("button", {
        class: "kid" + (done ? " done" : "") + (S.selected.has(c.id) ? " selected" : ""),
        "data-id": done ? null : String(c.id),
        "aria-pressed": done ? null : String(S.selected.has(c.id)),
        onclick: () => {
          if (done) return;
          S.selected.has(c.id) ? S.selected.delete(c.id) : S.selected.add(c.id);
          card.classList.toggle("selected", S.selected.has(c.id));
          card.setAttribute("aria-pressed", String(S.selected.has(c.id)));
          updateButton();
        },
      },
      h("span", { class: "tick" }, icon("check")),
      h("div", { class: "nm" }, c.name),
      h("div", { class: "ln" }, c.last_name, c.last_name && c.chinese_name ? " " : null,
        c.chinese_name ? h("span", { lang: "zh" }, c.chinese_name) : null),
      h("div", { class: "meta" },
        c.class ? h("span", { class: "pill " + (c.no_class ? "amber" : "green") }, className(c.class) + (c.room ? " · " + c.room : "")) : null,
        // A child's age and school year show only in children-only mode, when a children's ministry volunteer is
        // at the desk: on an open foyer screen anyone who keys four digits would see them.
        !everyone() && c.is_child && c.age !== null ? h("span", { class: "pill" }, t("age_n", { n: c.age })) : null,
        !everyone() && c.school_year ? h("span", { class: "pill" }, c.school_year) : null,
        c.visitor ? h("span", { class: "pill blue" }, t("visitor_pill")) : null),
      c.alert ? h("div", { class: "alert" }, h("span", { class: "ico" }, "!"), c.alert_note || t("allergy_alert")) : null,
      c.status === "in" ? h("div", { class: "state in" }, icon("check"), t("checked_in_at", { time: c.checked_in_at }) + (pickupOn() ? t("code_suffix", { code: c.code }) : "")) : null,
      c.status === "out" ? h("div", { class: "state out" }, t("collected_at", { time: c.checked_out_at })) : null,
      c.status === "in" ? h("div", { class: "kid-actions" },
        h("button", { class: "btn small outline-blue", onclick: (e) => { e.stopPropagation(); reprintDialog({ id: c.txn_id, code: c.code, time: c.checked_in_at, children: [c.name] }, [c.id]); } }, t("reprint_label")),
        pickupOn() ? h("button", { class: "btn small outline-purple", onclick: (e) => { e.stopPropagation(); confirmCheckout([c]); } }, t("check_out")) : null) : null);
      return card;
    });
    const txns = hh.transactions.length ? h("div", { class: "txns" }, hh.transactions.map((x) => h("div", { class: "txn" },
      h("span", { class: "code" }, pickupOn() ? x.code : x.time),
      h("div", { class: "grow" }, h("strong", null, x.children.join(", ")), h("div", { class: "hint" },
        t("checked_in_at", { time: x.time }) + (x.reprints ? t("reprinted_n", { n: x.reprints }) : "") + (x.job ? t("labels_status", { status: jobWord(x.job) }) : ""))),
      h("button", { class: "btn small outline-blue", onclick: () => reprintDialog(x) }, t("reprint")),
      h("button", { class: "btn small outline-amber", onclick: () => undoDialog(x.id, x.children) }, t("undo"))))) : null;
    const allDone = hh.people.length && hh.people.every((c) => c.status !== "not_in");
    const content = [
      h("div", { class: "hh-head" },
        h("button", { class: "btn big", onclick: () => showSearch() }, icon("back"), t("back")),
        h("div", null, h("h1", null, hh.title || hh.name || t("family_default"), hh.visitor ? h("span", { class: "pill blue", style: { marginLeft: "12px", verticalAlign: "middle" } }, t("visitor_pill")) : null),
          h("div", { class: "who" }, everyone() ? hh.phones.join("  ") : [hh.guardians.join(" · "), hh.phones.join("  ")].filter(Boolean).join("   "))),
        h("span", { class: "grow", style: { flex: 1 } }), toggleAll),
      txns,
      hh.people.length ? h("div", { class: "kids-grid" }, cards)
        : h("div", { class: "empty-state" }, h("strong", null, everyone() ? t("no_one_found") : t("no_children_found")),
          t("ask_admin")),
      h("div", { class: "action-bar" },
        allDone ? h("button", { class: "btn huge", onclick: () => showSearch() }, t("all_done")) : button),
    ];
    const keepScroll = S.screen === "household" && S.renderedHousehold === hh.id ? main.scrollTop : 0;
    show("household", ...content);
    S.renderedHousehold = hh.id;
    main.scrollTop = keepScroll;
    updateButton();
    S.keyHandler = (e) => {
      if (e.key === "Enter") doCheckin();
      else if (e.key === "Escape") showSearch();
    };
  }

  function jobWord(job) {
    const key = { printed: "js_printed", failed: "js_failed", unknown: "js_unknown", queued: "js_queued", printing: "js_printing", cancelled: "js_cancelled" }[job.status];
    return key ? t(key) : job.status;
  }

  // ---------------------------------------------------------------------------
  // Check in
  // ---------------------------------------------------------------------------
  async function doCheckin() {
    if (S.busy || !S.selected.size) return;
    if (!session()) { toast(t("choose_service_first"), "error"); return; }
    S.busy = true;
    const button = main.querySelector(".action-bar .btn.huge");
    if (button) { button.disabled = true; button.replaceChildren(h("span", { class: "spinner" }), "\u00a0 " + t("checking_in")); }
    const body = { txn_id: S.txnId, session_id: S.sessionId, household_id: S.householdId, person_ids: [...S.selected] };
    setPending({ body, family: (S.household && S.household.name) || t("family_word"), at: Date.now() });
    try {
      const result = await api("POST", "/api/checkin", body, { timeout: 10000 });
      setPending(null);
      showSuccess(result);
    } catch (err) {
      if (err.status === 0) {
        toast(t("no_answer"), "error", 6000);
        setOnline(false);
      } else {
        setPending(null);
        S.txnId = uuid();
        toast(errorText(err), "error", 5000);
        S.busy = false;
        refreshHousehold();
      }
    } finally {
      S.busy = false;
    }
  }

  async function resendPending() {
    const pending = S.pending;
    if (!pending || S.resending) return;
    S.resending = true;
    try {
      const result = await api("POST", "/api/checkin", pending.body, { timeout: 10000 });
      setPending(null);
      toast(t("confirmed", { family: pending.family }));
      showSuccess(result);
    } catch (err) {
      if (err.status !== 0) {
        setPending(null);
        toast(t("not_saved", { family: pending.family, error: errorText(err) }), "error", 8000);
        if (S.status && S.status.user) showSearch();
      }
    } finally {
      S.resending = false;
    }
  }

  function showSuccess(result) {
    S.lastResult = result;
    const names = result.children.map((c) => c.class ? `${c.name} (${className(c.class)})` : c.name).join(", ");
    const box = h("div", { class: "print-box wait" }, h("span", { class: "spinner" }), "  " + t("printing_labels"));
    const buttons = h("div", { class: "btn-row" });
    const countdown = h("div", { class: "hint", style: { marginTop: "12px", fontSize: "18px" } });
    show("success", h("div", { class: "center-col" },
      h("div", { class: "big-tick" }, icon("check")),
      h("h1", { class: "success-title" }, result.replay ? t("already_saved") : t("checked_in_title")),
      h("div", { class: "success-names" }, names),
      result.skipped && result.skipped.length ? h("div", { class: "print-box warn" }, t("already_earlier", { names: result.skipped.join(", ") })) : null,
      pickupOn() ? h("div", null, h("span", { class: "code-show" }, result.code)) : null,
      box, buttons, countdown,
      h("div", { class: "btn-row", style: { marginTop: "26px" } },
        h("button", { class: "btn big", onclick: () => showSearch() }, t("done_next")),
        h("button", { class: "btn big outline-amber", onclick: () => undoDialog(result.txn_id, result.children.map((c) => c.name)) }, t("undo")))));
    if (result.job) watchJob(result.job.id, box, buttons, countdown, result);
    else box.replaceChildren(...nodes(t("no_labels")));
  }

  async function watchJob(jobId, box, buttons, countdown, result) {
    const token = S.pollToken;
    const started = Date.now();
    let job;
    while (S.pollToken === token) {
      try { job = await api("GET", "/api/jobs/" + jobId, undefined, { timeout: 3000 }); } catch (e) { job = null; }
      if (job && ["printed", "failed", "unknown", "cancelled"].includes(job.status)) break;
      if (job && Date.now() - started > 1500) {
        box.replaceChildren(...nodes(h("span", { class: "spinner" }), "  " + tn(job.status === "printing" ? "job_printing" : "job_waiting", job.label_count, { printer: job.printer })));
      }
      await new Promise((r) => setTimeout(r, 300));
    }
    if (S.pollToken !== token || !job) return;
    renderJobResult(job, box, buttons, countdown, result);
  }

  function renderJobResult(job, box, buttons, countdown, result) {
    clear(buttons);
    const note = job.route_note ? h("div", { class: "sub" }, job.route_note) : null;
    const others = printers().filter((p) => p.name !== job.printer && p.ok);
    if (job.status === "printed") {
      box.className = "print-box " + (job.route_note ? "warn" : "ok");
      box.replaceChildren(...nodes(icon("check"), " " + tn("job_printed", job.label_count, { printer: job.printer }), note));
      let left = Number(settings().success_return_seconds || 3) + (job.route_note ? 4 : 0);
      const token = S.pollToken;
      const tick = () => {
        if (S.pollToken !== token) return;
        if (left <= 0) { showSearch(); return; }
        countdown.textContent = t("next_family", { n: left });
        left--;
        S.autoReturn = setTimeout(tick, 1000);
      };
      tick();
      main.addEventListener("pointerdown", () => { S.pollToken++; countdown.textContent = ""; }, { once: true });
      return;
    }
    if (job.status === "cancelled") {
      box.className = "print-box warn";
      box.replaceChildren(...nodes(t("labels_cancelled")));
      return;
    }
    box.className = "print-box " + (job.status === "unknown" ? "warn" : "bad");
    box.replaceChildren(...nodes(job.status === "unknown" ? t("did_labels") : t("print_failed"), h("div", { class: "sub" }, job.error || ""), note));
    const retryOn = async (printer) => {
      try {
        const updated = await api("POST", `/api/jobs/${job.id}/retry`, { printer: printer || null });
        box.className = "print-box wait";
        box.replaceChildren(...nodes(h("span", { class: "spinner" }), "  " + t("printing_on", { printer: updated.printer })));
        clear(buttons);
        watchJob(job.id, box, buttons, countdown, result);
      } catch (err) { toast(errorText(err), "error"); }
    };
    if (job.status === "unknown") {
      buttons.append(h("button", { class: "btn big green", onclick: async () => {
        await api("POST", `/api/jobs/${job.id}/resolve`, { outcome: "printed" }).catch(() => {});
        showSearch();
      } }, t("yes_printed")));
    }
    buttons.append(h("button", { class: "btn big blue", onclick: () => retryOn(job.printer) }, t("retry_on", { printer: job.printer })));
    for (const p of others) buttons.append(h("button", { class: "btn big outline-blue", onclick: () => retryOn(p.name) }, t("print_on", { printer: p.name })));
    countdown.textContent = t("retry_note");
  }

  // ---------------------------------------------------------------------------
  // Reprint / undo / check out
  // ---------------------------------------------------------------------------
  function reprintDialog(txn, personIds) {
    let which = "all";
    let printer = stationPrinter() || "";
    const whichRow = h("div", { class: "btn-row", style: { justifyContent: "flex-start" } });
    const printerRow = h("div", { class: "btn-row", style: { justifyContent: "flex-start" } });
    const paint = () => {
      clear(whichRow).append(...[["all", t("all_labels")], ["child", t("child_labels")], ["guardian", t("pickup_tag")]].map(([k, label]) =>
        h("button", { class: "btn " + (which === k ? "blue" : ""), onclick: () => { which = k; paint(); } }, label)));
      clear(printerRow).append(...printers().map((p) =>
        h("button", { class: "btn " + (printer === p.name ? "blue" : ""), onclick: () => { printer = p.name; paint(); } },
          h("span", { class: "dot " + (p.ok ? "ok" : p.ok === false ? "bad" : "warn") }), p.name)));
    };
    paint();
    dialog(pickupOn() ? `${t("reprint_labels")} · ${txn.code}` : t("reprint_labels"), h("div", null,
      h("p", null, txn.children.join(", ")), pickupOn() ? h("label", null, t("which_labels")) : null, pickupOn() ? whichRow : null,
      h("label", null, t("printer")), printerRow,
      h("p", { class: "hint" }, t("reprint_note"))),
    [{ label: t("cancel") }, { label: t("reprint"), class: "blue", onclick: async (close) => {
      try {
        const job = await api("POST", `/api/txn/${txn.id}/reprint`, { which, printer: printer || null, person_ids: personIds || null });
        toast(t("reprinting_on", { printer: job.printer }));
        trackJobToast(job.id);
        if (S.screen === "household") refreshHousehold();
      } catch (err) { toast(errorText(err), "error"); return true; }
    } }]);
  }

  async function trackJobToast(jobId) {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 500));
      let job;
      try { job = await api("GET", "/api/jobs/" + jobId); } catch (e) { continue; }
      if (job.status === "printed") { toast(t("printed_on", { printer: job.printer })); return; }
      if (["failed", "unknown"].includes(job.status)) { toast(t("print_problem_on", { printer: job.printer, error: job.error || jobWord(job) }), "error", 6000); return; }
    }
  }

  function undoDialog(txnId, names) {
    let reason = "";
    const other = h("input", { placeholder: t("other_reason"), oninput: () => { reason = other.value; paint(); } });
    const row = h("div", { class: "results" });
    const paint = () => clear(row).append(...UNDO_REASONS.map((r, i) => h("button", {
      class: "btn " + (reason === r ? "blue" : ""), onclick: () => { reason = r; other.value = ""; paint(); },
    }, t("undo_r" + (i + 1)))));
    paint();
    dialog(t("undo_title"), h("div", null,
      h("p", { style: { fontSize: "20px" } }, (names || []).join(", ")),
      h("p", { class: "hint" }, t("undo_hint")), row, other),
    [{ label: t("keep_checkin") }, { label: t("undo_checkin"), class: "red", onclick: async () => {
      if ((reason || "").trim().length < 3) { toast(t("choose_reason"), "error"); return true; }
      try {
        await api("POST", `/api/txn/${txnId}/undo`, { reason });
        toast(t("undone"));
        if (S.householdId) openHousehold(S.householdId); else showSearch();
      } catch (err) { toast(errorText(err), "error"); return true; }
    } }]);
  }

  // A volunteer lets a phone or spare iPad check people in for the rest of today, without an administrator.
  async function oneTimeDeviceDialog() {
    let res;
    try { res = await api("POST", "/api/stations/one-time", {}); }
    catch (err) { toast(errorText(err), "error"); return; }
    // The main computer's address as it is right now (it may have moved to another Wi-Fi since it started).
    const urls = res.urls || S.status.server_urls || [];
    if (res.urls) S.status.server_urls = res.urls;
    const url = urls[0];
    const lead = !url ? t("ot_no_address") : res.wifi ? t("ot_open_wifi", { wifi: res.wifi, url }) : t("ot_open", { url });
    dialog(t("ot_title"), h("div", null,
      h("p", null, lead),
      res.qr ? h("div", { style: { display: "flex", alignItems: "center", gap: "16px", justifyContent: "center", margin: "12px 0" } },
        h("img", { src: res.qr, alt: t("qr_alt"), style: { width: "168px", height: "168px", flexShrink: "0", imageRendering: "pixelated" } }),
        h("p", { style: { maxWidth: "220px", minWidth: "0", margin: "0", overflowWrap: "anywhere" } }, t("ot_scan", { url: res.qr_url }))) : null,
      h("div", { style: { fontSize: "56px", fontWeight: "700", letterSpacing: "10px", textAlign: "center", margin: "18px 0", fontFamily: "ui-monospace, Menlo, monospace" } }, res.code),
      url ? h("p", { class: "hint" }, t("ot_link", { url: url.replace(/\/$/, "") + "/?join=" + res.code })) : null,
      h("p", { class: "hint" }, t("ot_note", { minutes: res.minutes, printer: stationPrinter() || t("this_desk_printer") })),
      url ? h("p", { class: "hint" }, t("ot_stuck")) : null),
    [{ label: t("done") }]);
  }

  function confirmCheckout(children) {
    dialog(t("co_confirm_title"), h("p", { style: { fontSize: "22px" } }, children.map((c) => c.name).join(", ")),
      [{ label: t("cancel") }, { label: t("check_out"), class: "purple", onclick: async () => {
        try {
          const res = await api("POST", "/api/checkout", { attendance_ids: children.map((c) => c.attendance_id) });
          toast(res.checked_out.length ? t("collected_list", { names: res.checked_out.join(", ") }) : t("already_out"));
          if (S.screen === "household") refreshHousehold();
        } catch (err) { toast(errorText(err), "error"); return true; }
      } }]);
  }

  function showCheckout() {
    let code = "";
    const display = h("div", { class: "digits placeholder" }, t("code_ph"));
    const results = h("div", { class: "results" },
      h("div", { class: "empty-state" }, h("strong", null, t("check_out")), t("pickup_type")));
    const paint = () => { display.textContent = code || t("code_ph"); display.classList.toggle("placeholder", !code); };
    const lookup = async () => {
      if (code.length < 4) return;
      clear(results).append(h("div", { class: "empty-state" }, h("span", { class: "spinner" })));
      try {
        const txn = await api("GET", "/api/checkout/" + code);
        renderCheckoutTxn(txn, results);
      } catch (err) {
        clear(results).append(h("div", { class: "print-box bad" }, errorText(err)));
      }
    };
    const press = (k) => {
      if (k === "back") code = code.slice(0, -1);
      else if (k === "Clear") code = "";
      else if (code.length < 6) code += k;
      paint();
      if (code.length === 4) lookup();
    };
    show("checkout", h("div", { class: "search-grid" },
      h("div", null,
        h("div", { class: "hh-head" }, h("button", { class: "btn big", onclick: () => showSearch() }, icon("back"), t("back")),
          h("div", { class: "prompt", style: { margin: 0 } }, t("pickup_code"))),
        display,
        keypad([...CODE_KEYS, "Clear", "back"], press, "letters"),
        h("p", { class: "hint" }, t("no_tag"))),
      results));
    S.keyHandler = (e) => {
      const k = e.key.toUpperCase();
      if (CODE_KEYS.includes(k)) press(k);
      else if (e.key === "Backspace") press("back");
      else if (e.key === "Enter") lookup();
      else if (e.key === "Escape") showSearch();
    };
  }

  function renderCheckoutTxn(txn, results) {
    const selectable = txn.children.filter((c) => c.status === "in");
    const chosen = new Set(selectable.map((c) => c.attendance_id));
    const button = h("button", { class: "btn huge purple" });
    const update = () => { button.disabled = !chosen.size; button.textContent = chosen.size ? t("check_out_n", { n: chosen.size }) : t("nothing_out"); };
    clear(results).append(
      h("div", { class: "prompt" }, t("co_family_code", { family: txn.household_name || t("family_default"), code: txn.code })),
      ...txn.children.map((c) => {
        const card = h("button", {
          class: "kid" + (c.status !== "in" ? " done" : chosen.has(c.attendance_id) ? " selected" : ""),
          style: { minHeight: "auto" },
          onclick: () => {
            if (c.status !== "in") return;
            chosen.has(c.attendance_id) ? chosen.delete(c.attendance_id) : chosen.add(c.attendance_id);
            card.classList.toggle("selected", chosen.has(c.attendance_id));
            update();
          },
        }, h("span", { class: "tick" }, icon("check")), h("div", { class: "nm" }, c.name), h("div", { class: "ln" }, className(c.class)),
        c.status === "out" ? h("div", { class: "state out" }, t("already_collected", { time: c.checked_out_at || "" })) : null);
        return card;
      }),
      button);
    button.onclick = async () => {
      button.disabled = true;
      try {
        const res = await api("POST", "/api/checkout", { attendance_ids: [...chosen] });
        show("success", h("div", { class: "center-col" },
          h("div", { class: "big-tick out" }, icon("check")),
          h("h1", { class: "success-title" }, t("collected_title")),
          h("div", { class: "success-names" }, res.checked_out.join(", ") || t("already_checked_out_title")),
          h("div", { class: "btn-row" }, h("button", { class: "btn big", onclick: () => showSearch() }, t("done")))));
        const token = S.pollToken;
        setTimeout(() => { if (S.pollToken === token) showSearch(); }, 3000);
      } catch (err) { toast(errorText(err), "error"); update(); }
    };
    update();
  }

  // ---------------------------------------------------------------------------
  // Visitor / new family
  // ---------------------------------------------------------------------------
  async function showVisitor(draft) {
    let classes = [];
    try { classes = await api("GET", "/api/classrooms"); } catch (e) { /* optional */ }
    const kidsOnly = !everyone();
    const g = {
      name: h("input", { placeholder: kidsOnly ? t("ph_fullname_kids") : t("ph_fullname"), autocapitalize: "words" }),
      phone: h("input", { type: "tel", inputmode: "tel", autocomplete: "off", placeholder: t("ph_mobile") }),
      pickup: h("input", { placeholder: t("ph_pickup"), autocapitalize: "words" }),
      notes: h("input", { placeholder: t("optional") }),
      chinese: h("input", { lang: "zh", autocomplete: "off", maxlength: 40 }),
      email: h("input", { type: "email", inputmode: "email", autocomplete: "off", autocapitalize: "off", spellcheck: false, maxlength: 254 }),
    };
    // The optional taps about the person filling it in (checkin/visitor_form.py has the same codes). Tapping a
    // chosen answer again clears it.
    let choiceCount = 0;
    const choice = (label, options, onchange) => {
      let value = "";
      const labelId = "choice-" + ++choiceCount;
      const buttons = options.map(([code, text]) => h("button", { type: "button", class: "choice", "aria-pressed": "false", "data-code": code,
        onclick: () => {
          value = value === code ? "" : code;
          buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.code === value)));
          if (onchange) onchange(value);
        } }, text));
      const el = h("div", { class: "choice-row" }, h("div", { class: "choice-label", id: labelId }, label),
        h("div", { class: "choices", role: "group", "aria-labelledby": labelId }, buttons));
      return { el, value: () => value };
    };
    const contactHow = h("div", { class: "contact-how hidden" });
    const about = {
      sex: choice(t("gender"), [["Male", t("male")], ["Female", t("female")]]),
      age: choice(t("age_group"), [["under20", t("age_u20")], ["20s30s", t("age_20s30s")], ["40s50s", t("age_40s50s")], ["60plus", t("age_60plus")]]),
      language: choice(t("pref_lang"), [["en", t("lang_en")], ["zh", t("lang_zh")], ["both", t("lang_both")]]),
      reason: choice(t("what_brings"), [["church", t("reason_church")], ["visiting", t("reason_visiting")], ["new", t("reason_new")]]),
      contact: choice(t("contact_q"), [["yes", t("yes")], ["no", t("no_thanks")]],
        (v) => contactHow.classList.toggle("hidden", v !== "yes")),
      how: choice(t("how"), [["whatsapp", t("whatsapp")], ["call", t("call")], ["email", t("email_word")]]),
    };
    contactHow.append(about.how.el, h("p", { class: "hint consent" }, t("consent", { name: settings().church_name || "Your Church" })));
    const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s.]+$/;
    const wrap = h("div");
    const members = [];
    const addMember = (isChild) => {
      const m = {
        first: h("input", { placeholder: t("first_name"), autocapitalize: "words" }),
        last: h("input", { placeholder: t("last_blank"), autocapitalize: "words" }),
        child: h("input", { type: "checkbox", checked: isChild }),
        dob: h("input", { type: "date" }),
        year: h("input", { placeholder: t("ph_year") }),
        cls: h("select", null, h("option", { value: "" }, t("choose_class")), classes.map((r) => h("option", { value: r.id }, r.name + (r.room ? " · " + r.room : "")))),
        alert: h("input", { type: "checkbox" }),
        note: h("input", { placeholder: t("ph_alert") }),
      };
      // Children's details only when the church uses them (classes, children-only mode, allergy alerts).
      const kidsDetails = kidsOnly || classes.length > 0;
      const medical = settings().medical_alerts === true;
      const childFields = h("div", null,
        kidsDetails ? h("div", { class: "row" }, field(t("dob"), m.dob), field(t("school_year"), m.year)) : null,
        classes.length ? field(t("class_label"), m.cls) : null,
        medical ? h("label", { class: "toggle" }, m.alert, t("alert_toggle")) : null,
        medical ? field(t("alert_note"), m.note, t("keep_brief")) : null);
      const sync = () => childFields.classList.toggle("hidden", !(kidsOnly || m.child.checked));
      m.child.addEventListener("change", sync);
      sync();
      members.push(m);
      wrap.append(h("div", { class: "child-block" },
        h("div", { class: "row" }, field(t(kidsOnly ? "member_kid" : "member_adult", { n: members.length }), m.first), field(t("last_name"), m.last)),
        kidsOnly ? null : h("label", { class: "toggle" }, m.child, t("is_child")),
        childFields));
    };
    if (kidsOnly) addMember(true);
    // Mark what's missing right on the field and move to it, rather than only a toast that's gone in 3 seconds.
    const invalid = (input, message) => {
      input.classList.add("invalid");
      input.setAttribute("aria-invalid", "true");
      let note = input.parentNode.querySelector(".field-error");
      if (!note) {
        note = h("div", { class: "field-error", id: input.id + "-error", role: "alert" });
        input.after(note);
        input.setAttribute("aria-describedby", note.id);
        input.addEventListener("input", () => {
          input.classList.remove("invalid");
          input.removeAttribute("aria-invalid");
          note.remove();
          input.removeAttribute("aria-describedby");
        }, { once: true });
      }
      note.textContent = message;
      return input;
    };
    const firstChild = () => (members[0] ? members[0].first : null);
    const check = () => {
      const bad = [];
      if (!g.name.value.trim()) bad.push(invalid(g.name, kidsOnly ? t("err_parent_name") : t("err_name")));
      if (!g.phone.value.trim()) bad.push(invalid(g.phone, t("err_phone")));
      const email = g.email.value.trim();
      if (email && !EMAIL_RE.test(email)) bad.push(invalid(g.email, t("err_email")));
      else if (!email && about.contact.value() === "yes" && about.how.value() === "email") bad.push(invalid(g.email, t("err_email_needed")));
      if (kidsOnly && !members.some((m) => m.first.value.trim()) && firstChild()) bad.push(invalid(firstChild(), t("err_one_child")));
      return bad;
    };
    const focusField = (input) => {
      input.scrollIntoView({ block: "center" });
      input.focus({ preventScroll: true });
    };
    // The main computer's own checks, shown on the field they belong to.
    const SERVER_FIELDS = { "Enter your name.": () => g.name, "Enter a valid mobile number.": () => g.phone, "Add at least one child.": firstChild,
      "Enter a valid email address, or leave it blank.": () => g.email,
      "Enter your email address, or choose another way to get in touch.": () => g.email };
    const save = async (force, button) => {
      const missing = check();
      if (missing.length) { focusField(missing[0]); return; }
      const body = {
        guardian_name: g.name.value, phone: g.phone.value, pickup_name: g.pickup.value, notes: g.notes.value, force,
        chinese_name: g.chinese.value, email: g.email.value, sex: about.sex.value(), age_group: about.age.value(),
        preferred_language: about.language.value(), visit_reason: about.reason.value(), wants_contact: about.contact.value(),
        contact_by: about.contact.value() === "yes" ? about.how.value() : "",
        members: members.map((m) => {
          const child = kidsOnly || m.child.checked;
          return { first_name: m.first.value, last_name: m.last.value, is_child: child,
            birthdate: child ? m.dob.value || null : null, school_year: child ? m.year.value : "",
            classroom_id: child ? m.cls.value || null : null, alert: child && m.alert.checked, alert_note: child ? m.note.value : "" };
        }),
      };
      button.disabled = true;
      try {
        const res = await api("POST", "/api/visitors", body);
        toast(t("welcome_toast"));
        openHousehold(res.household_id, res.person_ids);
      } catch (err) {
        button.disabled = false;
        if (err.status === 409 && err.data && err.data.existing) {
          dialog(t("dup_title"), h("div", { class: "results" },
            err.data.existing.map((fam) => h("button", { class: "result", onclick: () => { document.querySelector(".overlay")?.remove(); openHousehold(fam.id); } },
              h("div", null, h("div", { class: "fam" }, fam.name), h("div", { class: "who" }, fam.guardians.join(" · ")),
                h("div", { class: "kids" }, (fam.members || fam.children).join(", "))), h("div", { class: "phone" }, fam.phone)))),
          [{ label: t("cancel") }, { label: t("register_anyway"), class: "blue", onclick: () => save(true, button) }]);
        } else {
          const target = SERVER_FIELDS[err.message] && SERVER_FIELDS[err.message]();
          if (target) focusField(invalid(target, errorText(err)));
          else toast(errorText(err), "error", 5000);
        }
      }
    };
    const saveButton = h("button", { class: "btn huge green", onclick: (e) => save(false, e.currentTarget) }, kidsOnly ? t("save_continue") : t("save_checkin"));
    show("visitor", h("div", { class: "form", style: { paddingBottom: "40px" } },
      h("div", { class: "hh-head" }, h("button", { class: "btn big", onclick: () => showSearch() }, icon("back"), t("back")),
        h("h1", null, kidsOnly ? t("v_title_kids") : t("v_title"))),
      h("div", { class: "row" }, field(kidsOnly ? t("v_parent_name") : t("v_your_name"), g.name), field(t("ph_mobile"), g.phone)),
      h("div", { class: "row" }, field(t("chinese_name"), g.chinese, t("optional_hint")), field(t("email_label"), g.email, t("optional_hint"))),
      h("details", { class: "about", open: true },
        h("summary", null, kidsOnly ? t("about_you") : t("little_about"), h("span", { class: "hint" }, t("optional_taps"))),
        about.sex.el, about.age.el, about.language.el, about.reason.el, about.contact.el, contactHow),
      kidsOnly ? null : h("p", { class: "hint" }, t("anyone_with")),
      wrap,
      h("button", { class: "btn big", onclick: () => addMember(kidsOnly) }, icon("plus"), kidsOnly ? t("add_child") : t("add_member")),
      h("div", { class: "row" }, pickupOn() ? field(t("auth_pickup"), g.pickup) : null, field(t("notes"), g.notes)),
      h("div", { style: { marginTop: "22px" } }, saveButton)));
    // After a language switch: put back what was typed and tapped. Fields are matched by position, which is the
    // same on both languages' pages.
    if (draft) {
      while (members.length < draft.members) addMember(kidsOnly);
      const boxes = [...main.querySelectorAll(".form input, .form select")];
      draft.fields.forEach((f, i) => {
        const el = boxes[i];
        if (!el) return;
        if (el.type === "checkbox") { el.checked = f; el.dispatchEvent(new Event("change")); } else el.value = f;
      });
      const taps = [...main.querySelectorAll(".form .choice")];
      draft.taps.forEach((i) => { if (taps[i]) taps[i].click(); });
    }
  }

  // What is typed and tapped on the first-time form, so changing language does not wipe it.
  function visitorDraft() {
    const form = main.querySelector(".form");
    return {
      members: form.querySelectorAll(".child-block").length,
      fields: [...form.querySelectorAll("input, select")].map((el) => (el.type === "checkbox" ? el.checked : el.value)),
      taps: [...form.querySelectorAll(".choice")].map((el, i) => (el.getAttribute("aria-pressed") === "true" ? i : -1)).filter((i) => i >= 0),
    };
  }

  // The language button: change the kept choice, then draw the screen again in the new language.
  function switchLanguage() {
    const draft = S.screen === "visitor" && main.querySelector(".form") ? visitorDraft() : null;
    const nameText = S.screen === "name-search" ? (main.querySelector(".namebox input") || {}).value || "" : "";
    KS.setLang(KS.lang() === "zh" ? "en" : "zh");
    const starting = document.getElementById("starting");
    if (starting) starting.lastChild.textContent = " " + t("starting");
    renderBar();
    const off = document.getElementById("offline");
    if (off) { off.remove(); if (!S.online) setOnline(false); }
    ({
      search: () => showSearch(),
      "name-search": () => showNameSearch(nameText),
      household: () => (S.household ? renderHousehold() : showSearch()),
      success: () => (S.lastResult ? showSuccess(S.lastResult) : showSearch()),
      "no-session": () => showNoSession(),
      lock: () => showLock(),
      checkout: () => showCheckout(),
      visitor: () => showVisitor(draft),
    }[S.screen] || (() => {}))();
  }

  // ---------------------------------------------------------------------------
  // Menu: printers, problems, recent, rooms, lock
  // ---------------------------------------------------------------------------
  async function openMenu() {
    const drawer = h("div", { class: "drawer" });
    const overlay = h("div", { class: "overlay", style: { zIndex: 55 }, onclick: () => { overlay.remove(); drawer.remove(); } });
    document.body.append(overlay, drawer);
    const close = () => { overlay.remove(); drawer.remove(); };
    const sess = session();
    const [problems, recent, rooms] = await Promise.all([
      api("GET", "/api/print-problems").catch(() => []),
      api("GET", "/api/recent").catch(() => []),
      sess ? api("GET", "/api/room-counts?session_id=" + sess.id).catch(() => []) : [],
    ]);
    drawer.append(...nodes(
      h("div", { class: "hh-head" }, h("button", { class: "btn big", onclick: close }, icon("close"), t("close")),
        h("div", null, h("strong", null, S.status.station.name), h("div", { class: "hint" }, t("prints_to", { printer: stationPrinter() || "-" })))),
      h("h2", null, t("printers_h")),
      ...printers().map((p) => h("div", { class: "item" },
        h("span", { class: "dot " + (p.ok ? "ok" : p.ok === false ? "bad" : "warn") }),
        h("div", { class: "grow" }, h("strong", null, p.name), h("div", { class: "hint" }, p.message || "")),
        h("button", { class: "btn small", onclick: async () => {
          try { const job = await api("POST", `/api/printers/${p.name}/test`, {}); toast(t("test_sent", { printer: p.name })); trackJobToast(job.id); }
          catch (err) { toast(errorText(err), "error"); }
        } }, t("test_print")))),
      problems.length ? h("h2", null, t("problems_today")) : null,
      ...problems.map((j) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("strong", null, `${pickupOn() && j.code ? j.code + " · " : ""}${j.household || j.kind}`),
          h("div", { class: "hint" }, t("problem_line", { time: j.created_at.slice(11, 16), printer: j.printer, error: j.error || jobWord(j) }))),
        ...printers().filter((p) => p.ok).map((p) => h("button", { class: "btn small outline-blue", onclick: async () => {
          try { const job = await api("POST", `/api/jobs/${j.id}/retry`, { printer: p.name }); toast(t("printing_on", { printer: job.printer })); trackJobToast(job.id); close(); }
          catch (err) { toast(errorText(err), "error"); }
        } }, t("print_on", { printer: p.name }))),
        h("button", { class: "btn small", onclick: async () => {
          try {
            await api("POST", `/api/jobs/${j.id}/resolve`, { outcome: j.status === "unknown" ? "printed" : "dismissed" });
            toast(j.status === "unknown" ? t("marked_printed") : t("dismissed"));
            close();
          } catch (err) { toast(errorText(err), "error"); }
        } }, j.status === "unknown" ? t("labels_ok") : t("not_needed")))),
      h("h2", null, t("recent_h")),
      recent.length ? null : h("div", { class: "hint" }, t("none_yet")),
      ...recent.map((x) => h("div", { class: "item" },
        h("span", { class: "pill " + (x.status === "undone" ? "amber" : "green") }, pickupOn() ? x.code : x.time),
        h("div", { class: "grow" }, h("strong", null, x.children.join(", ") || t("undone_word")),
          h("div", { class: "hint" }, `${x.time} · ${x.household || ""}${x.status === "undone" ? t("undone_suffix") : ""}${x.job ? " · " + jobWord(x.job) : ""}`)),
        x.status === "committed" ? h("button", { class: "btn small outline-blue", onclick: () => { close(); reprintDialog(x); } }, t("reprint")) : null,
        x.status === "committed" ? h("button", { class: "btn small outline-amber", onclick: () => { close(); undoDialog(x.id, x.children); } }, t("undo")) : null)),
      sess ? h("h2", null, t("rooms_h", { name: sess.name })) : null,
      ...rooms.map((r) => h("div", { class: "item" }, h("div", { class: "grow" }, h("strong", null, className(r.class_name))),
        h("span", { class: "pill green" }, t("here_n", { n: r.present })), pickupOn() ? h("span", { class: "pill purple" }, t("collected_n", { n: r.collected })) : null)),
      h("h2", null, t("this_device")),
      S.status.station.until ? h("p", { class: "hint" }, t("one_time_until", { time: S.status.station.until.slice(11, 16) })) : null,
      (S.status.server_urls || []).length ? h("p", { class: "hint" }, t("other_ipads", { urls: S.status.server_urls.join("  or  ") })) : null,
      h("div", { class: "btn-row", style: { justifyContent: "flex-start" } },
        h("button", { class: "btn outline-blue", onclick: () => { close(); oneTimeDeviceDialog(); } }, t("ot_title")),
        h("button", { class: "btn", onclick: () => { close(); chooseSession(); } }, t("switch_service")),
        h("button", { class: "btn", onclick: () => location.reload() }, t("reload")),
        h("button", { class: "btn", onclick: () => { location.href = "/admin"; } }, t("admin")),
        h("button", { class: "btn red", onclick: async () => {
          await api("POST", "/api/logout", {}).catch(() => {});
          store.set("checkin.session", null);
          S.authEpoch++;
          close();
          showLock();
        } }, t("lock")))));
  }

  // ---------------------------------------------------------------------------
  // Global input handling and idle return
  // ---------------------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    S.lastActivity = Date.now();
    if (document.querySelector(".overlay")) return;
    if (S.keyHandler) S.keyHandler(e);
  });
  document.addEventListener("pointerdown", () => { S.lastActivity = Date.now(); }, true);
  document.addEventListener("gesturestart", (e) => e.preventDefault());

  setInterval(() => {
    const idle = (Date.now() - S.lastActivity) / 1000;
    const limit = Number(settings().idle_return_seconds || 45);
    if (["household", "name-search", "visitor", "checkout"].includes(S.screen) && idle > limit && !S.busy && !document.querySelector(".overlay")) {
      showSearch();
    } else if (S.screen === "search" && S.digits && idle > 25) {
      showSearch();
    }
    if (S.screen !== "lock") renderClock();
  }, 1000);

  function renderClock() {
    const el = bar.querySelector(".clock");
    if (el) el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  heartbeat();
})();
