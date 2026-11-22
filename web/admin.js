/* Administrator screens: people, classes, sync, printers, labels, backups, users, audit. */
(function () {
  "use strict";
  const { h, field, icon, clear, toast, store, download } = HB;
  const root = document.getElementById("admin");
  const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  let token = store.getSession("checkin.admin");
  let current = "dashboard";
  let content;

  const api = (method, path, body, opts = {}) => HB.api(method, path, body, Object.assign({ token }, opts)).catch((err) => {
    if (err.status === 401) { token = ""; store.setSession("checkin.admin", null); renderLogin(); }
    throw err;
  });
  const fail = (err) => toast(err.message || "Failed", "error", 5000);
  const today = () => new Date().toLocaleDateString("en-CA");
  // 2026-11-12 → 12 Nov 2026, and a timestamp → 12 Nov 2026, 19:37. Anything unreadable is shown as it came.
  const day = (iso) => { const d = new Date(String(iso).slice(0, 10) + "T00:00"); return isNaN(d) ? iso : d.toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" }); };
  const when = (iso) => { const d = new Date(String(iso).replace(" ", "T")); return isNaN(d) ? iso : `${day(String(iso))}, ${d.toLocaleTimeString("en-SG", { hour: "2-digit", minute: "2-digit", hour12: false })}`; };

  function table(columns, rows, render = {}) {
    if (!rows.length) return h("p", { class: "hint" }, "Nothing to show.");
    return h("div", { class: "scroll" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, columns.map((c) => h("th", null, typeof c === "string" ? c : c.label)))),
      h("tbody", null, rows.map((row) => h("tr", null, columns.map((c) => {
        const key = typeof c === "string" ? c : c.key;
        const value = render[key] ? render[key](row) : row[key];
        return h("td", null, value === null || value === undefined ? "" : value);
      }))))));
  }


  function select(options, value) {
    return h("select", null, options.map(([v, label]) => h("option", { value: v, selected: String(v) === String(value ?? "") }, label)));
  }

  // ---------------------------------------------------------------------------
  function renderLogin() {
    const name = h("input", { value: "Admin", autocomplete: "username" });
    const password = h("input", { type: "password", autocomplete: "current-password" });
    const submit = async () => {
      try {
        const res = await HB.api("POST", "/api/admin/login", { name: name.value, password: password.value }, { token: "" });
        token = res.token;
        store.setSession("checkin.admin", token);
        renderShell();
      } catch (err) { fail(err); }
    };
    password.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    clear(root).append(h("div", { class: "login", style: { gridColumn: "1 / -1" } },
      h("h1", null, "Check-in administration"), field("Name", name), field("Password", password),
      h("div", { style: { marginTop: "16px", display: "flex", gap: "10px" } },
        h("button", { class: "btn green", onclick: submit }, "Sign in"),
        h("a", { class: "btn ghost", href: "/" }, "Back to check-in"))));
    setTimeout(() => password.focus(), 30);
  }

  const SECTIONS = [
    ["dashboard", "Dashboard", "home"], ["people", "People", "people"],
    ["classes", "Classes & services", "calendar"], ["breeze", "Sync", "update"],
    ["export", "Sessions", "list"], ["reports", "Reports", "reports"],
    ["printers", "Printers & stations", "printer"], ["labels", "Label templates", "tag"], ["backup", "Backup & restore", "archive"],
    ["users", "Users & PINs", "lock"], ["settings", "Settings", "settings"], ["audit", "Audit log", "list"],
  ];

  // On a phone the menu folds away behind one button (as in the church database), so each page starts at the top.
  let setMenuOpen = () => {};
  function renderShell() {
    const toggle = h("button", { type: "button", class: "menu-toggle", "aria-expanded": "false", "aria-controls": "nav-links" }, icon("menu"), "Menu");
    const nav = h("nav", { class: "side", "aria-label": "Menu" },
      h("div", { class: "head" }, h("div", null, h("div", { class: "brand" }, "Church Check-in"), h("div", { class: "sub" }, "Administration")), toggle),
      h("div", { class: "links", id: "nav-links" },
        SECTIONS.map(([key, label, pic]) => h("button", { class: key === current ? "on" : "", "data-key": key, onclick: () => { go(key); window.scrollTo(0, 0); } }, icon(pic), label)),
        h("div", { class: "foot" },
          h("button", { onclick: () => { location.href = "/"; } }, icon("back"), "Check-in screen"),
          h("button", { onclick: async () => { await api("POST", "/api/logout", {}).catch(() => {}); token = ""; store.setSession("checkin.admin", null); renderLogin(); } }, "Sign out"))));
    setMenuOpen = (open) => {
      nav.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    toggle.addEventListener("click", () => setMenuOpen(!nav.classList.contains("open")));
    nav.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && nav.classList.contains("open")) { setMenuOpen(false); toggle.focus(); }
    });
    content = h("div", { class: "content" });
    clear(root).append(nav, content);
    go(current);
  }

  function go(key) {
    current = key;
    setMenuOpen(false);
    root.querySelectorAll("nav.side [data-key]").forEach((b) => {
      b.classList.toggle("on", b.dataset.key === key);
      if (b.dataset.key === key) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    clear(content).append(h("p", { class: "hint" }, h("span", { class: "spinner" }), " Loading…"));
    VIEWS[key]().catch((err) => { clear(content).append(h("div", { class: "err-box" }, err.message)); });
  }

  function page(title, lead, ...children) {
    clear(content).append(...[h("h1", null, title), lead ? h("p", { class: "lead" }, lead) : null, ...children].filter(Boolean));
  }

  const VIEWS = {};

  // ---------------------------------------------------------------------------
  VIEWS.dashboard = async () => {
    const o = await api("GET", "/api/admin/overview");
    page("Dashboard", `Version ${o.version} · running since ${when(o.started_at)} · data folder: ${o.data_folder}`,
      o.lan_urls && o.lan_urls.length ? h("div", { class: "ok-box" }, "iPads connect to: " + o.lan_urls.join("  or  ")) : null,
      ...o.warnings.map((w) => h("div", { class: "warn-box" }, w)),
      h("div", { class: "grid stats" },
        [["Children", o.children], ["Families", o.households], ["People", o.people], ["Visitors", o.visitors],
          ["Children without a class", o.children_without_class]].map(([l, n]) => h("div", { class: "stat" }, h("div", { class: "n" }, n), h("div", { class: "l" }, l)))),
      h("h2", null, "Sunday report"),
      o.sunday_report ? h("p", null, `${day(o.sunday_report.date)} · ${o.sunday_report.delivered ? "Delivered" : o.sunday_report.delivery_error ? "Delivery retry pending" : "Built, awaiting delivery"}${o.sunday_report.late ? " · late catch-up" : ""}`)
        : h("p", { class: "hint" }, "No Sunday report yet."),
      h("h2", null, "Today"),
      table(["Service", "Date", "Status", "Checked in"], o.sessions_today.map((s) => ({ Service: s.name, Date: day(s.date), Status: s.status ? s.status[0].toUpperCase() + s.status.slice(1) : "", "Checked in": s.checked_in }))),
      h("h2", null, "Printers"),
      table(["Printer", "Connection", "Status", "Queued"], o.printers.map((p) => ({ Printer: p.name, Connection: `${p.backend} ${p.address || ""}`, Status: p.message, Queued: p.queued }))),
      h("h2", null, "Last roster update"),
      o.last_import ? h("p", null, when(o.last_import.ts)) : h("p", { class: "hint" }, "None yet."),
      h("h2", null, "Last backup"),
      o.last_backup ? h("p", null, `${o.last_backup.name} (${Math.round(o.last_backup.bytes / 1024)} KB)`) : h("p", { class: "hint" }, "None yet."));
  };

  // ---------------------------------------------------------------------------
  VIEWS.people = async () => {
    const q = h("input", { placeholder: "Name, phone or VIS- id", "aria-label": "Search people" });
    const list = h("div");
    const detail = h("div");
    const [classes] = await Promise.all([api("GET", "/api/admin/classrooms")]);
    const spaced = (digits) => digits.length === 8 ? `${digits.slice(0, 4)} ${digits.slice(4)}` : digits;
    const search = async () => {
      const rows = await api("GET", "/api/admin/people?q=" + encodeURIComponent(q.value));
      clear(list).append(table(["Name", "Type", "Mobile", "Family", "Class", "Alert", "Active", ""], rows.map((p) => ({
        Name: `${p.first_name}${p.preferred_name ? " (" + p.preferred_name + ")" : ""} ${p.last_name}`,
        Type: p.source === "visitor" ? `${p.is_child ? "Child visitor" : "Visitor"}${p.visitor_id ? " · " + p.visitor_id : ""}`
          : (p.is_child ? "Child" : "Adult"),
        Mobile: (p.mobiles || []).map(spaced).join(", "),
        Family: p.household ? p.household.name : "", Class: (classes.find((c) => c.id === p.classroom_id) || {}).name || "",
        Alert: p.medical_alert ? "yes" : "", Active: p.active ? "yes" : "no", id: p.id,
      })), {
        // The name opens the person too: on a phone the Open column is off to the right of the table.
        Name: (r) => h("button", { class: "linklike", onclick: () => openPerson(r.id) }, r.Name),
        "": (r) => h("button", { class: "btn small", onclick: () => openPerson(r.id) }, "Open"),
      }));
    };
    const openPerson = async (id) => {
      const p = await api("GET", "/api/admin/people/" + id);
      const f = {
        preferred_name: h("input", { value: p.preferred_name || "" }),
        classroom_id: select([["", "(automatic from age / school year)"], ...classes.map((c) => [c.id, c.name])], p.classroom_id),
        school_year: h("input", { value: p.school_year || "" }),
        birthdate: h("input", { type: "date", value: p.birthdate || "" }),
        medical_alert: h("input", { type: "checkbox", checked: !!p.medical_alert }),
        medical_note: h("input", { value: p.medical_note || "" }),
        pickup_note: h("input", { value: p.pickup_note || "" }),
        notes: h("input", { value: p.notes || "" }),
        breeze_id: h("input", { value: p.breeze_id || "" }),
        active: h("input", { type: "checkbox", checked: !!p.active }),
        is_child: h("input", { type: "checkbox", checked: !!p.is_child }),
      };
      clear(detail).append(h("h2", null, `${p.first_name} ${p.last_name}`),
        h("p", { class: "hint" }, `${p.source === "visitor" ? "Visitor " + (p.visitor_id || "") : "Roster"} · phones: ${p.phones.join(", ") || "none"} · families: ${p.households.map((x) => x.name + " (" + x.role + ")").join(", ")}`),
        h("div", { class: "inline-form" },
          field("Preferred name", f.preferred_name), field("Class", f.classroom_id), field("School year", f.school_year),
          field("Date of birth", f.birthdate), field("Alert note", f.medical_note), field("Pickup note", f.pickup_note),
          field("Notes", f.notes), field("Member ID", f.breeze_id, p.source === "visitor" ? "(Sync fills this in)" : ""),
          h("label", { class: "check" }, f.medical_alert, "Allergy / medical alert"),
          h("label", { class: "check" }, f.is_child, "Child"),
          h("label", { class: "check" }, f.active, "Active"),
          h("button", { class: "btn green", onclick: async () => {
            try {
              await api("PUT", "/api/admin/people/" + id, {
                preferred_name: f.preferred_name.value, classroom_id: f.classroom_id.value || null, school_year: f.school_year.value,
                birthdate: f.birthdate.value || null, medical_alert: f.medical_alert.checked, medical_note: f.medical_note.value,
                pickup_note: f.pickup_note.value, notes: f.notes.value, breeze_id: f.breeze_id.value, active: f.active.checked,
                is_child: f.is_child.checked,
              });
              toast("Saved.");
              search();
            } catch (err) { fail(err); }
          } }, "Save")),
        h("h2", null, "Recent attendance"),
        table(["date", "name", "class_name", "status"], p.attendance),
        h("h2", null, "Remove personal data"),
        h("p", { class: "hint" }, "Anonymising removes the name, phone, birth date and notes but keeps anonymous attendance counts. It cannot be undone."),
        h("button", { class: "btn outline-red", onclick: async () => {
          const reason = prompt("Reason for anonymising (recorded in the audit log):");
          if (!reason) return;
          try { await api("POST", `/api/admin/people/${id}/anonymise`, { reason }); toast("Anonymised."); clear(detail); search(); } catch (err) { fail(err); }
        } }, "Anonymise this person"));
      detail.scrollIntoView({ behavior: "smooth" });
    };
    q.addEventListener("keydown", (e) => e.key === "Enter" && search());
    page("People", "Search the local roster by name, part of a mobile number or VIS- id. Changes here are local; the church database is the master record, and the next sync updates the synced fields again.",
      h("div", { class: "toolbar" }, h("div", { style: { flex: 1 } }, q), h("button", { class: "btn blue", onclick: search }, "Search")), list, detail);
    search();
  };

  // ---------------------------------------------------------------------------
  VIEWS.classes = async () => {
    const [classes, services, chms] = await Promise.all([api("GET", "/api/admin/classrooms"), api("GET", "/api/admin/services"),
      api("GET", "/api/admin/chms-services").catch(() => [])]);
    const classForm = (c = {}) => {
      const f = {
        name: h("input", { value: c.name || "" }), room: h("input", { value: c.room || "" }),
        min: h("input", { type: "number", value: c.min_age_months ?? "" }), max: h("input", { type: "number", value: c.max_age_months ?? "" }),
        years: h("input", { value: c.school_years || "", placeholder: "P1,P2" }),
        order: h("input", { type: "number", value: c.sort_order ?? 0 }), active: h("input", { type: "checkbox", checked: c.active !== 0 }),
      };
      return h("div", { class: "inline-form", style: { marginBottom: "10px" } },
        field("Class", f.name), field("Room", f.room), field("Min age (months)", f.min), field("Max age (months)", f.max),
        field("School years", f.years), field("Order", f.order),
        h("label", { class: "check" }, f.active, "Active"),
        h("button", { class: "btn " + (c.id ? "" : "green"), onclick: async () => {
          const body = { name: f.name.value, room: f.room.value, min_age_months: f.min.value, max_age_months: f.max.value,
            school_years: f.years.value, breeze_event_name: c.breeze_event_name || "", sort_order: f.order.value, active: f.active.checked };
          try { c.id ? await api("PUT", "/api/admin/classrooms/" + c.id, body) : await api("POST", "/api/admin/classrooms", body); toast("Saved."); go("classes"); }
          catch (err) { fail(err); }
        } }, c.id ? `Save (${c.children} assigned)` : "Add class"));
    };
    const serviceForm = (s = {}) => {
      const f = {
        name: h("input", { value: s.name || "" }), weekday: select([["", "(any day, start manually)"], ...WEEKDAYS.map((d, i) => [i, d])], s.weekday),
        start: h("input", { value: s.start_time || "", placeholder: "09:30" }), event: h("input", { value: s.breeze_event_name || "" }),
        active: h("input", { type: "checkbox", checked: s.active !== 0 }),
        // The church database's own services (1.12.0 or later). Picking one links by its number, so the names
        // needn't match. With no list (an older database, or it can't be reached) the name box decides, as before.
        linked: chms.length ? select([["", "(match by the event name)"], ...chms.map((x) => [x.id, x.name])], s.chms_service_id || "") : null,
      };
      return h("div", { class: "inline-form", style: { marginBottom: "10px" } },
        field("Service", f.name), field("Opens automatically on", f.weekday), field("Start time", f.start),
        f.linked ? field("Service in the church database", f.linked) : null,
        field("Event name in the church database", f.event), h("label", { class: "check" }, f.active, "Active"),
        h("button", { class: "btn " + (s.id ? "" : "green"), onclick: async () => {
          const picked = f.linked ? chms.find((x) => x.id === f.linked.value) : null;
          const body = { name: f.name.value, weekday: f.weekday.value, start_time: f.start.value, active: f.active.checked,
            breeze_event_name: picked ? picked.name : f.event.value,
            chms_service_id: f.linked ? f.linked.value : (s.chms_service_id || "") };
          try { s.id ? await api("PUT", "/api/admin/services/" + s.id, body) : await api("POST", "/api/admin/services", body); toast("Saved."); go("classes"); }
          catch (err) { fail(err); }
        } }, s.id ? "Save" : "Add service"));
    };
    page("Classes & services",
      "A child's class comes from the roster's class column, or a manual choice in People, or else these age / school-year rules.",
      h("h2", null, "Classes"), ...classes.map((c) => classForm(c)), classForm(),
      h("h2", null, "Services"),
      h("p", { class: "hint" }, "Each service becomes a check-in session on its day. Sync marks attendance on the church database event with this name."),
      ...services.map((s) => serviceForm(s)), serviceForm());
  };

  // ---------------------------------------------------------------------------
  VIEWS.export = async () => {
    const date = h("input", { type: "date", value: today() });
    const area = h("div");
    const load = async () => {
      const sessions = await api("GET", "/api/admin/sessions?date=" + date.value);
      clear(area).append(
        table(["Service", "Event", "Status", "Checked in", ""], sessions.map((s) => ({
          Service: `${s.name} ${s.start_time || ""}`, Event: s.breeze_event_name, Status: s.status, "Checked in": s.checked_in, s,
        })), { "": (r) => r.s.status === "open" ? h("button", { class: "btn small", onclick: async () => {
          await api("PUT", "/api/admin/sessions/" + r.s.id, { status: "closed" }).catch(fail); load();
        } }, "Close session") : "" }));
    };
    date.addEventListener("change", load);
    page("Sessions", "Each day's check-in sessions. Sync sends their attendance to the church database event named here.",
      h("div", { class: "toolbar" }, field("Date", date)), area);
    load();
  };

  // ---------------------------------------------------------------------------
  // Sync with the church database. The view, the routes and the settings keep their old internal name, breeze.
  const syncName = "the church database";
  function chmsForm(s) {
    const url = h("input", { value: s.chms_url || "http://127.0.0.1:8091" });
    const key = h("input", { type: "password", autocomplete: "off", placeholder: "paste the key" });
    const auto = h("input", { type: "checkbox", checked: true });
    const connect = async () => {
      try {
        await api("POST", "/api/admin/breeze/connect-chms", { url: url.value, api_key: key.value, auto: auto.checked },
          { timeout: 30000 });
        key.value = "";
        toast("Connected to the church database.");
        go("breeze");
      } catch (err) { fail(err); }
    };
    return [h("p", { class: "hint" }, "Make the key in the church database: Admin → Connector."),
      h("div", { class: "inline-form" }, field("Address", url, "(https://<NAS address>:8091, or http://127.0.0.1:8091 on this computer)"),
        field("Key", key), h("button", { class: "btn blue", onclick: connect }, "Connect")),
      h("label", { class: "check" }, auto, " Sync by itself every 15 minutes (untick only for a practice copy that should just Check)")];
  }

  // Leave-out rules: people matching any rule stay in the roster as inactive and are never shown at check-in.
  async function leaveOut(box) {
    const res = await api("GET", "/api/admin/breeze/rules");
    const rules = res.rules.map((r) => Object.assign({}, r));
    const valueOptions = res.column_values || {};
    const list = h("div");
    const summary = h("summary", null);
    const count = () => { summary.textContent = `Leave out at check-in (${rules.length} rule${rules.length === 1 ? "" : "s"})`; };
    const save = async () => {
      try { await api("PUT", "/api/admin/breeze/rules", { rules }); count(); toast("Saved. The next sync applies them."); }
      catch (err) { fail(err); }
    };
    const paint = () => {
      if (!res.columns.length) {
        clear(list).append(h("p", { class: "hint" }, "Press Check once, then come back here: the rules use the church database's columns."));
        return;
      }
      clear(list).append(...rules.map((rule, i) => {
        const col = select(res.columns.map((c) => [c, c]), rule.column);
        const op = select([["any", "has any value"], ["in", "is one of"]], rule.op);
        const values = h("input", { value: (rule.values || []).join(", "), placeholder: "e.g. DCEAS, REMOVED", "aria-label": "Values" });
        const hint = h("div", { class: "hint" });
        const update = () => {
          rule.column = col.value; rule.op = op.value;
          rule.values = values.value.split(",").map((v) => v.trim()).filter(Boolean);
          values.style.display = op.value === "in" ? "" : "none";
          const opts = valueOptions[col.value];
          hint.textContent = op.value === "in" && opts ? "Values in this column: " + opts.slice(0, 15).join(", ") : "";
        };
        [col, op, values].forEach((el) => el.addEventListener("change", update));
        update();
        return h("div", { class: "inline-form", style: { marginBottom: "8px" } },
          field("Leave out people whose", col), field("", op), h("div", null, values, hint),
          h("button", { class: "btn small outline-red", onclick: () => { rules.splice(i, 1); paint(); } }, "Remove"));
      }), h("div", { class: "toolbar" },
        h("button", { class: "btn small", onclick: () => { rules.push({ column: res.columns[0], op: "in", values: [] }); paint(); } }, icon("plus"), "Add a rule"),
        h("button", { class: "btn small green", onclick: save }, "Save rules")));
    };
    count();
    paint();
    clear(box).append(h("details", { style: { marginTop: "14px" } }, summary,
      h("p", { class: "hint" }, "For example the deceased, or people who transferred out. They stay in the roster as inactive, never deleted; Check shows how many each rule leaves out."),
      list));
  }

  VIEWS.breeze = async () => {
    const s = await api("GET", "/api/admin/breeze");
    const area = h("div");
    if (!s.connected) {
      page("Sync", "Connect check-in to the church database once. It then updates the roster, adds newcomers and marks attendance by itself.",
        ...chmsForm(s),
        h("p", { class: "hint" }, "The key stays on this computer (and in encrypted backups). It is never shown again or sent to the iPads."));
      return;
    }
    const since = h("input", { type: "date", value: s.since });
    since.addEventListener("change", async () => {
      try { await api("PUT", "/api/admin/breeze/settings", { since: since.value }); toast("Saved."); } catch (err) { fail(err); }
    });
    const auto = h("input", { type: "checkbox", checked: s.chms_auto_sync });
    auto.addEventListener("change", async () => {
      try { await api("PUT", "/api/admin/breeze/settings", { chms_auto_sync: auto.checked }); toast("Saved."); }
      catch (err) { fail(err); }
    });
    const check = async () => {
      try { await api("POST", "/api/admin/breeze/check", {}); pollSync(area); } catch (err) { fail(err); }
    };
    const disconnect = async () => {
      if (!confirm("Disconnect from the church database? Its key is deleted from this computer.")) return;
      try { await api("POST", "/api/admin/breeze/disconnect", {}); go("breeze"); } catch (err) { fail(err); }
    };
    const last = s.last ? `Last sync: ${s.last.finished || ""} · ${s.last.state === "done" ? "finished" : (s.last.error || s.last.state)}` : "Not synced yet.";
    // After the church database is restored from a backup: send check-ins from the backup's date again.
    const resendDay = h("input", { type: "date", max: today() });
    const resendArea = h("div");
    const resend = async () => {
      if (!resendDay.value) { toast("Choose the date of the backup first."); return; }
      try {
        const p = await api("GET", "/api/admin/breeze/resend?from=" + resendDay.value);
        const day = new Date(p.from + "T00:00").toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" });
        const send = async () => {
          try {
            const done = await api("POST", "/api/admin/breeze/resend", { from: p.from });
            toast(`${done.count} check-in${done.count === 1 ? "" : "s"} will be sent again at the next sync.`);
            go("breeze");
          } catch (err) { fail(err); }
        };
        clear(resendArea).append(h("div", { class: "warn-box" },
          p.count
            ? `${p.count} check-in${p.count === 1 ? "" : "s"} from ${day} on will be sent to the church database again at the next sync. Use this only after the church database is restored from a backup. `
            : `There are no check-ins from ${day} on to send again. `,
          p.count ? h("button", { class: "btn small blue", onclick: send }, "Send them again") : null, " ",
          h("button", { class: "btn small ghost", onclick: () => clear(resendArea) }, p.count ? "Cancel" : "Close")));
      } catch (err) { fail(err); }
    };
    const rulesArea = h("div");
    page("Sync with the church database",
      "Check-in syncs with the church database by itself every 15 minutes: the roster, newcomers and attendance. Press Check to see what the next sync will do, or Sync now from the plan.",
      h("div", { class: "ok-box" }, `Connected to the church database (${s.chms_url}). `,
        h("button", { class: "btn small ghost", onclick: disconnect }, "Disconnect")),
      h("label", { class: "check" }, auto, " Sync by itself every 15 minutes"),
      h("div", { class: "inline-form" },
        field("Send check-ins from", since, "(earlier days are never sent)"),
        h("button", { class: "btn big blue", onclick: check }, "Check")),
      h("p", { class: "hint", id: "breeze-last" }, last),
      h("div", { class: "inline-form" },
        field("Church database restored from a backup?", resendDay, "(the backup's date)"),
        h("button", { class: "btn ghost", onclick: resend }, "Send check-ins again from…")),
      resendArea,
      rulesArea,
      area);
    leaveOut(rulesArea).catch(fail);
    if (s.state === "running") pollSync(area);
    else if (s.state === "done" && s.kind === "check" && s.result) renderPlan(s.result, area);
  };

  async function pollSync(area) {
    const bar = h("div", { class: "hint" });
    clear(area).append(bar);
    for (;;) {
      let s;
      try { s = await api("GET", "/api/admin/breeze"); } catch (err) { fail(err); return; }
      if (s.state !== "running") {
        if (s.state === "error") clear(area).append(h("div", { class: "err-box" }, s.error));
        else if (s.kind === "check") renderPlan(s.result, area);
        else renderDone(s.result, area);
        return;
      }
      clear(bar).append(h("span", { class: "spinner" }), ` ${s.step}…` + (s.total ? ` ${s.done} of ${s.total}` : ""));
      if (current !== "breeze") return;
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  function renderPlan(res, area) {
    const p = res.people, t = p.totals;
    const choices = {};
    const syncBtn = h("button", { class: "btn big green" }, "Sync now");
    const refresh = () => { syncBtn.disabled = Object.values(choices).some((sel) => !sel.value); };
    const newcomers = res.newcomers.map((n) => {
      const sel = select([
        ...(n.default ? [] : [["", "Choose…"]]),
        ...n.candidates.map((c) => [`link:${c.breeze_id}`, `Same person as ${c.name} in ${syncName}` +
          ` (${[c.same_name && "same name", c.same_phone && "same mobile"].filter(Boolean).join(", ")})`]),
        ["add", `Add to ${syncName} as a new person`], ["skip", "Not now"]], n.default);
      sel.addEventListener("change", refresh);
      choices[n.id] = sel;
      return { Newcomer: n.name + (n.is_child ? " (child)" : ""), "First visit": n.first_visit || "", Action: sel };
    });
    const sessions = res.attendance.map((a) => {
      // "This day" is for a one-off event; "From now on" changes the service for every later week too.
      const choose = (name, scope) => h("button", {
        class: "btn small " + (scope === "day" ? "ghost" : "blue"), onclick: async () => {
          try {
            await api("PUT", "/api/admin/breeze/settings", { session_id: a.session_id, event_name: name, scope });
            toast(scope === "day" ? `${a.date} '${a.name}' goes to '${name}' in ${syncName} (this day only).`
              : `'${a.name}' now goes to '${name}' in ${syncName}, every week.`);
            await api("POST", "/api/admin/breeze/check", {}); pollSync(area);
          } catch (err) { fail(err); }
        } }, scope === "day" ? `“${name}” this day only` : `“${name}” from now on`);
      const fix = a.problem && a.breeze_events.length
        ? a.breeze_events.flatMap((name) => [choose(name, "day"), " ", choose(name, "service"), " "]) : null;
      return {
        Day: a.date, Service: a.name,
        Event: a.problem ? h("div", { class: "warn-box" }, a.problem, " ", fix) : a.event_name,
        "To send": a.to_send, "To remove": a.to_remove || "",
        "Waiting for newcomers": a.waiting_newcomers || "",
      };
    });
    syncBtn.onclick = async () => {
      const decisions = Object.fromEntries(Object.entries(choices).map(([id, sel]) => [id, sel.value]));
      if (!confirm(`Sync with ${syncName} now?`)) return;
      try { await api("POST", "/api/admin/breeze/sync", { decisions }); pollSync(area); } catch (err) { fail(err); }
    };
    refresh();
    clear(area).append(...[
      h("h2", null, `1. People from ${syncName}`),
      h("p", null, `${t.people_in_file} people in ${syncName}: ${t.people_new} new, ${t.people_updated} changed, ${t.people_unchanged} unchanged, ` +
        `${t.left_out} left out by your rules` + (p.not_in_breeze ? `, ${p.not_in_breeze} no longer in ${syncName} (hidden at check-in)` : "") + "."),
      p.warning ? h("div", { class: "warn-box" }, p.warning) : null,
      p.new_ids && (p.new_ids.people || p.new_ids.households) ? h("p", { class: "hint" },
        `First sync with ${p.ids_back ? "the older church database" : "Member IDs"}: ${p.new_ids.people} people and ` +
        `${p.new_ids.households} families move to ${p.ids_back ? "their old IDs" : "their Member IDs"}. ` +
        "Check-ins, families and classes stay as they are.") : null,
      p.id_warning ? h("div", { class: "warn-box" }, p.id_warning) : null,
      h("h2", null, "2. Newcomers"),
      newcomers.length ? table(["Newcomer", "First visit", "Action"], newcomers)
        : h("p", { class: "hint" }, "No newcomers waiting."),
      h("h2", null, "3. Attendance"),
      sessions.length ? table(["Day", "Service", "Event", "To send", "To remove", "Waiting for newcomers"], sessions)
        : h("p", { class: "hint" }, `Nothing to send since ${res.since}.`),
      h("div", { style: { marginTop: "18px" } }, syncBtn)].filter(Boolean));
  }

  function renderDone(res, area) {
    const s = res.summary;
    const lastLine = document.getElementById("breeze-last");
    if (lastLine) lastLine.textContent = "Last sync: just now · finished";
    clear(area).append(...[
      h("div", { class: "ok-box" }, "Sync finished."),
      h("ul", null,
        h("li", null, `Roster: ${s.people_new} new, ${s.people_updated} updated, ${s.deactivated} no longer in ${syncName}.`),
        s.new_ids ? h("li", null, `IDs: ${s.new_ids} people switched to the ID ${syncName} now uses; check-ins and families are kept.`) : null,
        h("li", null, `Newcomers: ${s.added} added to ${syncName}, ${s.linked} linked to an existing person.`),
        h("li", null, `Attendance: ${s.attendance_sent} marked in ${syncName}, ${s.attendance_already} were already there` +
          (s.attendance_removed ? `, ${s.attendance_removed} undone check-ins removed` : "") + ".")),
      ...s.problems.map((text) => h("div", { class: "warn-box" }, text)),
      res.backup ? h("p", { class: "hint" }, `Backup made first: ${res.backup}`) : null].filter(Boolean));
  }

  VIEWS.reports = async () => {
    const kinds = [["present", "Checked in now, by class"], ["totals", "Totals by service and class"], ["attendance", "Full attendance for the day"],
      ["visitors", "Visitors"], ["medical", "Allergy / medical alerts"], ["print_issues", "Print failures and reprints"]];
    const kind = select(kinds, "present");
    const date = h("input", { type: "date", value: today() });
    const area = h("div");
    const load = async () => {
      try {
        const data = await api("GET", `/api/admin/reports/${kind.value}?date=${date.value}`);
        clear(area).append(h("h2", null, `${data.title} · ${data.date}`), table(data.columns, data.rows));
      } catch (err) { fail(err); }
    };
    page("Reports", "Live lists and daily reports. Opening the medical, visitor and attendance reports is recorded in the audit log.",
      h("div", { class: "toolbar" }, field("Report", kind), field("Date", date),
        h("button", { class: "btn blue", onclick: load }, "Show"),
        h("button", { class: "btn", onclick: async () => {
          try {
            const res = await api("GET", `/api/admin/reports/${kind.value}?date=${date.value}&format=csv`, undefined, { raw: true });
            download(await res.blob(), `${kind.value}-${date.value}.csv`);
          } catch (err) { fail(err); }
        } }, "Download CSV")), area);
    load();
  };

  // ---------------------------------------------------------------------------
  VIEWS.printers = async () => {
    const [data, stations] = await Promise.all([api("GET", "/api/admin/printers"), api("GET", "/api/admin/stations")]);
    const detected = h("div");
    const names = data.printers.map((p) => p.name);
    const printerForm = (p = {}) => {
      const f = {
        name: h("input", { value: p.name || "", placeholder: "LEFT-PRINTER", disabled: !!p.name }),
        backend: select([["usb", "USB (recommended)"], ["tcp", "Network IP (port 9100)"], ["preview", "Preview (no printer, saves PNGs)"]], p.backend || "usb"),
        address: h("input", { value: p.address || "", placeholder: "USB serial (blank = only printer) / IP / folder" }),
        media: select(data.media.map((m) => [m.key, m.description]), p.media || "62"),
        enabled: h("input", { type: "checkbox", checked: p.enabled !== false }),
      };
      return h("div", { class: "inline-form", style: { marginBottom: "10px" } },
        field("Name", f.name), field("Connection", f.backend), field("Address", f.address), field("Label roll", f.media),
        h("label", { class: "check" }, f.enabled, "Enabled"),
        p.name ? h("div", null, h("div", { class: "hint" }, "Status"), h("strong", null, p.message || "…")) : null,
        h("button", { class: "btn " + (p.name ? "" : "green"), onclick: async () => {
          try {
            await api("POST", "/api/admin/printers", { name: f.name.value, backend: f.backend.value, address: f.address.value, media: f.media.value, enabled: f.enabled.checked });
            toast("Saved."); go("printers");
          } catch (err) { fail(err); }
        } }, p.name ? "Save" : "Add printer"),
        p.name ? h("button", { class: "btn", onclick: async () => {
          try { const s = await api("POST", `/api/admin/printers/${p.name}/refresh`, {}, { timeout: 15000 }); toast(`${p.name}: ${s.message}`); go("printers"); } catch (err) { fail(err); }
        } }, "Check status") : null,
        p.name ? h("button", { class: "btn blue", onclick: async () => {
          try { await api("POST", `/api/admin/printers/${p.name}/test`, {}); toast("Test label sent."); } catch (err) { fail(err); }
        } }, "Test print") : null,
        p.name ? h("button", { class: "btn outline-red", onclick: async () => {
          if (!confirm(`Remove printer ${p.name}?`)) return;
          try { await api("DELETE", "/api/admin/printers/" + p.name); go("printers"); } catch (err) { fail(err); }
        } }, "Remove") : null);
    };
    const stationRow = (s) => {
      const printer = select([["", "(none)"], ...names.map((n) => [n, n])], s.printer);
      const fallback = select([["", "(none)"], ...names.map((n) => [n, n])], s.fallback_printer);
      return h("div", { class: "inline-form", style: { marginBottom: "10px" } },
        h("div", null, h("strong", null, s.name), h("div", { class: "hint" }, `${!s.active ? "revoked" : s.expires_at ? "one-time, until " + s.expires_at.slice(0, 16) : "active"} · last seen ${s.last_seen || "never"} ${s.last_ip || ""}`)),
        field("Prints to", printer), field("Backup printer", fallback),
        h("button", { class: "btn", onclick: async () => {
          try { await api("PUT", "/api/admin/stations/" + s.id, { printer: printer.value, fallback_printer: fallback.value, active: !!s.active }); toast("Saved."); }
          catch (err) { fail(err); }
        } }, "Save"),
        s.active ? h("button", { class: "btn outline-red", onclick: async () => {
          if (!confirm(`Revoke ${s.name}? That device will need to be set up again.`)) return;
          await api("PUT", "/api/admin/stations/" + s.id, { printer: s.printer, fallback_printer: s.fallback_printer, active: false }).catch(fail);
          go("printers");
        } }, "Revoke") : null);
    };
    page("Printers & stations",
      "Printers are plugged into the main computer by USB. Each check-in station (an iPad or this computer) prints to one printer, with an optional backup.",
      h("h2", null, "Printers"), ...data.printers.map((p) => printerForm(p)), printerForm(),
      h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: async () => {
        try {
          const found = await api("GET", "/api/admin/printers/detect", undefined, { timeout: 20000 });
          clear(detected).append(found.length ? table(["model", "serial", "path"], found) : h("div", { class: "warn-box" },
            "No Brother printer found on USB. Check the cable and power, and that P-touch Editor Lite is off (its LED must be off)."));
        } catch (err) { fail(err); }
      } }, "Detect USB printers")), detected,
      h("h2", null, "Stations"),
      h("p", { class: "hint" }, "To add an iPad: open the check-in address in Safari on the iPad, sign in as administrator, and name the station there. For a busy Sunday, a volunteer can tap Menu → Add a one-time device at any desk instead: that device works until the end of the day and appears here as One-time 1, 2 …"),
      ...stations.map(stationRow));
  };

  // ---------------------------------------------------------------------------
  VIEWS.labels = async () => {
    const names = await api("GET", "/api/admin/templates");
    const pick = select(names.map((n) => [n, n]), "child");
    const text = h("textarea", { class: "code", spellcheck: false, "aria-label": "Label template" });
    const img = h("img", { alt: "Label preview" });
    const msg = h("div");
    const load = async () => {
      const t = await api("GET", "/api/admin/templates/" + pick.value);
      text.value = t.text;
      preview();
    };
    const preview = async () => {
      try {
        const res = await api("POST", `/api/admin/templates/${pick.value}/preview`, { text: text.value }, { raw: true });
        img.src = URL.createObjectURL(await res.blob());
        clear(msg);
      } catch (err) {
        clear(msg).append(h("div", { class: "err-box" }, "Template problem: check the JSON."));
      }
    };
    pick.addEventListener("change", load);
    page("Label templates", "Layouts are JSON files in the data folder (see templates/README.md). Edit, preview with sample data, then save. No rebuild needed.",
      h("div", { class: "toolbar" }, field("Template", pick),
        h("button", { class: "btn blue", onclick: preview }, "Preview"),
        h("button", { class: "btn green", onclick: async () => {
          try { await api("PUT", "/api/admin/templates/" + pick.value, { text: text.value }); toast("Saved. New labels use it now."); } catch (err) { fail(err); }
        } }, "Save"),
        h("button", { class: "btn", onclick: async () => {
          if (!confirm("Replace with the built-in version?")) return;
          try { await api("POST", `/api/admin/templates/${pick.value}/reset`, {}); load(); } catch (err) { fail(err); }
        } }, "Reset to default")),
      msg,
      h("div", { class: "two" }, text, h("div", null, h("div", { class: "preview-img" }, img),
        h("p", { class: "hint" }, "Preview at actual print resolution (300 dpi), as you read the label."))));
    load();
  };

  // ---------------------------------------------------------------------------
  VIEWS.backup = async () => {
    const info = await api("GET", "/api/admin/backups");
    const dest = select([["", "Only the data folder on this computer"], ...info.drives.map((d) => [d.path, `${d.label} (${d.path})`])], "");
    const custom = h("input", { placeholder: "or type a folder path, e.g. E:\\checkin-backups" });
    const pass = h("input", { type: "password", placeholder: "at least 10 characters" });
    const upload = h("input", { type: "file", accept: ".checkinbak" });
    const upPass = h("input", { type: "password", placeholder: "needed if the backup came from another computer" });
    const result = h("div");
    const chosen = async () => {
      if (upload.files[0]) return { data_b64: await HB.fileToBase64(upload.files[0]), passphrase: upPass.value || null };
      toast("Choose a backup file.", "error");
      return null;
    };
    const verify = async (body) => {
      try {
        const m = await api("POST", "/api/admin/backups/verify", body, { timeout: 120000 });
        clear(result).append(h("div", { class: "ok-box" }, `Backup OK: made ${m.created_at} (${m.reason}), ${m.verified.people} people, ${m.verified.attendance} attendance records, integrity ${m.verified.integrity}.`));
      } catch (err) { clear(result).append(h("div", { class: "err-box" }, err.message)); }
    };
    const restore = async (body) => {
      const word = prompt("Restoring replaces ALL current data with the backup (a safety backup is made first). Type RESTORE to continue.");
      if (word !== "RESTORE") return;
      try {
        const m = await api("POST", "/api/admin/restore", Object.assign({ confirm: "RESTORE" }, body), { timeout: 180000 });
        clear(result).append(h("div", { class: "ok-box" }, `Restored backup from ${m.created_at}. Safety backup: ${m.safety_backup || "none"}.`));
      } catch (err) { clear(result).append(h("div", { class: "err-box" }, err.message)); }
    };
    page("Backup & restore", `Backups are encrypted with the backup passphrase. Automatic backups are made before every sync and restore. Folder: ${info.folder}`,
      info.has_key ? null : h("div", { class: "warn-box" }, "No backup passphrase set yet."),
      h("h2", null, "Make a backup now"),
      h("div", { class: "inline-form" }, field("Also copy to", dest), field("Folder", custom),
        h("button", { class: "btn green", disabled: !info.has_key, onclick: async () => {
          try {
            const res = await api("POST", "/api/admin/backups", { dest_dir: custom.value || dest.value || null }, { timeout: 120000 });
            toast(`Backup saved${res.copied_to ? " and copied to " + res.copied_to : ""}.`);
            go("backup");
          } catch (err) { fail(err); }
        } }, "Back up now")),
      result,
      h("h2", null, "Backups on this computer"),
      table(["name", "bytes", "modified", ""], info.backups, { "": (b) => h("span", null,
        h("button", { class: "btn small", onclick: async () => {
          const res = await api("GET", "/api/admin/backups/" + b.name, undefined, { raw: true }).catch(fail);
          if (res) download(await res.blob(), b.name);
        } }, "Download"), " ",
        h("button", { class: "btn small", onclick: () => verify({ name: b.name }) }, "Verify"), " ",
        h("button", { class: "btn small outline-red", onclick: () => restore({ name: b.name }) }, "Restore")) }),
      h("h2", null, "Verify or restore a backup file"),
      h("div", { class: "inline-form" }, field("Backup file", upload), field("Its passphrase", upPass),
        h("button", { class: "btn", onclick: async () => { const b = await chosen(); if (b) verify(b); } }, "Verify"),
        h("button", { class: "btn outline-red", onclick: async () => { const b = await chosen(); if (b) restore(b); } }, "Restore")),
      h("h2", null, "Backup passphrase"),
      h("p", { class: "hint" }, "Changing it affects new backups only. Keep every passphrase you have used, so older backups can still be restored."),
      h("div", { class: "inline-form" }, field("New passphrase", pass),
        h("button", { class: "btn", onclick: async () => {
          try { await api("POST", "/api/admin/backup-passphrase", { passphrase: pass.value }); toast("Passphrase set."); go("backup"); } catch (err) { fail(err); }
        } }, "Set passphrase")));
  };

  // ---------------------------------------------------------------------------
  VIEWS.users = async () => {
    const users = await api("GET", "/api/admin/users");
    const name = h("input", { placeholder: "e.g. Sunday volunteers, Jordan Example" });
    const role = select([["volunteer", "Volunteer (PIN)"], ["admin", "Administrator (password)"]], "volunteer");
    const secret = h("input", { type: "password", placeholder: "PIN (4-8 digits) or password" });
    page("Users & PINs", "Volunteers unlock stations with a PIN. Each volunteer login needs a different PIN. Administrators use a name and password.",
      table(["name", "role", "active", "created_at", ""], users, { "": (u) => h("span", null,
        h("button", { class: "btn small", onclick: async () => {
          const value = prompt(u.role === "admin" ? `New password for ${u.name}:` : `New PIN for ${u.name}:`);
          if (!value) return;
          try { await api("PUT", "/api/admin/users/" + u.id, { secret: value }); toast("Changed. That user is signed out everywhere."); } catch (err) { fail(err); }
        } }, u.role === "admin" ? "Change password" : "Change PIN"), " ",
        h("button", { class: "btn small " + (u.active ? "outline-red" : ""), onclick: async () => {
          try { await api("PUT", "/api/admin/users/" + u.id, { active: !u.active }); go("users"); } catch (err) { fail(err); }
        } }, u.active ? "Deactivate" : "Reactivate")) }),
      h("h2", null, "Add a user"),
      h("div", { class: "inline-form" }, field("Name", name), field("Role", role), field("PIN / password", secret),
        h("button", { class: "btn green", onclick: async () => {
          try { await api("POST", "/api/admin/users", { name: name.value, role: role.value, secret: secret.value }); toast("Added."); go("users"); } catch (err) { fail(err); }
        } }, "Add")));
  };

  // ---------------------------------------------------------------------------
  const SETTING_INFO = [
    ["church_name", "Church name", "Printed on labels.", "text"],
    ["checkin_mode", "Check-in mode", "Individual (recommended): everyone can check in, and only the person who searched starts ticked; they tap anyone else who is here. Whole family: everyone in the family starts ticked. Children only: adults don't show at check-in.",
      [["individual", "Individual"], ["family", "Whole family"], ["children", "Children only"]]],
    ["preselect_children", "Pre-select everyone", "Whole family and children-only modes: tick everyone automatically; people untick anyone who is not here. Not used in individual mode.", "bool"],
    ["child_labels_per_child", "Labels per person", "Name labels printed for each person.", "number"],
    ["pickup_codes", "Pickup codes (children's ministry)", "Off: no codes, no parent tag, no check-out. On: each check-in gets a code, a parent pickup tag and a Check out screen.", "bool"],
    ["guardian_labels_per_checkin", "Pickup tags per check-in", "Only when pickup codes are on (0 = none).", "number"],
    ["medical_alerts", "Allergy / medical alerts", "Off: not shown anywhere. On: flagged children show an alert on screen and on their label, and the visitor form asks about allergies.", "bool"],
    ["print_alert_note", "Print short alert note", "Print the brief allergy/medical note on the child's label (never full details).", "bool"],
    ["chinese_button", "中文 button on the check-in screens", "Off: the screens are in English only. On: a 中文 / English button lets each device switch. Ask a fluent speaker to review the Chinese wording before enabling it.", "bool"],
    ["failover", "Printer failover", "auto: if a station's printer is offline or out of labels, print on the backup printer. ask: show the choice.", ["auto", "ask"]],
    ["success_return_seconds", "Success screen seconds", "How long the confirmation shows before returning to search.", "number"],
    ["idle_return_seconds", "Idle timeout seconds", "Return to the blank search screen after this long without a tap.", "number"],
    ["volunteer_session_hours", "PIN unlock lasts (hours)", "After this, stations ask for the PIN again.", "number"],
    ["allow_volunteer_visitors", "Volunteers can register visitors", "Show the New family / Visitor button on stations.", "bool"],
    ["child_max_age", "Oldest age for children-only check-in", "Only used in children-only mode.", "number"],
    ["country_code", "Country code", "Removed from the front of phone numbers when searching.", "text"],
    ["print_confirm", "Print confirmation", "per_label: wait for the printer to confirm every label (recommended). job: one confirmation. none: don't wait.", ["per_label", "job", "none"]],
    ["print_high_quality", "Print quality priority", "Off (recommended): about 2 s per label. On: slightly finer print, about 3 s per label.", "bool"],
    ["print_compression", "Compress print data", "Smaller USB transfers. Printer transfer compression can be left at its default setting.", "bool"],
    ["backup_keep", "Backups to keep", "Older automatic and manual backups in the data folder are removed.", "number"],
    ["retention_visitor_months", "Anonymise inactive visitors after (months)", "The clean-up runs by itself once a day, and when you press 'Run retention clean-up'.", "number"],
    ["retention_audit_months", "Keep audit log (months)", "", "number"],
    ["retention_print_payload_days", "Keep label contents of print jobs (days)", "Older print jobs keep their status but not the names.", "number"],
  ];

  VIEWS.settings = async () => {
    let values = await api("GET", "/api/admin/settings");
    const inputs = {};
    const rows = SETTING_INFO.map(([key, label, desc, kind]) => {
      let input;
      if (kind === "bool") input = h("input", { type: "checkbox", checked: !!values[key], style: { width: "24px", height: "24px" } });
      else if (Array.isArray(kind)) input = select(kind.map((k) => (Array.isArray(k) ? k : [k, k])), values[key]);
      else input = h("input", { type: kind === "number" ? "number" : "text", value: values[key] ?? "" });
      input.setAttribute("aria-label", label);
      inputs[key] = [input, kind];
      return h("label", { class: "setting" }, h("div", null, h("strong", null, label), h("div", { class: "d" }, desc)), input);
    });
    page("Settings", null, ...rows,
      h("div", { class: "toolbar" },
        h("button", { class: "btn green", onclick: async () => {
          const body = {};
          for (const [key, [input, kind]] of Object.entries(inputs)) body[key] = kind === "bool" ? input.checked : input.value;
          if (body.checkin_mode === "children" && values.checkin_mode !== "children") {
            if (!confirm("Children-only check-in hides all adults on the iPads, so only children can check in. Turn it on?")) return;
            body.confirm_children_only = true;
          }
          try { values = await api("PUT", "/api/admin/settings", body); toast("Settings saved."); } catch (err) { fail(err); }
        } }, "Save settings"),
        h("button", { class: "btn", onclick: async () => {
          if (!confirm("Anonymise old visitors and remove old audit entries and label contents according to the retention settings?")) return;
          try { const r = await api("POST", "/api/admin/retention/run", {}); toast(`Done. Visitors anonymised: ${r.visitors_anonymised}. Old audit entries removed: ${r.audit_entries_deleted}. Old label contents cleared: ${r.print_payloads_cleared}.`, "", 8000); } catch (err) { fail(err); }
        } }, "Run retention clean-up")));
  };

  // ---------------------------------------------------------------------------
  VIEWS.audit = async () => {
    const date = h("input", { type: "date", value: today() });
    const area = h("div");
    const load = async () => {
      const data = await api("GET", "/api/admin/audit?date=" + date.value);
      clear(area).append(table(data.columns, data.rows));
    };
    date.addEventListener("change", load);
    page("Audit log", "Every check-in, undo, reprint, check-out, sync, login and admin change.",
      h("div", { class: "toolbar" }, field("Date", date)), area);
    load();
  };

  if (token) renderShell(); else renderLogin();
})();
