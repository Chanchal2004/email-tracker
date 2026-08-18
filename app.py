


import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
import secrets
import base64
import json
import csv
import io


from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


# ============================================================
# CONFIG
# ============================================================

APP = Flask(__name__)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))

DATABASE_URL = os.environ.get("DATABASE_URL")

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

SENDER_EMAIL = "chanchalchaudhary0101@gmail.com"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send"
]


# ============================================================
# PUBLIC CLOUDFLARE URL
# ============================================================

PUBLIC_URL = os.environ.get(
    "PUBLIC_URL",
    "https://email-tracker-7tr6.onrender.com"
).rstrip("/")


# ============================================================
# TIME
# ============================================================

def utc_now():

    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    )


def display_time(value):

    if not value:
        return "-"

    try:

        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        ist = dt.astimezone(
            timezone(
                timedelta(
                    hours=5,
                    minutes=30
                )
            )
        )

        return dt_to_display(
            ist
        )

    except Exception:

        return value


def dt_to_display(dt):

    return dt.strftime(
        "%d %b %Y, %I:%M:%S %p"
    )


# ============================================================
# DATABASE
# ============================================================

class DatabaseConnection:
    """Small compatibility wrapper for the existing database code.

    psycopg2 connections do not have .execute(). The original app uses
    conn.execute(...), so this wrapper forwards execute() to one
    RealDictCursor while preserving commit(), cursor(), and close().
    """

    def __init__(self, connection):
        self._connection = connection
        self._cursor = connection.cursor()

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        return self._cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        try:
            self._cursor.close()
        finally:
            self._connection.close()


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Add your Render PostgreSQL "
            "connection string to the Render Environment Variables."
        )

    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )

    return DatabaseConnection(conn)


def init_db():

    conn = get_db()

    cur = conn.cursor()

    # --------------------------------------------------------
    # EMAILS
    #
    # first_opened_at and last_opened_at are intentionally
    # kept for compatibility with an existing tracker.db.
    #
    # They are NOT displayed or used by the dashboard.
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS emails (

            id BIGSERIAL PRIMARY KEY,

            tracking_id TEXT UNIQUE NOT NULL,

            recipient TEXT NOT NULL,

            subject TEXT,

            sent_at TEXT,

            gmail_message_id TEXT,

            first_opened_at TEXT,

            last_opened_at TEXT,

            open_count INTEGER NOT NULL DEFAULT 0,

            first_clicked_at TEXT,

            last_clicked_at TEXT,

            click_count INTEGER NOT NULL DEFAULT 0,

            first_page_visit_at TEXT,

            last_page_visit_at TEXT,

            page_visit_count INTEGER NOT NULL DEFAULT 0
        )
    """)

    # --------------------------------------------------------
    # ACTIVITY
    # --------------------------------------------------------


    # --------------------------------------------------------
    # SAFE MIGRATIONS: existing PostgreSQL data is preserved.
    # campaign_id = parent/campaign ID
    # tracking_id = child/email ID
    # --------------------------------------------------------
    cur.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS campaign_id TEXT")
    cur.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_name TEXT")
    cur.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_email TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_emails_campaign ON emails(campaign_id)")
    cur.execute("""
        UPDATE emails
        SET campaign_id = COALESCE(campaign_id, 'legacy')
        WHERE campaign_id IS NULL
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity (

            id BIGSERIAL PRIMARY KEY,

            tracking_id TEXT NOT NULL,

            event TEXT NOT NULL,

            timestamp TEXT NOT NULL,

            ip TEXT,

            user_agent TEXT,

            referer TEXT,

            FOREIGN KEY(tracking_id)
                REFERENCES emails(tracking_id)
                ON DELETE CASCADE
        )
    """)

    # --------------------------------------------------------
    # FORM SUBMISSIONS
    #
    # Only information explicitly entered and submitted
    # through the disclosed form is stored.
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS form_submissions (

            id BIGSERIAL PRIMARY KEY,

            tracking_id TEXT NOT NULL,

            name TEXT NOT NULL,

            phone TEXT NOT NULL,

            email TEXT NOT NULL,

            submitted_at TEXT NOT NULL,

            ip TEXT,

            user_agent TEXT,

            referer TEXT,

            FOREIGN KEY(tracking_id)
                REFERENCES emails(tracking_id)
                ON DELETE CASCADE
        )
    """)

    # --------------------------------------------------------
    # INDEXES
    # --------------------------------------------------------

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_activity_tracking
        ON activity(tracking_id)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_activity_time
        ON activity(timestamp)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_form_tracking
        ON form_submissions(tracking_id)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_form_time
        ON form_submissions(submitted_at)
    """)

    conn.commit()

    conn.close()

    print("Database ready: PostgreSQL")


# ============================================================
# GMAIL AUTH
# ============================================================

def gmail_service():

    creds = None

    # ========================================================
    # 1. LOAD AUTHORIZED TOKEN FROM RENDER ENVIRONMENT
    # ========================================================
    #
    # Render Environment Variable:
    #
    # GMAIL_TOKEN_JSON
    #
    # Value = COMPLETE contents of token.json
    #
    # Example:
    # {
    #   "token": "...",
    #   "refresh_token": "...",
    #   "token_uri": "https://oauth2.googleapis.com/token",
    #   "client_id": "...",
    #   "client_secret": "...",
    #   "scopes": [
    #       "https://www.googleapis.com/auth/gmail.send"
    #   ]
    # }
    #
    # IMPORTANT:
    # We do NOT call run_local_server() on Render.
    # That would require a browser and causes the deployment problem.
    # ========================================================

    token_json = os.environ.get("GMAIL_TOKEN_JSON")

    if token_json:

        try:

            token_data = json.loads(token_json)

            creds = Credentials.from_authorized_user_info(
                token_data,
                SCOPES
            )

            print(
                "Gmail token loaded from GMAIL_TOKEN_JSON."
            )

        except Exception as e:

            print(
                "Could not load GMAIL_TOKEN_JSON:",
                e
            )

            creds = None

    # ========================================================
    # 2. LOCAL DEVELOPMENT FALLBACK
    # ========================================================
    #
    # This lets the same code work locally if token.json exists.
    #
    # Render does not need this when GMAIL_TOKEN_JSON is configured.
    # ========================================================

    if creds is None and os.path.exists(TOKEN_FILE):

        try:

            creds = Credentials.from_authorized_user_file(
                TOKEN_FILE,
                SCOPES
            )

            print(
                "Gmail token loaded from token.json."
            )

        except Exception as e:

            print(
                "Could not load token.json:",
                e
            )

            creds = None

    # ========================================================
    # 3. REFRESH EXPIRED TOKEN
    # ========================================================

    if (
        creds
        and creds.expired
        and creds.refresh_token
    ):

        try:

            creds.refresh(
                Request()
            )

            print(
                "Gmail token refreshed successfully."
            )

        except Exception as e:

            print(
                "Token refresh failed:",
                e
            )

            creds = None

    # ========================================================
    # 4. NEVER START INTERACTIVE OAUTH ON RENDER
    # ========================================================
    #
    # If this happens, either:
    #
    # - GMAIL_TOKEN_JSON is missing
    # - token.json is invalid
    # - token.json does not contain a refresh token
    # - the Google OAuth token has been revoked
    #
    # Generate/authorize token.json locally, then put its COMPLETE
    # contents into the Render variable GMAIL_TOKEN_JSON.
    # ========================================================

    if not creds or not creds.valid:

        raise RuntimeError(
            "Gmail authentication failed. "
            "Set GMAIL_TOKEN_JSON in Render to the complete "
            "contents of your authorized token.json file. "
            "Do not use run_local_server() on Render."
        )

    # ========================================================
    # 5. BUILD GMAIL API SERVICE
    # ========================================================

    return build(
        "gmail",
        "v1",
        credentials=creds
    )



# ============================================================
# EMAIL VALIDATION
# ============================================================

EMAIL_REGEX = re.compile(
    r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
)


def valid_email(value):

    return bool(
        EMAIL_REGEX.match(
            str(value).strip()
        )
    )


# ============================================================
# PHONE VALIDATION
# ============================================================

PHONE_REGEX = re.compile(
    r"^[0-9+\-\s()]{7,20}$"
)


def valid_phone(value):

    value = str(value).strip()

    if not PHONE_REGEX.match(value):

        return False

    digits = re.sub(
        r"\D",
        "",
        value
    )

    return 7 <= len(digits) <= 15


# ============================================================
# REQUEST INFORMATION
# ============================================================

def request_ip():

    cf_ip = request.headers.get(
        "CF-Connecting-IP"
    )

    if cf_ip:

        return cf_ip

    forwarded = request.headers.get(
        "X-Forwarded-For"
    )

    if forwarded:

        return (
            forwarded
            .split(",")[0]
            .strip()
        )

    return request.remote_addr or ""


def request_user_agent():

    return request.headers.get(
        "User-Agent",
        ""
    )


def request_referer():

    return request.headers.get(
        "Referer",
        ""
    )


# ============================================================
# TRACKING ID
# ============================================================

def tracking_exists(tracking_id):

    conn = get_db()

    row = conn.execute("""
        SELECT tracking_id
        FROM emails
        WHERE tracking_id = %s
    """, (
        tracking_id,
    )).fetchone()

    conn.close()

    return row is not None


# ============================================================
# ACTIVITY LOGGER
# ============================================================

def log_activity(
    tracking_id,
    event
):

    now = utc_now()

    conn = get_db()

    row = conn.execute("""
        SELECT tracking_id
        FROM emails
        WHERE tracking_id = %s
    """, (
        tracking_id,
    )).fetchone()

    if not row:

        conn.close()

        return False

    # --------------------------------------------------------
    # Save activity event
    # --------------------------------------------------------

    conn.execute("""
        INSERT INTO activity (

            tracking_id,
            event,
            timestamp,
            ip,
            user_agent,
            referer

        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        tracking_id,
        event,
        now,
        request_ip(),
        request_user_agent(),
        request_referer()
    ))

    # --------------------------------------------------------
    # OPEN
    #
    # Count every observed tracking-pixel request and keep
    # first/last observed open timestamps.
    # --------------------------------------------------------

    if event == "open":

        conn.execute("""
            UPDATE emails

            SET

                first_opened_at =
                    COALESCE(
                        first_opened_at,
                        %s
                    ),

                last_opened_at = %s,

                open_count =
                    open_count + 1

            WHERE tracking_id = %s
        """, (
            now,
            now,
            tracking_id
        ))

    # --------------------------------------------------------
    # CLICK
    # --------------------------------------------------------

    elif event == "click":

        conn.execute("""
            UPDATE emails

            SET

                first_clicked_at =
                    COALESCE(
                        first_clicked_at,
                        %s
                    ),

                last_clicked_at = %s,

                click_count =
                    click_count + 1

            WHERE tracking_id = %s
        """, (
            now,
            now,
            tracking_id
        ))

    # --------------------------------------------------------
    # PAGE VISIT
    # --------------------------------------------------------

    elif event == "page_visit":

        conn.execute("""
            UPDATE emails

            SET

                first_page_visit_at =
                    COALESCE(
                        first_page_visit_at,
                        %s
                    ),

                last_page_visit_at = %s,

                page_visit_count =
                    page_visit_count + 1

            WHERE tracking_id = %s
        """, (
            now,
            now,
            tracking_id
        ))

    conn.commit()

    conn.close()

    return True


