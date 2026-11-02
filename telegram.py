"""
telegram.py — Professional, spam-free Telegram Bot API notifications for VisaBot.

Designed for high-priority alerts:
- Slot Matched / Found
- Booking Started (Sniper engaged)
- Booking Confirmed (Success)
- Booking Missed / Failed
- Consolidated startup summary

Routine transient cooldowns and background retries are kept in logs only
to avoid spamming the channel.
"""

import os
import sys
import html
import requests
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_CHAT_IDS = []
API_BASE = "https://api.telegram.org/bot"


def _refresh_config() -> None:
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_IDS, API_BASE
    load_dotenv()
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    _telegram_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    if _telegram_chat_ids:
        TELEGRAM_CHAT_IDS = [c.strip() for c in _telegram_chat_ids.split(",") if c.strip()]
    else:
        TELEGRAM_CHAT_IDS = [TELEGRAM_CHAT_ID] if TELEGRAM_CHAT_ID else []
    API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


_refresh_config()


def _post_to_telegram(chat_id: str, text: str) -> bool:
    _refresh_config()
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("⚠️ Telegram bot token or chat id not configured.")
        return False
    url = f"{API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            try:
                body = r.json()
            except Exception:
                body = r.text
            print(f"❌ Telegram API returned {r.status_code}: {body}")
            return False
        return True
    except Exception as e:
        print(f"❌ Failed to send Telegram message: {e}")
        return False


def _notification_label(message: str) -> str:
    text = (message or "").replace("<b>", "").replace("</b>", "").replace("\n", " ")
    compact = " ".join(text.split())
    upper = compact.upper()

    if "MONITORING ACTIVE" in upper or "MONITORING STARTED" in upper:
        return "monitor started"
    if "MONITORING STOPPED" in upper:
        return "monitor stopped"
    if "SLOT" in upper and ("MATCH" in upper or "FOUND" in upper or "DETECTED" in upper):
        return "slot matched"
    if "BOOKING IN PROGRESS" in upper or "BOOKING STARTED" in upper:
        return "booking started"
    if "BOOKING FAILED" in upper or "BOOKING MISSED" in upper:
        return "booking failed"
    if "BOOKED SUCCESSFULLY" in upper or "BOOKING SUCCESS" in upper:
        return "booking success"
    if "LOGIN FAILED" in upper:
        return "login failed"
    return "notification"


def send(message: str, emoji: str = "") -> bool:
    """Send a message to configured Telegram chats. Returns True if at least one send succeeded."""
    _refresh_config()
    try:
        from src.common.utils import sanitize_text
        clean_msg = sanitize_text(message)
    except Exception:
        clean_msg = str(message or "")

    clean_emoji = str(emoji or "").strip()
    text = f"{clean_emoji} {clean_msg}" if clean_emoji else clean_msg

    if not TELEGRAM_CHAT_IDS:
        print("⚠️ No TELEGRAM_CHAT_ID(S) configured. Skipping message.")
        return False

    ok_any = False
    for cid in TELEGRAM_CHAT_IDS:
        ok = _post_to_telegram(cid, text)
        ok_any = ok_any or ok
    if ok_any:
        label = _notification_label(clean_msg)
        print(f"Telegram - {label}")
    return ok_any


def send_slack(msg: str):
    """Compatibility alias for slot match alerts."""
    return send(f"🎯 <b>QUALIFIED SLOT MATCH FOUND</b>\n━━━━━━━━━━━━━━━━━━\n{msg}")


def send_slack_error(msg: str):
    """Filter out routine transient cooldowns to prevent chat spam; send only critical failures."""
    # Always preserve full message in console/orchestrator logs
    print(f"⚠️ Slot Monitor Log: {msg}")

    msg_low = str(msg).lower()
    # Anti-spam filter: Suppress routine retry / transient / cooldown notifications
    if any(term in msg_low for term in ["cooling down", "waf block", "transient", "high traffic", "waiting room"]):
        return False

    # Format real fatal login failure cleanly
    lines = [
        "⚠️ <b>ACCOUNT LOGIN ALERT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"ℹ️ {html.escape(str(msg))}",
        "━━━━━━━━━━━━━━━━━━",
        "🔄 <i>System is automatically recovering session.</i>"
    ]
    return send("\n".join(lines))


