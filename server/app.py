import os
import json
import functools
import re
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_from_directory
)

import requests

from config import Config
import database as db

app = Flask(__name__)
app.config.from_object(Config)

# Initialize database tables
with app.app_context():
    db.init_db()

# ─── Jinja Custom Filters ──────────────────────────────────────

@app.template_filter("dt_fmt")
def dt_fmt(val, time_part=None):
    """
    Formats date/time values uniformly as: DD-Mon-YY, HH:MM:SS AM/PM
    (e.g., 28-Nov-26, 01:47:23 PM) or DD-Mon-YY for date-only values.
    """
    if not val or str(val).strip() in ("", "None", "null", "—", "-", "PENDING", "N/A"):
        return "—"
    raw = str(val).strip()
    if time_part and str(time_part).strip() not in ("", "None", "null", "—", "-", "N/A"):
        raw = f"{raw} {str(time_part).strip()}"

    # Strip subseconds
    clean = re.sub(r"\.\d+", "", raw)
    clean = clean.replace("T", " ").replace("Z", "").strip()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %I:%M:%S %p",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%b-%y %I:%M:%S %p",
        "%d-%b-%Y %I:%M:%S %p",
        "%d-%b-%y, %I:%M:%S %p",
        "%d-%b-%Y, %I:%M:%S %p",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(clean, fmt)
            if any(k in fmt for k in ["%H", "%I"]):
                return dt.strftime("%d-%b-%y, %I:%M:%S %p")
            else:
                return dt.strftime("%d-%b-%y")
        except ValueError:
            continue
    return raw

# ─── Auth Decorators ──────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def api_auth_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr or ""
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")
        api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
        # Allow if admin is logged into session, local connection, or valid API key is supplied
        if session.get("admin_logged_in") or is_local:
            return f(*args, **kwargs)
        if not api_key or api_key != Config.API_KEY:
            return jsonify({"status": "error", "message": "Unauthorized: Invalid or missing API key"}), 401
        return f(*args, **kwargs)
    return decorated_function

# ─── Telegram Helper ──────────────────────────────────────────

