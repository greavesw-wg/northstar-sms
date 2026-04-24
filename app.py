import psycopg2
import os
import csv
import re
import html

from flask import Flask, request, jsonify, redirect, url_for, Response
from flask_cors import CORS
from datetime import datetime
from uuid import uuid4
from typing import Any
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def check_auth(username, password):
    return username == os.getenv("DASHBOARD_USER") and password == os.getenv("DASHBOARD_PASS")


def authenticate():
    return Response(
        'Login required', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )


def requires_auth(f):
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)

    decorated.__name__ = f.__name__
    return decorated


def clean_phone(phone):
    return re.sub(r"\D", "", str(phone).strip())


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_requests (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            issue TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

init_db()

def generate_ticket_number(ticket_id, submitted_at):
    if isinstance(submitted_at, str):
        try:
            dt = datetime.fromisoformat(submitted_at.replace("Z", ""))
        except Exception:
            dt = datetime.utcnow()
    else:
        dt = submitted_at

    return f"NS-{dt.strftime('%Y%m%d')}-{int(ticket_id):06d}"


def format_status_badge(status_label):
    status = (status_label or "").lower()

    if status == "new":
        cls = "status-new"
    elif status in ["in progress", "in_progress"]:
        cls = "status-in-progress"
    elif status in ["complete", "completed"]:
        cls = "status-completed"
    else:
        cls = "status-other"

    return f'<span class="status-badge {cls}">{status_label}</span>'

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)
CORS(app)

# --------------------------------------------------
# North Star Client / Property Profiles (MVP)
# Replace with PostgreSQL / Supabase later
# --------------------------------------------------

client_properties: list[dict[str, Any]] = []
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_client_properties():
    global client_properties
    client_properties = []

    if not os.path.exists(CLIENT_PROPERTIES_FILE):
        return

    with open(CLIENT_PROPERTIES_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["unit_count"] = int(row["unit_count"]) if row.get("unit_count") not in (None, "", "None") else None
            row["building_count"] = int(row["building_count"]) if row.get("building_count") not in (None, "",
                                                                                                    "None") else None
            row["service_enabled"] = str(row.get("service_enabled", "")).lower() == "true"
            client_properties.append(row)


def save_client_properties():
    os.makedirs(os.path.dirname(CLIENT_PROPERTIES_FILE), exist_ok=True)

    fieldnames = [
        "id",
        "client_name",
        "property_name",
        "property_type",
        "unit_count",
        "building_count",
        "current_pms",
        "property_notes",
        "sign_up_date",
        "service_begin_date",
        "service_end_date",
        "payment_due_date",
        "service_enabled",
        "onboarding_status",
        "created_at",
        "updated_at",
    ]

    with open(CLIENT_PROPERTIES_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(client_properties)


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def validate_client_property_payload(data: dict[str, Any]):
    required_fields = ["client_name", "property_name"]

    for field in required_fields:
        if not data.get(field):
            return False, f"{field} is required"

    return True, ""


LEADS_FILE = os.path.join(BASE_DIR, "leads.csv")
LOG_FILE = os.path.join(BASE_DIR, "Logs", "work_orders.csv")
FAIL_LOG = os.path.join(BASE_DIR, "logs", "failed_messages.log")
CLIENT_PROPERTIES_FILE = os.path.join(BASE_DIR, "data", "client_properties.csv")
ACTIVITY_LOG = os.path.join(BASE_DIR, "logs", "activity_log.csv")
load_client_properties()


def log_message(from_number, message):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            from_number,
            message
        ])


def log_activity(event_type, client="", property_name="", action="", result=""):
    os.makedirs(os.path.dirname(ACTIVITY_LOG), exist_ok=True)
    file_exists = os.path.exists(ACTIVITY_LOG)

    with open(ACTIVITY_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "timestamp",
                "event_type",
                "client",
                "property",
                "action",
                "result",
            ])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            event_type,
            client,
            property_name,
            action,
            result,
        ])


def ensure_csv_exists():
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            writer.writerow([
                "timestamp",
                "first_name",
                "last_name",
                "email",
                "phone",
                "company_property",
                "unit_count",
                "current_pms",
                "score",
                "category",
                "summary",
                "message"
            ])