def _fmt_field(label: str, value: str) -> str:
    clean = html.escape(str(value if value is not None and str(value).strip() else "N/A"))
    return f"<b>{html.escape(str(label))}:</b> {clean}"


def format_slack_message(customer, chosen_ofc, chosen_consular, matched_ofc_city, matched_consular_city):
    ofc_cities_str = ", ".join(customer.get("ofc_cities", [])) or "N/A"
    consular_cities_str = ", ".join(customer.get("consular_cities", [])) or "N/A"
    ofc_end = customer.get("ofc_end")
    ofc_end_str = ofc_end.strftime("%Y-%m-%d") if ofc_end else "N/A"

    parts = [
        _fmt_field("Customer", customer.get('customer_name') or customer.get('username') or 'N/A'),
        _fmt_field("Requested OFC", ofc_cities_str),
        _fmt_field("Requested Consular", consular_cities_str),
        _fmt_field("OFC Deadline", ofc_end_str),
    ]

    if matched_ofc_city and chosen_ofc:
        parts += [
            "",
            _fmt_field("Matched OFC City", matched_ofc_city),
            _fmt_field("OFC Date", chosen_ofc.get('display_date')),
            _fmt_field("OFC Slots", str(chosen_ofc.get('count'))),
        ]

    if matched_consular_city and chosen_consular:
        parts += [
            "",
            _fmt_field("Matched Consular City", matched_consular_city),
            _fmt_field("Consular Date", chosen_consular.get('display_date')),
            _fmt_field("Consular Slots", str(chosen_consular.get('count'))),
        ]

    return "\n".join(parts)


def send_booking_status(title: str, customer: str, appointment_type: str, city: str = "N/A", date: str = "N/A", time_str: str = "N/A", user_id: str = None, message: str = "") -> bool:
    user_id = user_id or customer
    header_emoji = "🎉" if "SUCCESS" in title.upper() or "BOOKED" in title.upper() else "⚡" if "STARTED" in title.upper() or "PROGRESS" in title.upper() else "⚠️"
    
    city_str = abbreviate_cities(city) if city != "N/A" else "N/A"
    
    lines = [
        f"{header_emoji} <b>{html.escape(str(title))}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>User:</b> {html.escape(str(customer))}",
        f"🆔 <b>Account ID:</b> {html.escape(str(user_id))}",
        f"📌 <b>Type:</b> {html.escape(str(appointment_type))}",
        f"📍 <b>Location:</b> {html.escape(str(city_str))}",
        f"📅 <b>Date:</b> {html.escape(str(date))}",
        f"⏰ <b>Time:</b> {html.escape(str(time_str))}",
    ]
    if message:
        lines.extend(["━━━━━━━━━━━━━━━━━━", f"ℹ️ <i>{html.escape(str(message))}</i>"])
    return send("\n".join(lines))


def send_booking_started(customer: str, action_type: str = "SNIPER", user_id: str = None, city: str = "N/A", date: str = "N/A") -> bool:
    return send_booking_status(
        "SNIPER BOOKING IN PROGRESS",
        customer=customer,
        appointment_type=str(action_type),
        city=city,
        date=date,
        time_str="Auto-Selecting Best Slot",
        user_id=user_id,
        message="Locking slot and submitting confirmation to portal...",
    )


def abbreviate_cities(cities):
    if not cities:
        return "All"
    if isinstance(cities, str):
        cities = [c.strip() for c in cities.split(",") if c.strip()]
    mapping = {
        "CHENNAI": "CN",
        "MUMBAI": "MU",
        "HYDERABAD": "HY",
        "DELHI": "DL",
        "NEW DELHI": "DL",
        "KOLKATA": "KL"
    }
    return ", ".join(mapping.get(c.upper(), c[:2].upper()) for c in cities)