def send_telegram_alert(text):
    token = Config.TELEGRAM_BOT_TOKEN
    chat_ids = [c.strip() for c in Config.TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    if not chat_ids and Config.TELEGRAM_CHAT_ID:
        chat_ids = [Config.TELEGRAM_CHAT_ID.strip()]
        
    if not token or not chat_ids:
        return
        
    for cid in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            print(f"[Telegram] Error: {e}")

# ─── Web UI Routes ────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == Config.ADMIN_USERNAME and password == Config.ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            flash("Welcome to VisaWin Control Hub!", "success")
            return redirect(url_for("dashboard"))

        else:
            flash("Invalid credentials! Please try again.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    stats = db.get_stats()
    recent_triggers = db.get_recent_triggers(limit=5)
    recent_queue = db.get_queue(limit=5)
    return render_template(
        "dashboard.html",
        stats=stats,
        recent_triggers=recent_triggers,
        recent_queue=recent_queue,
        api_key=Config.API_KEY
    )

@app.route("/accounts")
@login_required
def accounts_page():
    accounts = db.get_all_accounts()
    total_count = len(accounts)
    enabled_count = sum(1 for a in accounts if a.get("enabled", True) not in (False, 0, "0", "false", "no", "off"))
    disabled_count = total_count - enabled_count
    return render_template(
        "accounts.html",
        accounts=accounts,
        total_count=total_count,
        enabled_count=enabled_count,
        disabled_count=disabled_count
    )

@app.route("/queue")
@login_required
def queue_page():
    queue_items = db.get_queue(limit=50)
    triggers = db.get_recent_triggers(limit=30)
    slot_history = db.get_slot_history(limit=100)
    ofc_bookings = db.get_booking_records(stage="OFC", limit=100)
    consular_bookings = db.get_booking_records(stage="CONSULAR", limit=100)
    return render_template(
        "queue.html",
        queue_items=queue_items,
        triggers=triggers,
        slot_history=slot_history,
        ofc_bookings=ofc_bookings,
        consular_bookings=consular_bookings
    )

@app.route("/confirmations")
@login_required
def confirmations_page():
    confirmations = db.get_all_booked_confirmations()
    # Also check JSON file if DB has no records yet (e.g. freshly upgraded)
    if not confirmations:
        json_conf = Path(__file__).resolve().parent.parent / "confirmations" / "booked_confirmations.json"
        if json_conf.exists():
            try:
                confirmations = json.loads(json_conf.read_text(encoding="utf-8"))
            except Exception:
                confirmations = []
    return render_template("confirmations.html", confirmations=confirmations)

@app.route("/confirmations/pdf/<path:filename>")
@login_required
def download_confirmation_pdf(filename):
    conf_dir = Path(__file__).resolve().parent.parent / "confirmations"
    return send_from_directory(str(conf_dir), filename, as_attachment=False)

@app.route("/confirmations/export")
@login_required
def export_confirmations_csv():
    import csv
    import io
    from flask import Response
    confirmations = db.get_all_booked_confirmations()
    if not confirmations:
        json_conf = Path(__file__).resolve().parent.parent / "confirmations" / "booked_confirmations.json"
        if json_conf.exists():
            try:
                confirmations = json.loads(json_conf.read_text(encoding="utf-8"))
            except Exception:
                confirmations = []

    if not confirmations:
        flash("No data found to download report.", "warning")
        return redirect(url_for("confirmations_page"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Customer Name", "Username", "OFC City", "OFC Booked Date & Time",
        "Consular City", "Consular Booked Date & Time", "Status", "PDF Filename", "Created At"
    ])
    for c in confirmations:
        writer.writerow([
            c.get("id", ""),
            c.get("customer_name", ""),
            c.get("username", ""),
            c.get("ofc_city", ""),
            dt_fmt(c.get("ofc_date", "")),
            c.get("consular_city", ""),
            dt_fmt(c.get("consular_date", "")),
            c.get("status", "BOOKED"),
            c.get("pdf_filename", ""),
            dt_fmt(c.get("created_at", ""))
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=visawin_booked_confirmations_report.csv"}
    )

# ─── REST API Routes ──────────────────────────────────────────



@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "service": "US Visa Scheduler Server",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/api/stats", methods=["GET"])
@api_auth_required
def api_stats():
    return jsonify({"status": "success", "data": db.get_stats()})

@app.route("/api/accounts", methods=["GET"])
@api_auth_required
def api_get_accounts():
    enabled_only = request.args.get("enabled", "").lower() in ["1", "true"]
    role = request.args.get("role")
    accounts = db.get_all_accounts(enabled_only=enabled_only, role=role)
    return jsonify({"status": "success", "count": len(accounts), "accounts": accounts})

@app.route("/api/accounts/export", methods=["GET"])
@login_required
def api_export_accounts_csv():
    import csv
    import io
    from flask import Response
    accounts = db.get_all_accounts()
    if not accounts:
        flash("No data found to download report.", "warning")
        return redirect(url_for("accounts_page"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Customer Name", "Username", "Role", "Booking Status", "Enabled",
        "OFC Cities", "OFC Start Date", "OFC End Date",
        "Consular Cities", "Consular Start Date", "Consular End Date",
        "Booked OFC Date", "Booked Consular Date", "Primary ID", "App ID"
    ])
    for acc in accounts:
        ofc_cities = acc.get("ofcCities") or acc.get("ofc_cities") or []
        if isinstance(ofc_cities, list):
            ofc_cities_str = "; ".join(ofc_cities)
        else:
            ofc_cities_str = str(ofc_cities)

        consular_cities = acc.get("consularCities") or acc.get("consular_cities") or []
        if isinstance(consular_cities, list):
            consular_cities_str = "; ".join(consular_cities)
        else:
            consular_cities_str = str(consular_cities)

        writer.writerow([
            acc.get("id", ""),
            acc.get("customer_name", ""),
            acc.get("username", ""),
            acc.get("role", "RESERVED_BOOKING"),
            acc.get("booking_status", "IDLE"),
            "YES" if acc.get("enabled", True) in (True, 1, "1", "true") else "NO",
            ofc_cities_str,
            dt_fmt(acc.get("ofcStartDate") or acc.get("ofc_start_date")),
            dt_fmt(acc.get("ofcEndDate") or acc.get("ofc_end_date")),
            consular_cities_str,
            dt_fmt(acc.get("consularStartDate") or acc.get("consular_start_date")),
            dt_fmt(acc.get("consularEndDate") or acc.get("consular_end_date")),
            dt_fmt(acc.get("booked_ofc_date")),
            dt_fmt(acc.get("booked_consular_date")),
            acc.get("primaryId", ""),
            acc.get("appd", "")
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts_report.csv"}
    )

@app.route("/api/accounts/<username>", methods=["GET"])
@api_auth_required
def api_get_account(username):
    account = db.get_account_by_username(username)
    if not account:
        return jsonify({"status": "error", "message": f"Account '{username}' not found"}), 404
    return jsonify({"status": "success", "account": account})

@app.route("/api/accounts", methods=["POST"])
@api_auth_required
def api_add_account():
    data = request.get_json(force=True)
    if not data or not data.get("username"):
        return jsonify({"status": "error", "message": "Username is required"}), 400
        
    existing = db.get_account_by_username(data.get("username").strip())
    if existing:
        return jsonify({"status": "error", "message": f"Account '{data.get('username')}' already exists"}), 409
        
    try:
        db.add_account(data)
        return jsonify({"status": "success", "message": "Account added successfully"}), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/accounts/<username>", methods=["PUT"])
@api_auth_required
def api_update_account(username):
    data = request.get_json(force=True)
    if not data:
        return jsonify({"status": "error", "message": "Payload is required"}), 400
    try:
        db.update_account(username, data)
        return jsonify({"status": "success", "message": f"Account '{username}' updated successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/accounts/<username>", methods=["DELETE"])
@api_auth_required
def api_delete_account(username):
    try:
        db.delete_account(username)
        return jsonify({"status": "success", "message": f"Account '{username}' deleted successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/accounts/<username>/toggle", methods=["POST"])
@api_auth_required
def api_toggle_account(username):
    try:
        acc = db.get_account_by_username(username)
        if acc and (acc.get("booking_status") == "BOOKED SUCCESS" or acc.get("role") == "COMPLETED" or acc.get("booked")):
            return jsonify({"status": "error", "message": f"Account '{username}' is already BOOKED SUCCESS! Monitoring cannot be re-enabled."}), 400
        db.toggle_account(username)
        return jsonify({"status": "success", "message": f"Account '{username}' status toggled"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/accounts/<username>/mark-booked", methods=["POST"])
@api_auth_required
def api_mark_account_booked(username):
    data = request.get_json(force=True) or {}
    try:
        db.mark_account_booked(
            username=username,
            ofc_date=data.get("ofc_date", ""),
            consular_date=data.get("consular_date", ""),
            ofc_city=data.get("ofc_city", ""),
            consular_city=data.get("consular_city", "")
        )
        return jsonify({"status": "success", "message": f"Account '{username}' marked as BOOKED SUCCESS"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/targets", methods=["GET"])
@api_auth_required
def api_get_targets():
    """Returns consolidated targets so Desktop Monitor knows exactly what to poll."""
    targets = db.get_monitoring_targets()
    return jsonify({"status": "success", "targets": targets})

@app.route("/api/trigger", methods=["POST"])
@api_auth_required
def api_receive_trigger():
    """
    Called when a slot is found.
    Accepts:
    1. Direct format: {"city": "CHENNAI", "date": "2026-10-15", "time": "09:30", "slot_type": "OFC"}
    2. Legacy format: {"username": "...", "updates": {"match": {"city": "CHENNAI", "date": "2026-10-15"}}}
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"status": "error", "message": "No payload provided"}), 400

    city = data.get("city")
    slot_date = data.get("date")
    slot_time = data.get("time", "")
    slot_type = data.get("slot_type", "OFC")

    # Check for nested format in 'updates'
    if not city or not slot_date:
        updates = data.get("updates", {})
        match_info = updates.get("match", {}) or updates.get("slot", {}) or updates.get("cached_booking", {})
        city = city or match_info.get("city") or updates.get("city")
        slot_date = slot_date or match_info.get("date") or updates.get("date")
        slot_time = slot_time or match_info.get("time") or updates.get("time", "")

    if not city or not slot_date:
        return jsonify({"status": "error", "message": "City and date could not be determined from payload"}), 400

    quantity = data.get("quantity") or data.get("count") or 1
    discovered_by = data.get("polled_by") or data.get("source") or data.get("username") or "MONITOR"
    result = db.record_trigger_and_queue(city, slot_date, slot_time, slot_type, quantity=quantity, discovered_by=discovered_by)

    # Broadcast alert to Telegram
    alert_msg = (
        f"🚨 <b>VISA SLOT DETECTED!</b>\n"
        f"📍 <b>City:</b> {city}\n"
        f"📅 <b>Date:</b> {slot_date} {slot_time}\n"
        f"🎯 <b>Matched Accounts:</b> {result['matched_count']}\n"
        f"⚡ <i>Dispatched to Server Booking Queue</i>"
    )
    send_telegram_alert(alert_msg)

    return jsonify({
        "status": "success",
        "message": f"Trigger processed: {result['matched_count']} accounts queued for booking",
        "data": result
    })

@app.route("/api/queue", methods=["GET"])
@api_auth_required
def api_get_queue():
    status = request.args.get("status")
    limit = int(request.args.get("limit", 50))
    queue = db.get_queue(limit=limit, status=status)
    return jsonify({"status": "success", "count": len(queue), "queue": queue})

@app.route("/api/queue/<int:queue_id>/status", methods=["POST"])
@api_auth_required
def api_update_queue_status(queue_id):
    data = request.get_json(force=True)
    status = data.get("status")
    notes = data.get("notes", "")
    if not status:
        return jsonify({"status": "error", "message": "Status is required"}), 400
    db.update_queue_status(queue_id, status, notes)
    return jsonify({"status": "success", "message": f"Queue item {queue_id} status updated to {status}"})

@app.route("/api/queue/reset", methods=["POST"])
@api_auth_required
def api_reset_queue():
    db.clear_booking_queue()
    return jsonify({"status": "success", "message": "Booking queue successfully cleaned and reset"})

@app.route("/api/queue/<int:queue_id>", methods=["DELETE", "POST"])
@api_auth_required
def api_delete_queue_item(queue_id):
    ok = db.delete_queue_item(queue_id)
    if ok:
        return jsonify({"status": "success", "message": f"Task #{queue_id} deleted successfully"})
    return jsonify({"status": "error", "message": f"Failed to delete task #{queue_id}"}), 500

@app.route("/api/triggers/reset", methods=["POST"])
@api_auth_required
def api_reset_triggers():
    db.clear_triggers_history()
    return jsonify({"status": "success", "message": "Trigger history cleared"})

@app.route("/api/slots/history", methods=["GET"])
@api_auth_required
def api_get_slot_history():
    limit = int(request.args.get("limit", 100))
    history = db.get_slot_history(limit=limit)
    return jsonify({"status": "success", "count": len(history), "history": history})

@app.route("/api/slots/history/clear", methods=["POST"])
@api_auth_required
def api_clear_slot_history():
    db.clear_slot_history()
    return jsonify({"status": "success", "message": "Slot monitoring history cleared"})

@app.route("/api/slots/history/export", methods=["GET"])
@login_required
def api_export_slot_history_csv():
    import csv
    import io
    from flask import Response
    history = db.get_slot_history(limit=500)
    if not history:
        flash("No data found to download report.", "warning")
        return redirect(url_for("queue_page"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "City", "Slot Date", "Slot Time", "Quantity", "Slot Type", "Discovered At", "Polled By", "Status"])
    for item in history:
        writer.writerow([
            item.get("id"),
            item.get("city"),
            dt_fmt(item.get("slot_date")),
            item.get("slot_time", ""),
            item.get("quantity", 1),
            item.get("slot_type", "OFC"),
            dt_fmt(item.get("created_at", "")),
            item.get("discovered_by", ""),
            item.get("status", "AVAILABLE")
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=slot_monitoring_history_report.csv"}
    )

# ─── Booking Records & Attempts Endpoints ─────────────────────

@app.route("/api/bookings/record", methods=["POST"])
@api_auth_required
def api_record_booking():
    data = request.get_json(force=True) or {}
    stage = data.get("stage", "OFC")
    username = data.get("username", "")
    customer_name = data.get("customer_name", username)
    city = data.get("city", "")
    slot_date = data.get("slot_date", "")
    slot_time = data.get("slot_time", "")
    status = data.get("status", "ATTEMPTED")
    notes = data.get("notes", "")

    if not username:
        return jsonify({"status": "error", "message": "Username is required"}), 400

    rec_id = db.record_booking_attempt(
        stage=stage,
        username=username,
        customer_name=customer_name,
        city=city,
        slot_date=slot_date,
        slot_time=slot_time,
        status=status,
        notes=notes
    )
    return jsonify({
        "status": "success",
        "message": f"{stage} booking record saved",
        "record_id": rec_id
    })

@app.route("/api/bookings", methods=["GET"])
@api_auth_required
def api_get_bookings():
    stage = request.args.get("stage")
    limit = int(request.args.get("limit", 100))
    records = db.get_booking_records(stage=stage, limit=limit)
    return jsonify({"status": "success", "count": len(records), "records": records})

@app.route("/api/bookings/<int:record_id>", methods=["DELETE", "POST"])
@api_auth_required
def api_delete_booking(record_id):
    ok = db.delete_booking_record(record_id)
    if ok:
        return jsonify({"status": "success", "message": f"Booking record #{record_id} deleted successfully"})
    return jsonify({"status": "error", "message": f"Failed to delete record #{record_id}"}), 500

@app.route("/api/bookings/clear", methods=["POST"])
@api_auth_required
def api_clear_bookings():
    data = request.get_json(silent=True) or {}
    stage = request.args.get("stage") or data.get("stage")
    db.clear_booking_records(stage=stage)
    label = f"{stage} " if stage else ""
    return jsonify({"status": "success", "message": f"{label}Booking records history cleared"})

@app.route("/api/bookings/export", methods=["GET"])
@login_required
def api_export_bookings_csv():
    import csv
    import io
    from flask import Response
    stage = (request.args.get("stage") or "OFC").upper()
    records = db.get_booking_records(stage=stage, limit=500)
    if not records:
        flash("No data found to download report.", "warning")
        return redirect(url_for("queue_page"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Stage", "Customer Name", "Username", "Slot City",
        "Slot Date", "Slot Time", "Status", "Details / Notes", "Date & Time Recorded"
    ])
    for item in records:
        writer.writerow([
            item.get("id"),
            item.get("stage", stage),
            item.get("customer_name", ""),
            item.get("username", ""),
            item.get("city", ""),
            dt_fmt(item.get("slot_date")),
            item.get("slot_time", ""),
            item.get("status", ""),
            item.get("notes", ""),
            dt_fmt(item.get("created_at", ""))
        ])
    filename = f"{stage.lower()}_bookings_history_report.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