def analyze_lead_with_openai(first_name, last_name, company_property, unit_count, current_pms, message):
    prompt = f"""
You are analyzing an inbound lead for North Star AI, an AI-assisted maintenance and operations platform for multifamily real estate.

Return ONLY valid JSON with these keys:
score
category
summary

Rules:
- score must be one of: LOW, MEDIUM, HIGH
- category should be a short business label
- summary should be 1 concise sentence

Lead details:
Name: {first_name} {last_name}
Company / Property: {company_property}
Units: {unit_count}
Current PMS: {current_pms}
Message: {message}
"""

    try:
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
        )

        text = response.output_text.strip()

        import json
        import re

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group(0))

        score = parsed.get("score", "MEDIUM")
        category = parsed.get("category", "General Inquiry")
        summary = parsed.get("summary", "Lead submitted through the North Star contact form.")

        return {
            "score": score,
            "category": category,
            "summary": summary
        }

    except Exception as e:
        print("OPENAI ANALYSIS ERROR:")
        print(str(e))
        return {
            "score": "MEDIUM",
            "category": "General Inquiry",
            "summary": "Lead submitted through the North Star contact form."
        }


@app.route("/sms", methods=["POST"])
def sms_handler():
    from_number = request.form.get("From", "").strip()
    message = request.form.get("Body", "").strip()

    # Log first so no request is lost
    log_message(from_number, message)

    # Temporary reply while we wire in the full AI engine
    resp = MessagingResponse()
    resp.message(
        "NorthStar Maintenance: Your request has been received. "
        "A technician will review shortly."
    )

    return str(resp)


@app.route("/sms-fallback", methods=["POST"])
def sms_fallback():
    from_number = request.form.get("From", "").strip()
    message = request.form.get("Body", "").strip()

    with open(FAIL_LOG, "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"{from_number} | {message}\n"
        )

    return "Logged", 200


def sms_fallback():
    ...
    return "Logged", 200


def format_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)

    if len(digits) == 10:
        digits = "1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        pass
    else:
        raise ValueError(f"Invalid phone number: {phone}")

    return f"+{digits}"


