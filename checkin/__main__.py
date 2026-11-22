"""Command line: python -m checkin <command>

  serve           Run the check-in server (main computer)
  printers        List Brother printers connected by USB and their status
  print-test      Phase 1 hardware test: print numbered labels silently and report timings
  backup          Make an encrypted backup (optionally copy to a folder / USB drive)
  restore FILE    Restore a backup (for example on a replacement computer)
  reset-admin     Create or reset an administrator from this computer's console
  sync            Check what a sync with the church database would change; --run to sync
  sample-roster   Write a fake roster CSV for testing
"""

import argparse
import csv
import datetime as dt
import getpass
import json
import logging
import sys
import time
from pathlib import Path


from .network import lan_addresses as _lan_addresses

log = logging.getLogger("checkin")


def cmd_serve(args):
    import threading

    from . import tls
    from .app import App
    from .server import make_server

    app = App(args.data)
    host = args.bind or app.config.get("bind", "0.0.0.0")
    port = int(args.port or app.config.get("port", 8080))
    https_port = app.config.get("https_port")
    # Bound to this computer only (--bind 127.0.0.1): no other device can connect, so offer no network address.
    addresses = [] if host in ("127.0.0.1", "localhost", "::1") else _lan_addresses()
    secure = None
    if https_port:
        # The iPads use HTTPS; plain HTTP stays on for this computer only (kiosk, admin shortcut, update script).
        names, ips = tls.certificate_names(app.config, addresses)
        context = tls.server_context(tls.ensure_certificates(app.paths.keys, names, ips))
        secure = make_server(app, host, int(https_port), context)
        server = make_server(app, "127.0.0.1", port)
        url_port = f"https://{{}}:{https_port}/"
    else:
        server = make_server(app, host, port)
        url_port = f"http://{{}}:{port}/"
    app.lan_urls = [url_port.format(address) for address in addresses]
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Asked again whenever a one-time device is added: the computer may have moved to another network.
        app.lan_url_source = lambda: [url_port.format(address) for address in _lan_addresses()]
    app.start()
    log.info("listening on port %s%s; iPads use %s", port, f" and {https_port} (HTTPS)" if secure else "",
             ", ".join(app.lan_urls) or "(no network address)")
    print(f"Church Check-in is running. Data folder: {app.paths.data}")
    print(f"  On this computer:  http://localhost:{port}/      (admin: http://localhost:{port}/admin)")
    for url in app.lan_urls:
        print(f"  From the iPads:    {url}")
    if secure:
        print(f"  iPad certificate:  {app.lan_urls[0] if app.lan_urls else f'https://<this computer>:{https_port}/'}ca.crt"
              "   (install once; see docs/SETUP-WINDOWS.md)")
        threading.Thread(target=secure.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                         name="https").start()
    if app.needs_setup():
        print("  First run: open http://localhost:%d/ on THIS computer to complete setup." % port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping…")
    finally:
        server.shutdown()
        if secure:
            secure.shutdown()
        app.stop()


def cmd_printers(args):
    from .printing.backends import UsbBackend, discover_usb

    devices = discover_usb()
    if not devices:
        print("No Brother printer found on USB.")
        print("Check: USB cable connected, printer on, P-touch Editor Lite OFF (its LED must be off).")
        if sys.platform != "win32":
            print("On macOS/Linux the 'pyusb' package and libusb are needed (pip install pyusb; brew install libusb).")
        return 1
    for device in devices:
        status = UsbBackend(device["address"] or None).status()
        print(f"{device['model'] or 'Brother printer'}  serial={device['serial'] or '-'}")
        print(f"    path:   {device['path']}  (driver: {device.get('driver', '-')})")
        print(f"    status: {status.message}  (media {status.media_width}mm {status.media_type}, model {status.model})")
    return 0


def cmd_print_test(args):
    """Phase 1 go/no-go: N numbered labels, silently, with per-label timing and printer confirmation."""
    from .labels import LabelRenderer
    from .config import DEFAULT_TEMPLATES
    from .printing.backends import make_backend
    from .printing.raster import MEDIA, build_job, check_media

    media = MEDIA[args.media]
    backend = make_backend(args.backend, args.address, args.media)
    renderer = LabelRenderer(Path(args.templates) if args.templates else DEFAULT_TEMPLATES)
    template = renderer.load("test")
    template["media"] = args.media
    status = check_media(backend.status(), media)
    print(f"Printer status: {status.message} (model {status.model}, media {status.media_width}mm {status.media_type})")
    if not status.ok:
        print("Fix the printer and run the test again.")
        return 2
    report_path = Path(args.report or f"print-test-{dt.datetime.now():%Y%m%d-%H%M%S}.csv")
    rows, failures = [], 0
    started_all = time.time()
    number = 1
    while number <= args.count:
        batch = list(range(number, min(args.count, number + args.batch - 1) + 1))
        pages = []
        for n in batch:
            now = dt.datetime.now()
            pages.append(renderer.render(template, {
                "printer": args.address or args.backend.upper(), "number": f"{n}/{args.count}",
                "media": media.description, "date": now.strftime("%a %d %b %Y"), "time": now.strftime("%H:%M:%S"),
                "church": "Print test"}))
        data = build_job(pages, media, compress=args.compress, high_quality=args.quality)
        started = time.time()
        result = backend.print_job(data, len(pages), args.confirm, media)
        elapsed = time.time() - started
        ok = result.status == "printed"
        failures += 0 if ok else 1
        label = f"{batch[0]}" if len(batch) == 1 else f"{batch[0]}-{batch[-1]}"
        print(f"  label {label:>7}: {result.status:<8} {elapsed:5.2f}s  {result.message}")
        rows.append({"labels": label, "status": result.status, "seconds": f"{elapsed:.2f}", "message": result.message,
                     "confirmed": result.confirmed, "printer_log": json.dumps(result.log, default=str)})
        if not ok and not args.keep_going:
            print("Stopping at the first failure (use --keep-going to continue).")
            break
        number += len(batch)
        if args.pause:
            time.sleep(args.pause)
    total = time.time() - started_all
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["labels"])
        writer.writeheader()
        writer.writerows(rows)
    times = sorted(float(r["seconds"]) for r in rows) or [0]
    print(f"\nDone: {len(rows)} jobs, {failures} problem(s), total {total:.1f}s, "
          f"median {times[len(times) // 2]:.2f}s, slowest {times[-1]:.2f}s per job.")
    print(f"Report: {report_path.resolve()}")
    return 0 if failures == 0 else 3