# ============================================================
# CREATE EMAIL HTML
# ============================================================

def create_email_html(
    tracking_id,
    message
):

    tracking_url = (
        PUBLIC_URL
        + "/go/"
        + tracking_id
    )

    pixel_url = (
        PUBLIC_URL
        + "/track/"
        + tracking_id
        + ".gif"
    )

    # Preserve line breaks and make plain http(s) URLs clickable.
    # Each detected URL is routed through this email's tracking ID.
    url_re = re.compile(r"(https?://[^\s<]+)")
    parts = []
    last = 0

    for match in url_re.finditer(message):
        parts.append(escape(message[last:match.start()]))
        raw_url = match.group(1).rstrip(".,);]}")
        safe_url_text = escape(raw_url)
        parts.append(
            '<a href="' + escape(
                PUBLIC_URL + "/go/" + tracking_id
            ) + '" style="color:#1261a0 !important;'
            'text-decoration:underline !important;"'
            ' target="_blank">' + safe_url_text + '</a>'
        )
        last = match.end()

    parts.append(escape(message[last:]))
    safe_message = "".join(parts).replace("\n", "<br>")

    safe_tracking_url = escape(tracking_url)
    safe_pixel_url = escape(pixel_url)

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;line-height:1.5;">
<div>{safe_message}</div>

<p>
<a href="{safe_tracking_url}"
   target="_blank"
   style="color:#1261a0 !important;text-decoration:underline !important;font-weight:600;">
   Open link
</a>
</p>

<img src="{safe_pixel_url}" width="1" height="1"
     style="display:block;width:1px;height:1px;border:0;opacity:0;"
     alt="">
</body>
</html>
"""


# ============================================================
# SEND ONE EMAIL
# ============================================================

def send_one_email(
    service,
    recipient,
    subject,
    message,
    campaign_id=None,
    recipient_name=""
):

    tracking_id = secrets.token_urlsafe(
        32
    )

    sent_at = utc_now()

    html = create_email_html(
        tracking_id,
        message
    )

    msg = EmailMessage()

    msg["To"] = recipient

    msg["From"] = SENDER_EMAIL

    msg["Subject"] = subject

    msg.set_content(
        message
    )

    msg.add_alternative(
        html,
        subtype="html"
    )

    raw = base64.urlsafe_b64encode(
        msg.as_bytes()
    ).decode()

    result = (
        service
        .users()
        .messages()
        .send(
            userId="me",
            body={
                "raw": raw
            }
        )
        .execute()
    )

    gmail_message_id = result.get(
        "id"
    )

    conn = get_db()

    conn.execute("""
        INSERT INTO emails (
            tracking_id,
            campaign_id,
            recipient,
            recipient_name,
            recipient_email,
            subject,
            sent_at,
            gmail_message_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        tracking_id,
        campaign_id or "legacy",
        recipient,
        recipient_name or "",
        recipient,
        subject,
        sent_at,
        gmail_message_id
    ))

    conn.commit()

    conn.close()

    return {
        "success": True,
        "tracking_id": tracking_id,
        "recipient": recipient,
        "message_id": gmail_message_id,
        "campaign_id": campaign_id or "legacy",
        "tracking_url":
            PUBLIC_URL
            + "/go/"
            + tracking_id
    }