@app.route("/maintenance-request", methods=["POST"])
def maintenance_request():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    phone = clean_phone(data.get("phone", ""))
    building = " ".join(str(data.get("building", "")).split()).strip()
    unit = " ".join(str(data.get("unit", "")).split()).strip()
    issue = str(data.get("issue", "")).strip()

    if not name or not phone or not building or not unit or not issue:
        return jsonify({"error": "Name, phone, building, unit, and issue are required."}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Defer strict matching until client/community templates exist.
        property_id = None
        building_id = None
        unit_id = None
        resident_id = None

        issue_lower = issue.lower()

        outsourced_keywords = [
            "hvac",
            "heat",
            "heating",
            "no heat",
            "air conditioning",
            "ac",
            "a/c",
            "cooling",
            "refrigerator",
            "fridge",
            "appliance",
            "plumbing",
            "leak",
            "leaking",
            "water",
            "pipe",
            "electrical",
            "breaker",
            "pest",
            "lock"
        ]

        assigned_type = "Outsource" if any(
            keyword in issue_lower for keyword in outsourced_keywords
        ) else "In-House"

        print(f"Assigned Type: {assigned_type}")

        if assigned_type == "Outsource":
            print("Trigger Vendor Coordination Loop")
        else:
            print("Handled by In-House Maintenance")

        routing_phone = "6096385183"  # Hunters Glen routing number

        cur.execute("""
            SELECT create_maintenance_request_from_intake(
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::text
            )
        """, (
            routing_phone,
            building,
            unit,
            name,
            phone,
            issue,
            assigned_type
        ))

        request_id = cur.fetchone()[0]
        conn.commit()

        request_id = cur.fetchone()[0]
        conn.commit()

        sms_phone = format_phone(phone)

        try:
            message = twilio_client.messages.create(
                body="North Star AI: Your maintenance request has been received. Updates to your maintenance request will be sent to this number.",
                messaging_service_sid=os.getenv("TWILIO_MESSAGING_SERVICE_SID"),
                to=sms_phone
            )

            print("TWILIO RAW RESPONSE:", message)
            print("TWILIO SID:", getattr(message, "sid", None))
            print("TWILIO STATUS:", getattr(message, "status", None))

            cur.execute("""
                UPDATE maintenance_requests_v2
                SET acknowledgment_sent = %s,
                    acknowledgment_status = %s,
                    acknowledgment_sid = %s,
                    acknowledgment_error = NULL,
                    acknowledgment_sent_at = NOW()
                WHERE id = %s
            """, (
                True,
                str(message.status),  # queued / accepted / sent
                message.sid,
                request_id
            ))
            conn.commit()

            print(
                f"SMS queued. SID={message.sid}, status={message.status}, to={sms_phone}"
            )

        except Exception as sms_error:
            cur.execute("""
                UPDATE maintenance_requests_v2
                SET acknowledgment_sent = %s,
                    acknowledgment_status = %s,
                    acknowledgment_error = %s
                WHERE id = %s
            """, (
                False,
                "failed",
                str(sms_error),
                request_id
            ))
            conn.commit()

            print(f"SMS ERROR sending to {sms_phone}: {sms_error}")

        cur.close()
        conn.close()

        return jsonify({
            "success": True,
            "message": "Maintenance request submitted."
        }), 200

    except Exception as e:
        print("DATABASE ERROR:", repr(e))
        return jsonify({"error": f"Database insert failed: {str(e)}"}), 500


@app.route("/contact", methods=["POST"])
def contact():
    data = request.get_json(silent=True) or {}

    first_name = str(data.get("first_name", "")).strip()
    last_name = str(data.get("last_name", "")).strip()
    email = str(data.get("email", "")).strip()
    phone = clean_phone(data.get("phone", ""))
    company_property = str(data.get("company_property", "")).strip()
    unit_count = str(data.get("unit_count", "")).strip()
    current_pms = str(data.get("current_pms", "")).strip()
    message = str(data.get("message", "")).strip()

    if not first_name or not last_name or not email:
        return jsonify({
            "error": "First name, last name, and email are required."
        }), 400
    log_activity(
        event_type="contact_received",
        client=company_property,
        property_name=company_property,
        action="contact_form",
        result="received"
    )
    ensure_csv_exists()

    lead_analysis = analyze_lead_with_openai(
        first_name=first_name,
        last_name=last_name,
        company_property=company_property,
        unit_count=unit_count,
        current_pms=current_pms,
        message=message
    )

    score = lead_analysis["score"]
    category = lead_analysis["category"]
    summary = lead_analysis["summary"]
    log_activity(
        event_type="lead_analyzed",
        client=company_property,
        property_name=company_property,
        action=f"score={score}; category={category}",
        result=category
    )
    log_activity(
        event_type="ai_response_generated",
        client=company_property,
        property_name=company_property,
        action="summary_created",
        result="category"
    )

    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            first_name,
            last_name,
            email,
            phone,
            company_property,
            unit_count,
            current_pms,
            score,
            category,
            summary,
            message
        ])

    print(f"Lead Score: {score}")
    print(f"Lead Category: {category}")
    print(f"Lead Summary: {summary}")

    print("\n" + "=" * 70)
    print("NEW NORTH STAR INQUIRY")
    print(f"Name: {first_name} {last_name}")
    print(f"Email: {email}")
    print(f"Phone: {phone}")
    print(f"Company / Property: {company_property}")
    print(f"Units: {unit_count}")
    print(f"Current PMS: {current_pms}")
    print(f"Message: {message}")
    print("=" * 70 + "\n")

    sms_status = "not attempted"

    sms_body = f"""
    NEW NORTH STAR LEAD
    Score: {score}
    Category: {category}

    Name: {first_name} {last_name}
    Company: {company_property}
    Units: {unit_count}

    Summary:
    {summary}
    ---
    """

    try:
        print("=== SMS SIMULATION ===")
        print(f"To: {os.getenv('MY_PHONE_NUMBER')}")
        print(f"Message:\n{sms_body}")
        print("======================")

        sms_status = "simulated"

    except Exception as e:
        print("TWILIO ERROR:")
        print(str(e))
        sms_status = f"failed: {str(e)}"

    return jsonify({
        "success": True,
        "message": "Lead captured successfully.",
        "sms_status": sms_status
    })