def send_monitoring_status(customer: str, user_id: str = None, mode: str = "SNIPER", ofc_cities: list = None, ofc_range: str = "N/A", consular_cities: list = None, consular_range: str = "N/A", started: bool = True) -> bool:
    ofc_cities = ofc_cities or []
    consular_cities = consular_cities or []
    status = "MONITORING ACTIVE" if started else "MONITORING STOPPED"
    emoji = "🚀" if started else "🛑"
    
    ofc_str = abbreviate_cities(ofc_cities)
    consular_str = abbreviate_cities(consular_cities)
    
    ofc_range_fmt = str(ofc_range).replace("->", "-").replace(" -> ", " - ")
    consular_range_fmt = str(consular_range).replace("->", "-").replace(" -> ", " - ")
    
    lines = [
        f"{emoji} <b>{status}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>User:</b> {html.escape(str(customer))}",
        f"🎯 <b>Role:</b> {html.escape(str(mode))}",
        f"📍 <b>OFC:</b> {html.escape(ofc_str)}",
        f"📅 <b>OFC Dates:</b> {html.escape(ofc_range_fmt)}",
    ]
    if consular_cities:
        lines.append(f"🏛️ <b>Consular:</b> {html.escape(consular_str)}")
        lines.append(f"📅 <b>Consular Dates:</b> {html.escape(consular_range_fmt)}")
    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        f"🟢 <i>{'System is actively monitoring slots 24×7.' if started else 'Monitoring stopped.'}</i>"
    ])
    return send("\n".join(lines))


def send_booking_success(customer: str, appointment_type: str, city: str = "N/A", date: str = "N/A", time_str: str = "N/A", user_id: str = None) -> bool:
    status = "OFC APPOINTMENT CONFIRMED" if str(appointment_type).upper() == "OFC" else "CONSULAR APPOINTMENT CONFIRMED" if str(appointment_type).upper() == "CONSULAR" else "APPOINTMENT CONFIRMED"
    return send_booking_status(status, customer, appointment_type, city, date, time_str, user_id, "✅ Appointment booked and confirmed on portal.")


def send_booking_failure(customer: str, appointment_type: str, city: str = "N/A", date: str = "N/A", time_str: str = "N/A", user_id: str = None, reason: str = "") -> bool:
    msg = reason or "Slot was taken before lock could complete. Returning to standby."
    return send_booking_status("BOOKING MISSED", customer, appointment_type, city, date, time_str, user_id, msg)


def send_partial_booking(customer: str, appointment_type: str, city: str = "N/A", date: str = "N/A", time_str: str = "N/A", user_id: str = None, reason: str = "") -> bool:
    title = "OFC STEP CONFIRMED (PENDING CONSULAR)" if str(appointment_type).upper() == "OFC" else "PARTIAL APPOINTMENT CONFIRMED"
    msg = reason or "OFC slot reserved; searching for matching Consular slot."
    return send_booking_status(title, customer, appointment_type, city, date, time_str, user_id, msg)


def send_ofc_success_triggering_consular(customer: str, user_id: str = None, ofc_city: str = "N/A", ofc_date: str = "N/A") -> bool:
    ofc_city_str = abbreviate_cities(ofc_city) if ofc_city != "N/A" else "N/A"
    lines = [
        "🎉 <b>OFC BOOKED — SEARCHING CONSULAR</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>User:</b> {html.escape(str(customer))}",
        f"🏢 <b>OFC City:</b> {html.escape(ofc_city_str)}",
        f"📅 <b>OFC Date:</b> {html.escape(str(ofc_date))}",
        "━━━━━━━━━━━━━━━━━━",
        "⚡ <b>Status:</b> OFC successfully booked!",
        "🔍 <b>Next Step:</b> Actively hunting Consular slots...",
    ]
    return send("\n".join(lines))


def send_monitoring_summary(total_accounts: int, customer_names: str, poll_min: int, poll_max: int) -> bool:
    summary = [
        "🎛️ <b>VISA BOT FLEET ONLINE</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👥 <b>Active Accounts:</b> {total_accounts}",
        f"📋 <b>Fleet:</b> {html.escape(str(customer_names))}",
        f"⏱ <b>Poll Cycle:</b> ~{poll_min}s - {poll_max}s",
        "━━━━━━━━━━━━━━━━━━",
        "🟢 <i>All sessions healthy and watching portal 24×7.</i>"
    ]
    return send("\n".join(summary))


# Backwards-compatible aliases
send_telegram = send
format_telegram_message = format_slack_message
send_telegram_error = send_slack_error