# ============================================================
# DASHBOARD
# ============================================================

@APP.get("/")
def dashboard():

    conn = get_db()

    emails = conn.execute("""
        SELECT *
        FROM emails
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template_string(
        DASHBOARD_HTML,
        emails=emails,
        public_url=PUBLIC_URL,
        sender=SENDER_EMAIL,
        format_time=display_time
    )


# ============================================================
# SEND EMAIL
# ============================================================

@APP.post("/send")
def send_from_dashboard():

    recipients_text = request.form.get(
        "recipients",
        ""
    )

    subject = request.form.get(
        "subject",
        ""
    ).strip()

    message = request.form.get(
        "message",
        ""
    ).strip()

    raw_recipients = re.split(
        r"[\n,;]+",
        recipients_text
    )

    recipients = []

    invalid = []

    for item in raw_recipients:

        email = item.strip()

        if not email:

            continue

        if valid_email(email):

            if email.lower() not in [
                x.lower()
                for x in recipients
            ]:

                recipients.append(
                    email
                )

        else:

            invalid.append(
                email
            )

    if not recipients:

        return render_template_string(
            MESSAGE_HTML,
            title="Error",
            message=(
                "At least one valid recipient "
                "email required."
            ),
            back=True
        ), 400

    if not subject:

        return render_template_string(
            MESSAGE_HTML,
            title="Error",
            message="Subject required.",
            back=True
        ), 400

    if not message:

        return render_template_string(
            MESSAGE_HTML,
            title="Error",
            message="Message required.",
            back=True
        ), 400

    try:

        service = gmail_service()

    except Exception as e:

        return render_template_string(
            MESSAGE_HTML,
            title="Gmail Login Error",
            message=str(e),
            back=True
        ), 500

    results = []

    for recipient in recipients:

        try:

            result = send_one_email(
                service,
                recipient,
                subject,
                message
            )

            results.append(
                result
            )

        except Exception as e:

            results.append({
                "success": False,
                "recipient": recipient,
                "error": str(e)
            })

    return render_template_string(
        SEND_RESULT_HTML,
        results=results,
        invalid=invalid
    )


# ============================================================
# 1x1 GIF
# ============================================================

GIF_1X1 = (
    b"GIF89a"
    b"\x01\x00\x01\x00"
    b"\x80\x00\x00"
    b"\x00\x00\x00"
    b"\xff\xff\xff"
    b"!\xf9\x04\x01"
    b"\x00\x00\x00\x00"
    b",\x00\x00\x00\x00"
    b"\x01\x00\x01\x00"
    b"\x00\x02\x02"
    b"D\x01\x00;"
)


# ============================================================
# EMAIL OPEN TRACKING
# ============================================================

@APP.get(
    "/track/<tracking_id>.gif"
)
def track_open(tracking_id):

    if tracking_exists(
        tracking_id
    ):

        log_activity(
            tracking_id,
            "open"
        )

    response = APP.response_class(
        GIF_1X1,
        status=200,
        mimetype="image/gif"
    )

    response.headers[
        "Cache-Control"
    ] = (
        "no-store, "
        "no-cache, "
        "must-revalidate, "
        "max-age=0"
    )

    response.headers[
        "Pragma"
    ] = "no-cache"

    response.headers[
        "Expires"
    ] = "0"

    return response


# ============================================================
# TRACKING LINK
# ============================================================

@APP.get(
    "/go/<tracking_id>"
)
def tracked_link(tracking_id):

    if not tracking_exists(
        tracking_id
    ):

        return render_template_string(
            MESSAGE_HTML,
            title="Invalid Link",
            message=(
                "This tracking link is "
                "invalid or expired."
            ),
            back=False
        ), 404

    # --------------------------------------------------------
    # Every request to /go/<tracking_id> = CLICK
    # --------------------------------------------------------

    log_activity(
        tracking_id,
        "click"
    )

    return render_template_string(
        LANDING_HTML,
        tracking_id=tracking_id
    )


# ============================================================
# PAGE VISIT
# ============================================================

@APP.post(
    "/api/page-visit/<tracking_id>"
)
def page_visit(tracking_id):

    if not tracking_exists(
        tracking_id
    ):

        return jsonify({
            "success": False,
            "error": "Invalid tracking ID"
        }), 404

    log_activity(
        tracking_id,
        "page_visit"
    )

    return jsonify({
        "success": True
    })


# ============================================================
# FORM SUBMISSION
#
# Only explicitly submitted form values are stored.
# ============================================================

@APP.post(
    "/api/form-submit/<tracking_id>"
)
def form_submit(tracking_id):

    if not tracking_exists(
        tracking_id
    ):

        return jsonify({
            "success": False,
            "error": "Invalid tracking ID"
        }), 404

    # --------------------------------------------------------
    # Read form values
    # --------------------------------------------------------

    name = request.form.get(
        "name",
        ""
    ).strip()

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    email = request.form.get(
        "email",
        ""
    ).strip()

    consent = request.form.get(
        "consent",
        ""
    ).strip()

    # --------------------------------------------------------
    # Consent
    # --------------------------------------------------------

    if consent != "yes":

        return render_template_string(
            FORM_ERROR_HTML,
            tracking_id=tracking_id,
            message=(
                "Please confirm the consent checkbox "
                "before submitting."
            )
        ), 400

    # --------------------------------------------------------
    # Name validation
    # --------------------------------------------------------

    if not name:

        return render_template_string(
            FORM_ERROR_HTML,
            tracking_id=tracking_id,
            message="Name is required."
        ), 400

    if len(name) > 100:

        return render_template_string(
            FORM_ERROR_HTML,
            tracking_id=tracking_id,
            message="Name is too long."
        ), 400

    # --------------------------------------------------------
    # Phone validation
    # --------------------------------------------------------

    if not valid_phone(phone):

        return render_template_string(
            FORM_ERROR_HTML,
            tracking_id=tracking_id,
            message=(
                "Please enter a valid contact number."
            )
        ), 400

    # --------------------------------------------------------
    # Email validation
    # --------------------------------------------------------

    if not valid_email(email):

        return render_template_string(
            FORM_ERROR_HTML,
            tracking_id=tracking_id,
            message=(
                "Please enter a valid email address."
            )
        ), 400

    # --------------------------------------------------------
    # Save submission
    # --------------------------------------------------------

    now = utc_now()

    conn = get_db()

    conn.execute("""
        INSERT INTO form_submissions (

            tracking_id,
            name,
            phone,
            email,
            submitted_at,
            ip,
            user_agent,
            referer

        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        tracking_id,
        name,
        phone,
        email,
        now,
        request_ip(),
        request_user_agent(),
        request_referer()
    ))

    # --------------------------------------------------------
    # Also save FORM SUBMIT activity event
    # --------------------------------------------------------

    conn.execute("""
        INSERT INTO activity (

            tracking_id,
            event,
            timestamp,
            ip,
            user_agent,
            referer

        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        tracking_id,
        "form_submit",
        now,
        request_ip(),
        request_user_agent(),
        request_referer()
    ))

    conn.commit()

    conn.close()

    return render_template_string(
        FORM_SUCCESS_HTML,
        name=escape(name)
    )


# ============================================================
# ACTIVITY PAGE
#
# Shows:
#   1. Activity history
#   2. Form submissions
# ============================================================

@APP.get(
    "/api/activity/<tracking_id>"
)
def get_activity(tracking_id):

    if not tracking_exists(
        tracking_id
    ):

        return render_template_string(
            MESSAGE_HTML,
            title="Invalid Tracking ID",
            message=(
                "This tracking ID is invalid."
            ),
            back=True
        ), 404

    conn = get_db()

    # --------------------------------------------------------
    # Activity
    # --------------------------------------------------------

    activity_rows = conn.execute("""
        SELECT
            id,
            event,
            timestamp,
            ip,
            user_agent,
            referer
        FROM activity
        WHERE tracking_id = %s
        ORDER BY id ASC
    """, (
        tracking_id,
    )).fetchall()

    # --------------------------------------------------------
    # Form submissions
    # --------------------------------------------------------

    submission_rows = conn.execute("""
        SELECT
            id,
            name,
            phone,
            email,
            submitted_at,
            ip,
            user_agent,
            referer
        FROM form_submissions
        WHERE tracking_id = %s
        ORDER BY id DESC
    """, (
        tracking_id,
    )).fetchall()

    # --------------------------------------------------------
    # Email details
    # --------------------------------------------------------

    email_row = conn.execute("""
        SELECT
            recipient,
            subject,
            sent_at,
            open_count,
            first_opened_at,
            last_opened_at,
            click_count,
            first_clicked_at,
            last_clicked_at
        FROM emails
        WHERE tracking_id = %s
    """, (
        tracking_id,
    )).fetchone()

    conn.close()

    return render_template_string(
        ACTIVITY_HTML,
        tracking_id=tracking_id,
        email=email_row,
        activity=activity_rows,
        submissions=submission_rows,
        format_time=display_time
    )


# ============================================================
# DOWNLOAD ACTIVITY + FORM SUBMISSION REPORT
# ============================================================

@APP.get(
    "/api/activity/<tracking_id>/report"
)
def download_activity_report(tracking_id):

    if not tracking_exists(tracking_id):

        return render_template_string(
            MESSAGE_HTML,
            title="Invalid Tracking ID",
            message="This tracking ID is invalid.",
            back=True
        ), 404

    conn = get_db()

    email_row = conn.execute("""
        SELECT *
        FROM emails
        WHERE tracking_id = %s
    """, (tracking_id,)).fetchone()

    activity_rows = conn.execute("""
        SELECT id, event, timestamp, ip, user_agent, referer
        FROM activity
        WHERE tracking_id = %s
        ORDER BY id ASC
    """, (tracking_id,)).fetchall()

    submission_rows = conn.execute("""
        SELECT id, name, phone, email, submitted_at, ip, user_agent, referer
        FROM form_submissions
        WHERE tracking_id = %s
        ORDER BY id ASC
    """, (tracking_id,)).fetchall()

    conn.close()

    output = io.StringIO(newline="")
    writer = csv.writer(output)

    writer.writerow(["EMAIL TRACKING REPORT"])
    writer.writerow(["Tracking ID", tracking_id])
    writer.writerow(["Recipient", email_row["recipient"]])
    writer.writerow(["Subject", email_row["subject"] or ""])
    writer.writerow(["Sent At", display_time(email_row["sent_at"])])
    writer.writerow(["Open Count", email_row["open_count"] or 0])
    writer.writerow(["First Open", display_time(email_row["first_opened_at"])])
    writer.writerow(["Last Open", display_time(email_row["last_opened_at"])])
    writer.writerow(["Click Count", email_row["click_count"] or 0])
    writer.writerow(["First Click", display_time(email_row["first_clicked_at"])])
    writer.writerow(["Last Click", display_time(email_row["last_clicked_at"])])
    writer.writerow([])

    writer.writerow(["ACTIVITY"])
    writer.writerow(["ID", "Event", "Time", "IP", "User Agent", "Referer"])

    for row in activity_rows:
        writer.writerow([
            row["id"],
            row["event"],
            display_time(row["timestamp"]),
            row["ip"] or "",
            row["user_agent"] or "",
            row["referer"] or ""
        ])

    writer.writerow([])
    writer.writerow(["FORM SUBMISSIONS"])
    writer.writerow([
        "ID", "Name", "Phone", "Email", "Submitted",
        "IP", "User Agent", "Referer"
    ])

    for row in submission_rows:
        writer.writerow([
            row["id"],
            row["name"] or "",
            row["phone"] or "",
            row["email"] or "",
            display_time(row["submitted_at"]),
            row["ip"] or "",
            row["user_agent"] or "",
            row["referer"] or ""
        ])

    response = APP.response_class(
        output.getvalue(),
        mimetype="text/csv"
    )

    response.headers["Content-Disposition"] = (
        'attachment; filename="'
        + tracking_id
        + '_activity_report.csv"'
    )

    return response




# ============================================================
# FORM SUBMISSIONS API
#
# Kept as an API too.
# ============================================================

@APP.get(
    "/api/submissions/<tracking_id>"
)
def get_submissions(tracking_id):

    if not tracking_exists(
        tracking_id
    ):

        return jsonify({
            "error": "Invalid tracking ID"
        }), 404

    conn = get_db()

    rows = conn.execute("""
        SELECT
            id,
            tracking_id,
            name,
            phone,
            email,
            submitted_at,
            ip,
            user_agent,
            referer
        FROM form_submissions
        WHERE tracking_id = %s
        ORDER BY id DESC
    """, (
        tracking_id,
    )).fetchall()

    conn.close()

    output = []

    for row in rows:

        item = dict(row)

        item["submitted_ist"] = display_time(
            item["submitted_at"]
        )

        output.append(
            item
        )

    return jsonify(output)


# ============================================================
# EMAIL API
# ============================================================

@APP.get(
    "/api/emails"
)
def get_emails():

    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM emails
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    output = []

    for row in rows:

        item = dict(row)

        item["sent_ist"] = display_time(
            item["sent_at"]
        )

        # ----------------------------------------------------
        # First Open / Last Open intentionally NOT returned.
        # ----------------------------------------------------

        item.pop(
            "first_opened_at",
            None
        )

        item.pop(
            "last_opened_at",
            None
        )

        item["first_click_ist"] = display_time(
            item["first_clicked_at"]
        )

        item["last_click_ist"] = display_time(
            item["last_clicked_at"]
        )

        item["first_page_visit_ist"] = display_time(
            item["first_page_visit_at"]
        )

        item["last_page_visit_ist"] = display_time(
            item["last_page_visit_at"]
        )

        output.append(
            item
        )

    return jsonify(output)



# ============================================================
# CAMPAIGN REPORT
# Parent = campaign_id
# Child  = tracking_id
# ============================================================

def _report_rows():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            e.id,
            e.campaign_id,
            e.tracking_id,
            e.recipient,
            e.recipient_name,
            e.recipient_email,
            e.subject,
            e.sent_at,
            e.open_count,
            e.first_opened_at,
            e.last_opened_at,
            e.click_count,
            e.first_clicked_at,
            e.last_clicked_at,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM activity a
                    WHERE a.tracking_id = e.tracking_id
                      AND a.event = 'open'
                ), 0
            ) AS total_open_events,
            COALESCE(
                (
                    SELECT COUNT(DISTINCT
                        COALESCE(a.ip,'') || '|' ||
                        COALESCE(a.user_agent,'')
                    )
                    FROM activity a
                    WHERE a.tracking_id = e.tracking_id
                      AND a.event = 'open'
                ), 0
            ) AS unique_open_signatures,
            COALESCE(
                (
                    SELECT COUNT(DISTINCT
                        COALESCE(a.ip,'') || '|' ||
                        COALESCE(a.user_agent,'')
                    )
                    FROM activity a
                    WHERE a.tracking_id = e.tracking_id
                      AND a.event = 'click'
                ), 0
            ) AS unique_click_signatures
        FROM emails e
        ORDER BY e.id DESC
    """).fetchall()
    conn.close()
    return rows