def cmd_backup(args):
    from .app import App
    app = App(args.data)
    result = app.backups.create("manual-cli", args.out)
    print(f"Backup written: {result['path']}")
    if result["copied_to"]:
        print(f"Copied to:      {result['copied_to']}")
    app.db.close()
    return 0


def cmd_restore(args):
    from .app import App
    data = Path(args.file).read_bytes()
    app = App(args.data)
    passphrase = getpass.getpass("Backup passphrase: ")
    manifest = app.backups.verify(data, passphrase)
    counts = manifest.get("counts", {})
    print(f"Backup from {manifest['created_at']} ({counts.get('people')} people, {counts.get('attendance')} "
          f"attendance records). Integrity: {manifest['verified']['integrity']}")
    if input("Type RESTORE to replace the data on this computer: ").strip() != "RESTORE":
        print("Cancelled.")
        return 1
    app.restore(data, passphrase, user_name="console")
    print("Restored. Start the app normally.")
    app.stop()
    return 0


def cmd_reset_admin(args):
    from .app import App
    from .auth import hash_secret
    app = App(args.data)
    password = getpass.getpass(f"New password for administrator '{args.name}': ")
    app.auth.validate_secret("admin", password)
    user = app.db.one("SELECT id FROM user WHERE name=?", (args.name,))
    if user:
        with app.db.tx() as conn:
            conn.execute("UPDATE user SET secret_hash=?, active=1, role='admin', failed_attempts=0, "
                         "locked_until=NULL WHERE id=?",
                         (hash_secret(password), user["id"]))
            from .db import audit
            audit(conn, "user.reset_admin_console", "user", user["id"], {"name": args.name})
    else:
        app.auth.create_user(args.name, "admin", password)
    print("Administrator ready.")
    app.db.close()
    return 0