@app.route("/api/client-properties", methods=["POST"])
def create_client_property():
    data = request.get_json(silent=True) or {}

    is_valid, error_message = validate_client_property_payload(data)
    if not is_valid:
        return jsonify({"success": False, "error": error_message}), 400

    record = {
        "id": str(uuid4()),
        "client_name": str(data.get("client_name", "")).strip(),
        "property_name": str(data.get("property_name", "")).strip(),
        "property_type": str(data.get("property_type", "")).strip(),
        "unit_count": int(data["unit_count"]) if data.get("unit_count") not in (None, "") else None,
        "building_count": int(data["building_count"]) if data.get("building_count") not in (None, "") else None,
        "current_pms": str(data.get("current_pms", "")).strip(),
        "property_notes": str(data.get("property_notes", "")).strip(),
        "sign_up_date": str(data.get("sign_up_date", "")).strip(),
        "service_begin_date": str(data.get("service_begin_date", "")).strip(),
        "service_end_date": str(data.get("service_end_date", "")).strip(),
        "payment_due_date": str(data.get("payment_due_date", "")).strip(),
        "service_enabled": bool(data.get("service_enabled", True)),
        "onboarding_status": str(data.get("onboarding_status", "in_progress")).strip() or "in_progress",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    client_properties.append(record)
    save_client_properties()
    log_activity(
        event_type="client_created",
        client=record.get("client_name", ""),
        property_name=record.get("property_name", ""),
        action="create",
        result="success"
    )
    return jsonify({
        "success": True,
        "message": "Client property created successfully.",
        "id": record["id"],
        "record": record
    }), 201


@app.route("/api/client-properties", methods=["GET"])
def list_client_properties():
    return jsonify({
        "count": len(client_properties),
        "clients": client_properties
    })


@app.route("/api/client-properties/<record_id>", methods=["PATCH"])
def update_client_property(record_id):
    data = request.get_json(silent=True) or {}

    record = next((r for r in client_properties if r["id"] == record_id), None)
    if not record:
        return jsonify({
            "success": False,
            "error": "Record not found."
        }), 404

    allowed_fields = {
        "client_name",
        "property_name",
        "property_type",
        "unit_count",
        "building_count",
        "current_pms",
        "property_notes",
        "sign_up_date",
        "service_begin_date",
        "service_end_date",
        "payment_due_date",
        "service_enabled",
        "onboarding_status"
    }

    for key, value in data.items():
        if key not in allowed_fields:
            continue

        if key in {"unit_count", "building_count"}:
            if value in (None, ""):
                record[key] = None
            else:
                try:
                    record[key] = int(value)
                except (TypeError, ValueError):
                    return jsonify({
                        "success": False,
                        "error": f"{key} must be an integer."
                    }), 400

        elif key == "service_enabled":
            if value not in (True, False):
                return jsonify({
                    "success": False,
                    "error": "service_enabled must be true or false."
                }), 400
            record[key] = value

        else:
            record[key] = str(value).strip()

    record["updated_at"] = now_iso()
    save_client_properties()

    return jsonify({
        "success": True,
        "message": "Client property updated successfully.",
        "record": record
    }), 200

@app.route("/dashboard")
@requires_auth
def dashboard():
    conn = get_db_connection()
    cur = conn.cursor()

    # Total requests
    cur.execute("SELECT COUNT(*) FROM maintenance_requests_v2")
    total_requests = cur.fetchone()[0]

    cur.execute("""
       SELECT
            mr.id,
            mr.resident_name,
            mr.resident_phone,
            COALESCE(p.property_name, 'Unassigned Community') AS property_name,
            mr.building_label,
            mr.unit_label,
            mr.issue_description,
            mr.status,
            mr.assigned_type,
            mr.submitted_at
        FROM maintenance_requests_v2 mr
        LEFT JOIN properties p ON mr.property_id = p.id
        WHERE COALESCE(mr.dashboard_status, 'visible') = 'visible'
        ORDER BY mr.submitted_at DESC
        LIMIT 100               
    """)

    recent_requests = cur.fetchall()

    cur.close()
    conn.close()

    total_clients = total_requests
    enabled_clients = 0
    disabled_clients = 0
    total_units = 0

    activity_rows = ""

    for r in recent_requests:
        ticket_id = r[0]
        resident_name = r[1]
        resident_phone = (r[2] or "").strip()
        property_name = r[3]
        building = (r[4] or "").strip()
        unit = (r[5] or "").strip()
        issue = r[6]
        status = r[7]
        assigned_type = (r[8] or "").strip()
        submitted_at = r[9]

        ticket_number = generate_ticket_number(ticket_id, submitted_at)

        if building and unit:
            property_display = f"{property_name} • Building {building} • Unit {unit}"
        elif building:
            property_display = f"{property_name} • Building {building}"
        elif unit:
            property_display = f"{property_name} • Unit {unit}"
        else:
            property_display = (property_name or "Unassigned Community").strip()

        status_label = {
            "new": "New",
            "in_progress": "In Progress",
            "complete": "Complete"
        }.get(status, "Unknown")

        id_safe = html.escape(str(ticket_id), quote=True)
        ticket_number_safe = html.escape(str(ticket_number), quote=True)
        clean_name = re.sub(r'[^a-zA-Z0-9]', '', resident_name)
        jitsi_room = f"NorthStar-{ticket_number}-{clean_name}"
        jitsi_room_safe = html.escape(str(jitsi_room), quote=True)
        video_cell = f'<a href="https://meet.jit.si/{jitsi_room_safe}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()" class="video-link">📹 Call</a>'
        # Ensure datetime object
        dt = datetime.fromisoformat(submitted_at) if isinstance(submitted_at, str) else submitted_at

        # Format: April 20, 2026, 6:15 PM
        formatted_time = dt.strftime("%B %d, %Y, %I:%M %p").replace(" 0", " ")

        submitted_at_safe = html.escape(formatted_time, quote=True)
        resident_name_safe = html.escape(str(resident_name), quote=True)
        resident_phone_safe = html.escape(str(resident_phone), quote=True) if resident_phone else ""
        phone_cell = f'<a href="tel:{resident_phone_safe}" onclick="event.stopPropagation()" class="phone-link">{resident_phone_safe}</a>' if resident_phone_safe else "—"
        property_display_safe = html.escape(str(property_display), quote=True)
        issue_safe = html.escape(str(issue), quote=True)
        status_label_safe = html.escape(str(status_label), quote=True)

        assigned_type_safe = html.escape(str(assigned_type), quote=True)

        activity_rows += f"""
        <tr 
            data-ticket-id="{id_safe}"
            data-ticket-number="{ticket_number_safe}"
            data-submitted-at="{submitted_at_safe}"
            data-event="Maintenance Request"
            data-resident-name="{resident_name_safe}"
            data-property-display="{property_display_safe}"
            data-issue="{issue_safe}"
            data-status="{status_label_safe}"
            onclick="openTicketModal(this)"
        >                       
            <td>{ticket_number_safe}</td>
            <td><span style="color:#94a3b8;">{submitted_at_safe}</span></td>
            <td>Maintenance Request</td>
            <td>{resident_name_safe}</td>
            <td>{phone_cell}</td>
            <td>{video_cell}</td>
            <td class="property-cell">{property_display_safe}</td>
            <td class="issue-cell">{issue_safe}</td>
            <td>{assigned_type_safe}</td>
            <td class="status-cell">{format_status_badge(status_label)}</td>
            <td>
                <button class="delete-btn" onclick="deleteTicket(event, '{id_safe}', '{ticket_number_safe}')">
                    Delete
                </button>
            </td>
        </tr>
        """

    if not activity_rows:
        activity_rows = """
            <tr>
                <td colspan="8">No recent activity yet.</td>
            </tr>
        """

    page_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>North Star Command</title>
        <style>
        html, body {{
            height: 100%;
            margin: 0;
        }}
        body {{
            font-family: Arial, sans-serif;
            background: #0b1220;
            color: #e5e7eb;
            overflow: hidden;
        }}
        .wrap {{
            height: 100vh;
            padding: 24px;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .title {{
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 6px;
        }}
        .subtitle {{
            color: #94a3b8;
            margin-bottom: 18px;
            font-size: 14px;
        }}
        .cards {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }}
        .card {{
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 18px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.25);
        }}
        .card-label {{
            font-size: 11px;
            color: #94a3b8;
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .card-value {{
            font-size: 18px;
            font-weight: 700;
        }}
        .panel {{
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 12px 16px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.25);
        }}
        .panel.activity-panel {{
            flex: 1 1 auto;
            min-height: 0;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}
        .table-container {{
            flex: 1 1 auto;
            min-height: 0;
            overflow-y: auto;
            overflow-x: auto;
            border: 1px solid #1f2937;
            border-radius: 8px;
        }}
        .ops-table {{
            min-width: 1200px;
            width: max-content;
            border-collapse: collapse;
        }}
        .ops-table th,
        .ops-table td {{
            padding: 8px 8px;
            border-bottom: 1px solid #1f2937;
            text-align: left;
            font-size: 12px;
            vertical-align: top;
            line-height: 1.2;
            white-space: nowrap;
        }}
        .ops-table th {{
            position: sticky;
            top: 0;
            background: #111827;
            z-index: 2;
            color: #93c5fd;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.05em;
        }}
        .ops-table td.issue-cell {{
            white-space: normal;
            overflow-wrap: anywhere;
            word-break: break-word;
            max-width: 420px;
        }}
        .ops-table td.property-cell {{
            white-space: normal;
            min-width: 170px;
        }}
        .ops-table td.status-cell {{
            min-width: 100px;
        }}
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
        }}
        .enabled {{
            background: #052e16;
            color: #86efac;
            border: 1px solid #166534;
        }}
        .disabled {{
            background: #450a0a;
            color: #fca5a5;
            border: 1px solid #991b1b;
        }}
        .progress {{
            background: #3f2f0b;
            color: #fcd34d;
            border: 1px solid #a16207;
        }}
        button {{
            background: #1d4ed8;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 12px;
            cursor: pointer;
            font-weight: 600;
        }}
        button.off {{
            background: #b91c1c;
        }}
        button:hover {{
            opacity: 0.92;
        }}
        .status-row {{
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
        }}
        .status-item strong {{
            display: block;
            margin-bottom: 6px;
        }}
        .phone-link {{
        color: #38bdf8;
        font-weight: 600;
        text-decoration: none;
        }}

        .phone-link:hover {{
        color: #0ea5e9;
        text-decoration: underline;
        }}
        .video-link {{
        color: #22c55e;
        font-weight: 600;
        text-decoration: none;
        }}

        .video-link:hover {{
        color: #16a34a;
        text-decoration: underline;
        }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="title">North Star Command</div>
            <div class="subtitle">Operational control center for client/property service management</div>

            <div class="panel">
                <h3 style="margin-top:0;">System Status</h3>
                <div class="status-row">
                    <div class="status-item">
                        <strong>AI Engine</strong>
                        <span class="badge enabled">Online</span>
                    </div>
                    <div class="status-item">
                        <strong>Lead Processor</strong>
                        <span class="badge enabled">Running</span>
                    </div>
                    <div class="status-item">
                        <strong>Activity Logger</strong>
                        <span class="badge enabled">Active</span>
                    </div>
                    <div class="status-item">
                        <strong>Data Store</strong>
                        <span class="badge enabled">Healthy</span>
                    </div>
                </div>
            </div>

            <div class="panel activity-panel">
                <h3 style="margin-top:0;">Recent Activity</h3>
                <div class="table-container">
                    <table class="ops-table">
                        <thead>
                            <tr>
                                <th>TICKET #</th>
                                <th>TIME</th>
                                <th>EVENT</th>
                                <th>CLIENT</th>
                                <th>PHONE</th>
                                <th>VIDEO</th>
                                <th>PROPERTY</th>
                                <th>ISSUE</th>
                                <th>ASSIGNED</th>
                                <th>STATUS</th>
                                <th>ACTION</th>                              
                            </tr>
                        </thead>
                        <tbody>
    """
    page_html += activity_rows
    page_html += """
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    <script>
            async function deleteTicket(event, ticketId, ticketNumber) {
    event.stopPropagation();

    const confirmed = confirm(`Remove ticket ${ticketNumber} from the dashboard?`);
    if (!confirmed) return;

    try {
        const response = await fetch(`/delete-ticket/${ticketId}`, {
            method: "POST",
            credentials: "same-origin"
        });

        if (!response.ok) {
            const text = await response.text();
            console.error("Delete failed:", response.status, text);
            alert(`Delete failed: ${response.status}`);
            return;
        }

        window.location.reload();
    } catch (error) {
        alert("Unable to remove ticket.");
        console.error(error);
    }
}
        </script>           
    </body>
    </html>
    """
    return page_html

@app.route("/delete-ticket/<int:ticket_id>", methods=["POST"])
@requires_auth
def delete_ticket(ticket_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE maintenance_requests_v2
        SET dashboard_status = 'hidden'
        WHERE id = %s
    """, (ticket_id,))

    conn.commit()
    cur.close()
    conn.close()

    return ("", 204)

@app.route("/toggle-service/<record_id>", methods=["POST"])
def toggle_service(record_id):
    for record in client_properties:
        if record.get("id") == record_id:
            current_state = bool(record.get("service_enabled", False))
            record["service_enabled"] = not current_state
            record["updated_at"] = now_iso()
            save_client_properties()
            log_activity(
                event_type="service_toggled",
                client=record.get("client_name", ""),
                property_name=record.get("property_name", ""),
                action="enabled" if record.get("service_enabled") else "disabled",
                result="success",
            )
            return redirect(url_for("dashboard"))

    return jsonify({
        "success": False,
        "error": "Record not found."
    }), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)


