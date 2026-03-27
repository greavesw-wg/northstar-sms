import psycopg2
import os
import csv
import re

from flask import Flask, request, jsonify, redirect, url_for
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
            row["building_count"] = int(row["building_count"]) if row.get("building_count") not in (None, "", "None") else None
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

@app.route("/maintenance-request", methods=["POST"])
def maintenance_request():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    phone = clean_phone(data.get("phone", ""))
    issue = str(data.get("issue", "")).strip()

    if not name or not phone or not issue:
        return jsonify({"error": "Name, phone, and issue are required."}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO maintenance_requests (name, phone, issue)
            VALUES (%s, %s, %s)
        """, (name, phone, issue))

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "success": True,
            "message": "Maintenance request submitted."
        }), 200

    except Exception as e:
        print("DATABASE ERROR:", e)

        return jsonify({
            "success": False,
            "error": "Database insert failed"
        }), 500

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
def dashboard():
    total_clients = len(client_properties)
    enabled_clients = sum(1 for c in client_properties if c.get("service_enabled"))
    disabled_clients = total_clients - enabled_clients
    total_units = sum(int(c.get("unit_count") or 0) for c in client_properties)

    recent_activity = []
    if os.path.exists(ACTIVITY_LOG):
        with open(ACTIVITY_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()[1:]  # skip header
            recent_activity = lines[-5:]  # last 5 events
            recent_activity.reverse()  # newest first

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>North Star Command</title>
        <style>
            body {{
                margin: 0;
                font-family: Arial, sans-serif;
                background: #0b1220;
                color: #e5e7eb;
                overflow: hidden;
}}
            .wrap {{
                padding: 24px;
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
                padding: 12px 16px;   /* tighter */
                box-shadow: 0 4px 14px rgba(0,0,0,0.25);
                margin-bottom: 16px;  /* tighter spacing between panels */
            }}
      
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 8px;
            }}
            th, td {{
                padding: 12px 10px;
                border-bottom: 1px solid #1f2937;
                text-align: left;
                font-size: 14px;
                vertical-align: top;
            }}
            th {{
                color: #93c5fd;
                text-transform: uppercase;
                font-size: 12px;
                letter-spacing: 0.05em;
            }}
            
            .ops-table {{
                width: 100%;
                border-collapse: collapse;
                table-layout: fixed;
            }}
            
            .ops-table thead {{
                display: table;
                width: 100%;
                table-layout: fixed;
            }}
            
            .ops-table thead th {{
                position: sticky;
                top: 0;
                background: #111827;
                z-index: 2;
            }}
            
            .ops-table tbody {{
                display: block;
                max-height: 120px;
                overflow-y: auto;
                width: 100%;
            }}
            
            .ops-table tbody tr {{
                display: table;
                width: 100%;
                table-layout: fixed;
            }}
            
            .ops-table th,
            .ops-table td {{
                padding: 8px 8px;
                border-bottom: 1px solid #1f2937;
                text-align: left;
                font-size: 12px;
                vertical-align: top;
                line-height: 1.2;
            }}
            
            .ops-table th {{
                font-size: 11px;
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

            <div class="panel">
                <h3 style="margin-top:0;">Recent Activity</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Event</th>
                            <th>Client</th>
                            <th>Property</th>
                            <th>Action</th>
                            <th>Result</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for row in recent_activity:
        cols = row.strip().split(",", 5)
        if len(cols) < 6:
            continue

        html += f"""
                        <tr>
                            <td>{cols[0]}</td>
                            <td>{cols[1]}</td>
                            <td>{cols[2]}</td>
                            <td>{cols[3]}</td>
                            <td>{cols[4]}</td>
                            <td>{cols[5]}</td>
                        </tr>
        """

    if not recent_activity:
        html += """
                        <tr>
                            <td colspan="6">No recent activity yet.</td>
                        </tr>
        """

    html += f"""
                    </tbody>
                </table>
            </div>

            <div class="cards">
                <div class="card">
                    <div class="card-label">Total Clients</div>
                    <div class="card-value">{total_clients}</div>
                </div>
                <div class="card">
                    <div class="card-label">Active Services</div>
                    <div class="card-value">{enabled_clients}</div>
                </div>
                <div class="card">
                    <div class="card-label">Disabled Services</div>
                    <div class="card-value">{disabled_clients}</div>
                </div>
                <div class="card">
                    <div class="card-label">Total Units</div>
                    <div class="card-value">{total_units}</div>
                </div>
            </div>

            <div class="panel">
                <h2 style="margin-top: 0;">Client Operations</h2>
                <table class="ops-table">
                    <thead>                        
                            <th>Client</th>
                            <th>Property</th>
                            <th>Type</th>
                            <th>Units</th>
                            <th>Buildings</th>
                            <th>PMS</th>
                            <th>Onboarding</th>
                            <th>Service State</th>
                            <th>Control</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for c in client_properties:
        service_enabled = bool(c.get("service_enabled"))
        service_badge = (
            '<span class="badge enabled">ENABLED</span>'
            if service_enabled else
            '<span class="badge disabled">DISABLED</span>'
        )

        onboarding_status = str(c.get("onboarding_status", "") or "").strip().lower()
        if onboarding_status == "in_progress":
            onboarding_badge = '<span class="badge progress">IN PROGRESS</span>'
        elif onboarding_status in ("complete", "completed"):
            onboarding_badge = '<span class="badge enabled">COMPLETE</span>'
        else:
            onboarding_badge = f'<span class="badge progress">{c.get("onboarding_status", "")}</span>'

        btn_label = "Disable" if service_enabled else "Enable"
        btn_class = "off" if service_enabled else ""

        html += f"""
                        <tr>
                            <td>{c.get("client_name", "")}</td>
                            <td>{c.get("property_name", "")}</td>
                            <td>{c.get("property_type", "")}</td>
                            <td>{c.get("unit_count", "")}</td>
                            <td>{c.get("building_count", "")}</td>
                            <td>{c.get("current_pms", "")}</td>
                            <td>{onboarding_badge}</td>
                            <td>{service_badge}</td>
                            <td>
                                <form method="post" action="/toggle-service/{c.get("id", "")}">
                                    <button type="submit" class="{btn_class}">{btn_label}</button>
                                </form>
                            </td>
                        </tr>
        """

    if not client_properties:
        html += """
                        <tr>
                            <td colspan="9">No client properties loaded.</td>
                        </tr>
        """

    html += """
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """

    return html

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
    app.run(host="127.0.0.1", port=5000, debug=True)