def cmd_sync(args):
    """Check (read-only) or run the sync with the church database from the console, as Admin → Sync does."""
    from .app import App
    app = App(args.data)
    state = app.breeze.start("sync" if args.run else "check", wait=True)
    if state["state"] == "error":
        print("Problem:", state["error"])
        return 1
    result = state["result"]
    if args.run:
        print(json.dumps(result["summary"], indent=1))
    else:
        totals = result["people"]["totals"]
        print(f"People: {totals['people_new']} new, {totals['people_updated']} updated, "
              f"{totals['people_unchanged']} unchanged, {result['people']['not_in_breeze']} no longer in the church database")
        for item in result["newcomers"]:
            print(f"Newcomer #{item['id']}: {len(item['candidates'])} possible match(es); "
                  f"default {item['default'] or 'choose'}")
        for item in result["attendance"]:
            print(f"{item['date']} {item['name']} → {item.get('instance_id') and item['event_name'] or item['problem']}: "
                  f"{item['to_send']} to send, {item['to_remove']} to remove, {item['waiting_newcomers']} newcomers")
        print(f"Attendance since {result['since']}. Calls: {state.get('calls')}. Nothing was changed.")
    app.db.close()
    return 0


def cmd_sample(args):
    from .sample import write_sample
    count = write_sample(args.out, args.families, args.format, args.seed)
    print(f"Wrote {count} rows to {args.out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m checkin", description="Church Check-in")
    parser.add_argument("--data", help="data folder (default: ./data next to the app, or CHECKIN_DATA)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the check-in server")
    p.add_argument("--port", type=int)
    p.add_argument("--bind")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("printers", help="list USB Brother printers")
    p.set_defaults(func=cmd_printers)

    p = sub.add_parser("print-test", help="Phase 1: print numbered test labels")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--batch", type=int, default=1, help="labels per job (4 = a typical family)")
    p.add_argument("--backend", default="usb", choices=["usb", "tcp", "preview"])
    p.add_argument("--address", help="USB serial number, IP address, or preview folder")
    p.add_argument("--media", default="62")
    p.add_argument("--confirm", default="per_label", choices=["per_label", "job", "none"])
    p.add_argument("--compress", action="store_true")
    p.add_argument("--quality", action="store_true", help="print quality priority (slower)")
    p.add_argument("--pause", type=float, default=0.0, help="seconds between jobs")
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--templates", help="folder with test.json (default: built-in)")
    p.add_argument("--report", help="CSV report path")
    p.set_defaults(func=cmd_print_test)

    p = sub.add_parser("backup", help="make an encrypted backup")
    p.add_argument("--out", help="also copy to this folder (for example E:\\)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore an encrypted backup")
    p.add_argument("file")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("reset-admin", help="create or reset an administrator")
    p.add_argument("name")
    p.set_defaults(func=cmd_reset_admin)

    p = sub.add_parser("sync", help="check what a sync with the church database would change (add --run to sync)")
    p.add_argument("--run", action="store_true", help="actually sync (otherwise read-only check)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("sample-roster", help="write a fake roster CSV")
    p.add_argument("--families", type=int, default=120)
    p.add_argument("--format", choices=["people", "children"], default="people")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default="sample-roster.csv")
    p.set_defaults(func=cmd_sample)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    from .app import RestoreBlocked, UpgradeBlocked
    try:
        return args.func(args) or 0
    except UpgradeBlocked as exc:
        print(f"Not started: {exc}", file=sys.stderr)
        return 2
    except RestoreBlocked as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