@APP.get("/report")
def campaign_report():
    rows = _report_rows()

    # Overall summary across every email ever sent.
    sent = len(rows)
    opened = sum(1 for r in rows if (r["open_count"] or 0) > 0)
    clicked = sum(1 for r in rows if (r["click_count"] or 0) > 0)
    total_opens = sum(int(r["open_count"] or 0) for r in rows)
    total_clicks = sum(int(r["click_count"] or 0) for r in rows)

    return render_template_string(
        CAMPAIGN_REPORT_HTML,
        rows=rows,
        sent=sent,
        opened=opened,
        not_opened=sent-opened,
        clicked=clicked,
        not_clicked=sent-clicked,
        total_opens=total_opens,
        total_clicks=total_clicks,
        open_rate=round(opened / sent * 100, 1) if sent else 0,
        click_rate=round(clicked / sent * 100, 1) if sent else 0,
        format_time=display_time
    )


@APP.get("/report.csv")
def campaign_report_csv():
    rows = _report_rows()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Parent Campaign ID",
        "Child Tracking ID",
        "Name",
        "Email",
        "Subject",
        "Sent",
        "Opened",
        "Total Opens",
        "Unique Open Signatures",
        "Clicked",
        "Total Clicks",
        "Unique Click Signatures",
        "First Open",
        "Last Open",
        "First Click",
        "Last Click",
        "Possible Forward"
    ])

    for r in rows:
        unique_opens = int(r["unique_open_signatures"] or 0)
        possible_forward = "POSSIBLE" if unique_opens > 1 else ""
        writer.writerow([
            r["campaign_id"] or "legacy",
            r["tracking_id"],
            r["recipient_name"] or "",
            r["recipient_email"] or r["recipient"],
            r["subject"] or "",
            display_time(r["sent_at"]),
            "YES" if (r["open_count"] or 0) > 0 else "NO",
            r["open_count"] or 0,
            unique_opens,
            "YES" if (r["click_count"] or 0) > 0 else "NO",
            r["click_count"] or 0,
            int(r["unique_click_signatures"] or 0),
            display_time(r["first_opened_at"]),
            display_time(r["last_opened_at"]),
            display_time(r["first_clicked_at"]),
            display_time(r["last_clicked_at"]),
            possible_forward
        ])

    response = APP.response_class(
        output.getvalue(),
        mimetype="text/csv"
    )
    response.headers["Content-Disposition"] = (
        'attachment; filename="email_campaign_report.csv"'
    )
    return response


CAMPAIGN_REPORT_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email Campaign Report</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f4f6f9;font-family:Arial,sans-serif;color:#222}
.wrap{max-width:1700px;margin:auto;padding:22px}
h1{margin:0 0 6px}
.muted{color:#6b7280;font-size:13px}
.top{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.btn{display:inline-block;background:#222;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:18px 0}
.card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.num{font-size:28px;font-weight:700;margin-top:7px}
.chart{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}
.bar{height:22px;background:#e5e7eb;border-radius:11px;overflow:hidden;margin:7px 0 13px}
.fill{height:100%;background:#2563eb}
.controls{margin:16px 0}
.controls button{border:1px solid #ddd;background:#fff;border-radius:7px;padding:8px 12px;margin:3px;cursor:pointer}
.table-wrap{overflow:auto;background:#fff;border-radius:12px}
table{width:100%;border-collapse:collapse;min-width:1500px}
th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{background:#f3f4f6;position:sticky;top:0;z-index:1}
.yes{color:#15803d;font-weight:700}
.no{color:#b91c1c;font-weight:700}
.forward{color:#b45309;font-weight:700}
.small{font-size:12px;color:#6b7280}
@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}.wrap{padding:12px}}
</style>
</head>
<body>
<div class="wrap">
<h1>Email Campaign Report</h1>
<div class="muted">One screen: every sent email, opened/not opened, clicked/not clicked, activity and download.</div>

<div class="top">
<a class="btn" href="/">← Dashboard</a>
<a class="btn" href="/report.csv">⬇ Download CSV</a>
</div>

<div class="cards">
<div class="card"><div class="muted">Sent</div><div class="num">{{ sent }}</div></div>
<div class="card"><div class="muted">Opened</div><div class="num">{{ opened }}</div></div>
<div class="card"><div class="muted">Not Opened</div><div class="num">{{ not_opened }}</div></div>
<div class="card"><div class="muted">Total Opens</div><div class="num">{{ total_opens }}</div></div>
<div class="card"><div class="muted">Clicked</div><div class="num">{{ clicked }}</div></div>
<div class="card"><div class="muted">Total Clicks</div><div class="num">{{ total_clicks }}</div></div>
</div>

<div class="chart">
<strong>Open rate — {{ open_rate }}%</strong>
<div class="bar"><div class="fill" style="width:{{ open_rate }}%"></div></div>
<strong>Click rate — {{ click_rate }}%</strong>
<div class="bar"><div class="fill" style="width:{{ click_rate }}%"></div></div>
<div class="small">Forwarding cannot be directly confirmed by Gmail/Outlook. “Possible” is only a heuristic based on different observed tracking signatures.</div>
</div>

<div class="controls">
<button onclick="filterRows('all')">All</button>
<button onclick="filterRows('opened')">Opened</button>
<button onclick="filterRows('not-opened')">Not Opened</button>
<button onclick="filterRows('clicked')">Clicked</button>
<button onclick="filterRows('not-clicked')">Not Clicked</button>
</div>

<div class="table-wrap">
<table id="reportTable">
<thead>
<tr>
<th>Parent Campaign ID</th>
<th>Child Tracking ID</th>
<th>Name</th>
<th>Email</th>
<th>Sent</th>
<th>Opened</th>
<th>Opens</th>
<th>Clicked</th>
<th>Clicks</th>
<th>First Open</th>
<th>Last Open</th>
<th>First Click</th>
<th>Last Click</th>
<th>Possible Forward</th>
</tr>
</thead>
<tbody>
{% for r in rows %}
<tr data-open="{{ 1 if (r['open_count'] or 0)>0 else 0 }}" data-click="{{ 1 if (r['click_count'] or 0)>0 else 0 }}">
<td class="small">{{ r['campaign_id'] or 'legacy' }}</td>
<td class="small">{{ r['tracking_id'] }}</td>
<td>{{ r['recipient_name'] or '—' }}</td>
<td>{{ r['recipient_email'] or r['recipient'] }}</td>
<td class="small">{{ format_time(r['sent_at']) }}</td>
<td class="{{ 'yes' if (r['open_count'] or 0)>0 else 'no' }}">{{ 'YES' if (r['open_count'] or 0)>0 else 'NO' }}</td>
<td>{{ r['open_count'] or 0 }}</td>
<td class="{{ 'yes' if (r['click_count'] or 0)>0 else 'no' }}">{{ 'YES' if (r['click_count'] or 0)>0 else 'NO' }}</td>
<td>{{ r['click_count'] or 0 }}</td>
<td class="small">{{ format_time(r['first_opened_at']) }}</td>
<td class="small">{{ format_time(r['last_opened_at']) }}</td>
<td class="small">{{ format_time(r['first_clicked_at']) }}</td>
<td class="small">{{ format_time(r['last_clicked_at']) }}</td>
<td class="forward">{{ 'POSSIBLE' if (r['unique_open_signatures'] or 0)>1 else '—' }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
</div>
<script>
function filterRows(kind){
  document.querySelectorAll('#reportTable tbody tr').forEach(row=>{
    const opened=row.dataset.open==='1';
    const clicked=row.dataset.click==='1';
    let show =
      kind==='all' ||
      (kind==='opened' && opened) ||
      (kind==='not-opened' && !opened) ||
      (kind==='clicked' && clicked) ||
      (kind==='not-clicked' && !clicked);
    row.style.display=show?'':'none';
  });
}
</script>
</body>
</html>
"""


# ============================================================
# HEALTH
# ============================================================

@APP.get("/health")
def health():

    now = utc_now()

    return jsonify({
        "ok": True,
        "application": "email-tracker",
        "sender": SENDER_EMAIL,
        "public_url": PUBLIC_URL,
        "time_utc": now,
        "time_ist": display_time(now)
    })


# ============================================================
# DASHBOARD HTML
# ============================================================

DASHBOARD_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Email Tracker</title>

<style>

* {
    box-sizing:border-box;
}

body {

    margin:0;

    padding:25px;

    font-family:Arial,sans-serif;

    background:#f4f6f9;

    color:#222;
}

.container {

    max-width:1600px;

    margin:auto;
}

.card {

    background:white;

    border-radius:14px;

    padding:24px;

    margin-bottom:20px;

    box-shadow:
        0 3px 18px
        rgba(0,0,0,.07);
}

h1,
h2 {

    margin-top:0;
}

label {

    display:block;

    font-weight:600;

    margin-top:15px;

    margin-bottom:6px;
}

input,
textarea {

    width:100%;

    padding:12px;

    border:
        1px solid #ccc;

    border-radius:8px;

    font-size:15px;

    font-family:inherit;
}

textarea {

    min-height:140px;

    resize:vertical;
}

button {

    margin-top:18px;

    padding:12px 22px;

    background:#222;

    color:white;

    border:0;

    border-radius:8px;

    cursor:pointer;

    font-size:15px;
}

.public-url {

    background:#f0f2f5;

    padding:10px;

    border-radius:7px;

    word-break:break-all;

    font-family:monospace;
}

.table-wrap {

    overflow-x:auto;
}

table {

    width:100%;

    border-collapse:collapse;

    min-width:1700px;
}

th,
td {

    padding:11px;

    border-bottom:
        1px solid #ddd;

    text-align:left;

    vertical-align:top;
}

th {

    background:#f0f0f0;
}

.badge {

    display:inline-block;

    padding:5px 9px;

    border-radius:6px;

    background:#eee;

    font-weight:600;
}

.small {

    font-size:12px;

    color:#666;

    line-height:1.5;
}

.time {

    white-space:nowrap;

    font-size:13px;
}

a {

    color:#1261a0;

    text-decoration:none;
}

a:hover {

    text-decoration:underline;
}

.action {

    display:inline-block;

    padding:7px 10px;

    margin:3px;

    background:#222;

    color:white;

    border-radius:6px;

    font-size:12px;
}

.action:hover {

    color:white;

    text-decoration:none;
}

</style>

</head>

<body>

<div class="container">


<!-- ======================================================
     HEADER
====================================================== -->

<div class="card">

<h1>
Email Tracker
</h1>

<p>

<strong>
From:
</strong>

{{ sender }}

</p>

<p>

<strong>
Public URL:
</strong>

</p>

<div class="public-url">

{{ public_url }}

</div>

<p>
<a class="action" href="/report">📊 Full Campaign Report</a>
</p>

<p class="small">

Open tracking is based on the tracking-image
request. Email providers may preload images,
so an open event is not guaranteed proof of
manual reading.

Click tracking is generated when the unique
tracking URL is requested.

The information form is shown openly on the
landing page and requires the visitor to
actively submit it with consent.

</p>

</div>


<!-- ======================================================
     SEND EMAIL
====================================================== -->

<div class="card">

<h2>
Send Email
</h2>

<form
    method="POST"
    action="/send"
>

<label>
Recipients
</label>

<textarea
    name="recipients"
    placeholder="user1@example.com
user2@example.com
user3@example.com"
    required
></textarea>

<p class="small">

One recipient per line, or use commas.

</p>


<label>
Subject
</label>

<input
    name="subject"
    type="text"
    placeholder="Subject"
    required
>


<label>
Message
</label>

<textarea
    name="message"
    placeholder="Write your email..."
    required
></textarea>


<button
    type="submit"
>

Send Email

</button>

</form>

</div>


<!-- ======================================================
     TRACKING
====================================================== -->

<div class="card">

<h2>
Tracking
</h2>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>
Recipient
</th>

<th>
Subject
</th>

<th>
Sent
</th>

<th>
Opens
</th>

<th>
Clicks
</th>

<th>
First Click
</th>

<th>
Last Click
</th>

<th>
Page Visits
</th>

<th>
Actions
</th>

</tr>

</thead>

<tbody>

{% for e in emails %}

<tr>

<td>
{{ e["recipient"] }}
</td>

<td>
{{ e["subject"] or "-" }}
</td>

<td class="time">

{{ format_time(e["sent_at"]) }}

</td>

<td>

<span class="badge">

{{ e["open_count"] or 0 }}

</span>

</td>

<td>

<span class="badge">

{{ e["click_count"] or 0 }}

</span>

</td>

<td class="time">

{{ format_time(e["first_clicked_at"]) }}

</td>

<td class="time">

{{ format_time(e["last_clicked_at"]) }}

</td>

<td>

<span class="badge">

{{ e["page_visit_count"] or 0 }}

</span>

</td>

<td>

<a
    class="action"
    href="/api/activity/{{ e["tracking_id"] }}"
    target="_blank"
>
View Activity
</a>

<a
    class="action"
    href="/go/{{ e["tracking_id"] }}"
    target="_blank"
>
Test Link
</a>

</td>

</tr>

{% else %}

<tr>

<td
    colspan="9"
>

No emails sent yet.

</td>

</tr>

{% endfor %}

</tbody>

</table>

</div>

</div>


</div>

</body>

</html>

"""


# ============================================================
# ACTIVITY HTML
# ============================================================

ACTIVITY_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Activity</title>

<style>

* {
    box-sizing:border-box;
}

body {

    margin:0;

    padding:25px;

    background:#f4f6f9;

    color:#222;

    font-family:Arial,sans-serif;
}

.container {

    max-width:1600px;

    margin:auto;
}

.card {

    background:white;

    padding:25px;

    margin-bottom:20px;

    border-radius:14px;

    box-shadow:
        0 4px 20px
        rgba(0,0,0,.07);
}

.header {

    display:flex;

    justify-content:space-between;

    align-items:center;

    gap:15px;

    flex-wrap:wrap;
}

h1,
h2 {

    margin-top:0;
}

.info {

    background:#f0f2f5;

    padding:15px;

    border-radius:9px;

    margin-top:15px;

    line-height:1.7;
}

.table-wrap {

    overflow-x:auto;
}

table {

    width:100%;

    border-collapse:collapse;

    min-width:1000px;
}

th,
td {

    padding:12px;

    border-bottom:
        1px solid #ddd;

    text-align:left;

    vertical-align:top;
}

th {

    background:#f0f0f0;

    font-weight:700;
}

tr:hover {

    background:#fafafa;
}

.event {

    display:inline-block;

    padding:5px 10px;

    border-radius:7px;

    background:#eee;

    font-weight:600;

    text-transform:uppercase;

    font-size:12px;
}

.event-open {

    background:#e8f5e9;
}

.event-click {

    background:#e3f2fd;
}

.event-page {

    background:#fff3e0;
}

.event-form {

    background:#f3e5f5;
}

.small {

    font-size:12px;

    color:#666;

    word-break:break-word;

    line-height:1.5;
}

.time {

    white-space:nowrap;

    font-size:13px;
}

.button-row {

    display:flex;

    gap:10px;

    flex-wrap:wrap;

    margin-top:20px;
}

button,
.action {

    display:inline-block;

    padding:10px 15px;

    border:0;

    border-radius:8px;

    background:#222;

    color:white;

    cursor:pointer;

    text-decoration:none;

    font-size:14px;
}

.delete {

    background:#b42318;
}

.back {

    background:#555;
}

button:hover,
.action:hover {

    opacity:.85;

    color:white;

    text-decoration:none;
}

.empty {

    padding:25px;

    text-align:center;

    color:#777;
}

.warning {

    background:#fff4e5;

    border:1px solid #ffd8a8;

    padding:13px;

    border-radius:8px;

    margin-top:15px;

    font-size:13px;

    line-height:1.5;
}

</style>

</head>

<body>

<div class="container">


<!-- ======================================================
     ACTIVITY HEADER
====================================================== -->

<div class="card">

<div class="header">

<div>

<h1>
Activity
</h1>

<p class="small">

Tracking ID:

{{ tracking_id }}

</p>

</div>

<div>

<a
    class="action back"
    href="/"
>
← Dashboard
</a>

</div>

</div>


{% if email %}

<div class="info">

<strong>
Recipient:
</strong>

{{ email["recipient"] }}

<br>

<strong>
Subject:
</strong>

{{ email["subject"] or "-" }}

<br>

<strong>
Sent:
</strong>

{{ format_time(email["sent_at"]) }}

<br>

<strong>
Opens:
</strong>

{{ email["open_count"] or 0 }}

<br>

<strong>
First Open:
</strong>

{{ format_time(email["first_opened_at"]) }}

<br>

<strong>
Last Open:
</strong>

{{ format_time(email["last_opened_at"]) }}

<br>

<strong>
Clicks:
</strong>

{{ email["click_count"] or 0 }}

<br>

<strong>
First Click:
</strong>

{{ format_time(email["first_clicked_at"]) }}

<br>

<strong>
Last Click:
</strong>

{{ format_time(email["last_clicked_at"]) }}

</div>

{% endif %}


<div class="button-row">

<a
    class="action"
    href="/api/activity/{{ tracking_id }}/report"
>
Download Report (CSV)
</a>

</div>

</div>


<!-- ======================================================
     ACTIVITY HISTORY TABLE
====================================================== -->

<div class="card">

<h2>
Activity History
</h2>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>
#
</th>

<th>
Event
</th>

<th>
Time
</th>

<th>
IP
</th>

<th>
User Agent
</th>

<th>
Referer
</th>

</tr>

</thead>

<tbody>

{% for row in activity %}

<tr>

<td>
{{ row["id"] }}
</td>

<td>

{% if row["event"] == "open" %}

<span class="event event-open">
OPEN
</span>

{% elif row["event"] == "click" %}

<span class="event event-click">
CLICK
</span>

{% elif row["event"] == "page_visit" %}

<span class="event event-page">
PAGE VISIT
</span>

{% elif row["event"] == "form_submit" %}

<span class="event event-form">
FORM SUBMIT
</span>

{% else %}

<span class="event">
{{ row["event"] }}
</span>

{% endif %}

</td>

<td class="time">

{{ format_time(row["timestamp"]) }}

</td>

<td class="small">

{{ row["ip"] or "-" }}

</td>

<td class="small">

{{ row["user_agent"] or "-" }}

</td>

<td class="small">

{{ row["referer"] or "-" }}

</td>

</tr>

{% else %}

<tr>

<td
    colspan="6"
    class="empty"
>

No activity found.

</td>

</tr>

{% endfor %}

</tbody>

</table>

</div>

</div>


<!-- ======================================================
     FORM SUBMISSIONS TABLE
====================================================== -->

<div class="card">

<h2>
Form Submissions
</h2>

<p class="small">

This table contains only the information that
the visitor explicitly entered and submitted
through the disclosed form.

</p>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>
#
</th>

<th>
Name
</th>

<th>
Phone
</th>

<th>
Email
</th>

<th>
Submitted
</th>

<th>
IP
</th>

<th>
User Agent
</th>

<th>
Referer
</th>

</tr>

</thead>

<tbody>

{% for row in submissions %}

<tr>

<td>
{{ row["id"] }}
</td>

<td>
{{ row["name"] }}
</td>

<td>
{{ row["phone"] }}
</td>

<td>
{{ row["email"] }}
</td>

<td class="time">

{{ format_time(row["submitted_at"]) }}

</td>

<td class="small">

{{ row["ip"] or "-" }}

</td>

<td class="small">

{{ row["user_agent"] or "-" }}

</td>

<td class="small">

{{ row["referer"] or "-" }}

</td>

</tr>

{% else %}

<tr>

<td
    colspan="8"
    class="empty"
>

No form has been submitted yet.

</td>

</tr>

{% endfor %}

</tbody>

</table>

</div>

</div>


</div>

</body>

</html>

"""


# ============================================================
# SEND RESULT HTML
# ============================================================

SEND_RESULT_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<title>Send Result</title>

<style>

body {

    font-family:Arial,sans-serif;

    background:#f4f6f9;

    padding:30px;
}

.card {

    max-width:950px;

    margin:auto;

    background:white;

    padding:25px;

    border-radius:12px;

    box-shadow:
        0 4px 20px
        rgba(0,0,0,.08);
}

.result {

    padding:15px;

    margin:10px 0;

    border-radius:8px;

    background:#f3f3f3;
}

.success {

    border-left:
        5px solid #22a06b;
}

.error {

    border-left:
        5px solid #d64545;
}

.url {

    word-break:break-all;

    font-family:monospace;
}

a {

    color:#1261a0;
}

</style>

</head>

<body>

<div class="card">

<h2>
Send Result
</h2>

{% for r in results %}

{% if r.get("success") %}

<div class="result success">

<strong>
Sent:
</strong>

{{ r["recipient"] }}

<br><br>

<strong>
Tracking URL:
</strong>

<div class="url">

<a
    href="{{ r["tracking_url"] }}"
    target="_blank"
>

{{ r["tracking_url"] }}

</a>

</div>

<br>

<strong>
Tracking ID:
</strong>

{{ r["tracking_id"] }}

</div>

{% else %}

<div class="result error">

<strong>
Failed:
</strong>

{{ r.get("recipient", "-") }}

<br><br>

{{ r.get("error", "Unknown error") }}

</div>

{% endif %}

{% endfor %}


{% if invalid %}

<h3>
Invalid addresses
</h3>

<ul>

{% for x in invalid %}

<li>
{{ x }}
</li>

{% endfor %}

</ul>

{% endif %}


<p>

<a href="/">
← Back to Dashboard
</a>

</p>

</div>

</body>

</html>

"""


# ============================================================
# MESSAGE HTML
# ============================================================

MESSAGE_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<title>{{ title }}</title>

<style>

body {

    font-family:Arial,sans-serif;

    background:#f4f6f9;

    padding:40px;
}

.card {

    max-width:600px;

    margin:auto;

    background:white;

    padding:30px;

    border-radius:12px;
}

a {

    color:#1261a0;
}

</style>

</head>

<body>

<div class="card">

<h2>
{{ title }}
</h2>

<p>
{{ message }}
</p>

{% if back %}

<p>

<a href="/">
← Back
</a>

</p>

{% endif %}

</div>

</body>

</html>

"""


# ============================================================
# LANDING PAGE WITH DISCLOSED FORM
# ============================================================

LANDING_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Continue</title>

<style>

* {
    box-sizing:border-box;
}

body {

    margin:0;

    padding:30px;

    background:#f5f7fb;

    font-family:Arial,sans-serif;

    color:#222;
}

.card {

    max-width:520px;

    margin:50px auto;

    padding:30px;

    background:white;

    border-radius:14px;

    box-shadow:
        0 5px 25px
        rgba(0,0,0,.08);
}

h2 {

    margin-top:0;
}

label {

    display:block;

    margin-top:16px;

    margin-bottom:6px;

    font-weight:600;
}

input {

    width:100%;

    padding:12px;

    border:1px solid #ccc;

    border-radius:8px;

    font-size:15px;
}

.consent {

    display:flex;

    gap:10px;

    align-items:flex-start;

    margin-top:18px;

    font-size:13px;

    line-height:1.5;

    color:#555;
}

.consent input {

    width:auto;

    margin-top:3px;
}

button {

    width:100%;

    margin-top:20px;

    padding:13px;

    background:#222;

    color:white;

    border:0;

    border-radius:8px;

    cursor:pointer;

    font-size:15px;
}

.notice {

    background:#f0f2f5;

    padding:12px;

    border-radius:8px;

    font-size:13px;

    line-height:1.5;

    color:#555;

    margin-bottom:20px;
}

</style>

</head>

<body>

<div class="card">

<h2>
Continue
</h2>

<div class="notice">

To continue, please provide the information
below. By checking the consent box and
submitting the form, you agree that the
information you enter will be recorded for
the stated purpose.

</div>


<form
    method="POST"
    action="/api/form-submit/{{ tracking_id }}"
>


<label>
Name
</label>

<input
    type="text"
    name="name"
    maxlength="100"
    autocomplete="name"
    required
>


<label>
Contact number
</label>

<input
    type="tel"
    name="phone"
    maxlength="20"
    autocomplete="tel"
    required
>


<label>
Email address
</label>

<input
    type="email"
    name="email"
    maxlength="254"
    autocomplete="email"
    required
>


<label class="consent">

<input
    type="checkbox"
    name="consent"
    value="yes"
    required
>

<span>

I understand that the name, contact number,
and email address I enter in this form will be
recorded along with the submission time.

</span>

</label>


<button
    type="submit"
>

Submit & Continue

</button>

</form>

</div>


<script>

const trackingId =
    "{{ tracking_id }}";


// ----------------------------------------------------------
// PAGE VISIT
// ----------------------------------------------------------

fetch(
    "/api/page-visit/" + trackingId,
    {
        method:"POST"
    }
)
.catch(
    () => {}
);

</script>

</body>

</html>

"""


# ============================================================
# FORM ERROR HTML
# ============================================================

FORM_ERROR_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Form Error</title>

<style>

body {

    margin:0;

    padding:30px;

    background:#f5f7fb;

    font-family:Arial,sans-serif;
}

.card {

    max-width:500px;

    margin:70px auto;

    background:white;

    padding:30px;

    border-radius:14px;

    box-shadow:
        0 5px 25px
        rgba(0,0,0,.08);

    text-align:center;
}

a {

    color:#1261a0;
}

</style>

</head>

<body>

<div class="card">

<h2>
Form Error
</h2>

<p>
{{ message }}
</p>

<p>

<a href="/go/{{ tracking_id }}">
← Go back to form
</a>

</p>

</div>

</body>

</html>

"""


# ============================================================
# FORM SUCCESS HTML
# ============================================================

FORM_SUCCESS_HTML = """

<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Submitted</title>

<style>

body {

    margin:0;

    padding:30px;

    background:#f5f7fb;

    font-family:Arial,sans-serif;
}

.card {

    max-width:500px;

    margin:70px auto;

    background:white;

    padding:30px;

    border-radius:14px;

    box-shadow:
        0 5px 25px
        rgba(0,0,0,.08);

    text-align:center;
}

</style>

</head>

<body>

<div class="card">

<h2>
Thank you
</h2>

<p>
Your information has been submitted.
</p>

<p>
You may now continue.
</p>

</div>

</body>

</html>

"""


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    print()

    print("=" * 70)

    print(
        "EMAIL TRACKER"
    )

    print("=" * 70)

    print()

    print(
        "Sender:"
    )

    print(
        SENDER_EMAIL
    )

    print()

    print(
        "Public URL:"
    )

    print(
        PUBLIC_URL
    )

    print()

    print(
        "Dashboard:"
    )

    print(
        f"http://127.0.0.1:{PORT}/"
    )

    print()

    print(
        "Health:"
    )

    print(
        f"http://127.0.0.1:{PORT}/health"
    )

    print()

    print(
        "Database:"
    )

    print(
        "PostgreSQL"
    )

    print()

    print(
        "Gmail token:"
    )

    print(
        "GMAIL_TOKEN_JSON environment variable"
        if os.environ.get("GMAIL_TOKEN_JSON")
        else TOKEN_FILE
    )

    print()

    print("=" * 70)

    init_db()

    APP.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True
    )
