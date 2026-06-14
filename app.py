import os
import secrets
import smtplib
import ssl
import certifi
import threading
import time
from email.message import EmailMessage
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from functools import wraps
from collections import defaultdict

from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from pymongo import MongoClient, ASCENDING
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/carpooling_calendar")
DB_NAME = os.getenv("DB_NAME", "carpooling_calendar")
MAIL_SERVER = os.getenv("MAIL_SERVER", "").strip()
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "").strip()
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "").replace(" ", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", MAIL_USERNAME).strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
APP_TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
ENABLE_LOGIN_EMAILS = os.getenv("ENABLE_LOGIN_EMAILS", "true").lower() in ["1", "true", "yes", "on"]
ENABLE_DAILY_EMAILS = os.getenv("ENABLE_DAILY_EMAILS", "true").lower() in ["1", "true", "yes", "on"]
DAILY_EMAIL_TIME = os.getenv("DAILY_EMAIL_TIME", "07:00").strip() or "07:00"
MONGO_TLS_ALLOW_INVALID = os.getenv("MONGO_TLS_ALLOW_INVALID", "false").lower() in ["1", "true", "yes", "on"]

mongo_client_options = {
    "serverSelectionTimeoutMS": 30000,
    "connectTimeoutMS": 30000,
    "socketTimeoutMS": 30000,
}

if MONGO_URI.startswith("mongodb+srv://") or "mongodb.net" in MONGO_URI:
    mongo_client_options.update({
        "tls": True,
        "tlsCAFile": certifi.where(),
    })
    # Emergency Render/Atlas debugging only. Keep false for normal use.
    if MONGO_TLS_ALLOW_INVALID:
        mongo_client_options.update({
            "tlsAllowInvalidCertificates": True,
        })

client = MongoClient(MONGO_URI, **mongo_client_options)
db = client[DB_NAME]

WEEKDAYS = [(0, "Mon", "Monday"), (1, "Tue", "Tuesday"), (2, "Wed", "Wednesday"), (3, "Thu", "Thursday"), (4, "Fri", "Friday")]
WEEKEND = [5, 6]
STATUS_LABELS = {
    "available": "Available",
    "cant_bring_car": "Can’t bring car",
    "may_not_come": "May be absent",
    "off_today": "Off today",
    "leave": "Leave",
}


def init_indexes():
    try:
        db.users.create_index([("email", ASCENDING)], unique=True)
        db.groups.create_index([("created_by", ASCENDING)])
        db.members.create_index([("group_id", ASCENDING), ("email", ASCENDING)], unique=True)
        db.members.create_index([("user_id", ASCENDING)])
        db.assignments.create_index([("group_id", ASCENDING), ("date", ASCENDING)], unique=True)
        db.statuses.create_index([("group_id", ASCENDING), ("member_id", ASCENDING), ("date", ASCENDING)], unique=True)
        db.alerts.create_index([("group_id", ASCENDING), ("created_at", ASCENDING)])
        db.invites.create_index([("token", ASCENDING)], unique=True)
        db.password_resets.create_index([("token", ASCENDING)], unique=True)
        db.password_resets.create_index([("expires_at", ASCENDING)])
        db.job_logs.create_index([("key", ASCENDING)], unique=True)
    except Exception:
        pass


def oid(value):
    return value if isinstance(value, ObjectId) else ObjectId(value)


def today_date():
    try:
        return local_now().date()
    except Exception:
        return date.today()


def to_iso(d):
    return d.isoformat() if isinstance(d, date) else str(d)


def monday_of_week(d=None):
    d = d or today_date()
    return d - timedelta(days=d.weekday())


def week_dates(d=None):
    start = monday_of_week(d)
    return [start + timedelta(days=i) for i in range(7)]


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        return db.users.find_one({"_id": oid(uid)})
    except Exception:
        return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def avatar_for_gender(gender):
    return "female.png" if str(gender).lower() == "female" else "male.png"


def member_avatar(member):
    # Always use the selected gender image. This also fixes old MongoDB rows
    # that may still contain male1.svg/female1.svg from older versions.
    return avatar_for_gender(member.get("gender", "male"))


def get_active_group(user):
    if not user:
        return None
    gid = session.get("group_id")
    if gid:
        member = db.members.find_one({"group_id": oid(gid), "user_id": user["_id"], "status": "confirmed"})
        if member:
            group = db.groups.find_one({"_id": oid(gid)})
            if group:
                return group
    member = db.members.find_one({"user_id": user["_id"], "status": "confirmed"})
    if member:
        session["group_id"] = str(member["group_id"])
        return db.groups.find_one({"_id": member["group_id"]})
    return None


def ensure_user_member(group, user, working_days=None):
    working_days = working_days or [0, 1, 2, 3, 4]
    payload = {
        "group_id": group["_id"],
        "user_id": user["_id"],
        "name": user["name"],
        "email": user["email"].lower(),
        "gender": user.get("gender", "male"),
        "avatar": avatar_for_gender(user.get("gender", "male")),
        "working_days": working_days,
        "weekly_days_count": len(working_days),
        "status": "confirmed",
        "updated_at": datetime.utcnow(),
    }
    existing = db.members.find_one({"group_id": group["_id"], "email": user["email"].lower()})
    if existing:
        db.members.update_one({"_id": existing["_id"]}, {"$set": payload})
        return db.members.find_one({"_id": existing["_id"]})
    payload["created_at"] = datetime.utcnow()
    res = db.members.insert_one(payload)
    return db.members.find_one({"_id": res.inserted_id})


def enriched_members(group_id):
    members = list(db.members.find({"group_id": oid(group_id), "status": "confirmed"}).sort("created_at", ASCENDING))
    for m in members:
        m["id"] = str(m["_id"])
        m["avatar"] = member_avatar(m)
        m["initials"] = "".join([p[:1] for p in m.get("name", "U").split()[:2]]).upper()
        m["days_short"] = [WEEKDAYS[d][1] for d in m.get("working_days", []) if 0 <= d <= 4]
        m["weekly_days_count"] = len(m.get("working_days", []))
    return members


def pending_invites_for(group_id):
    invites = list(db.invites.find({"group_id": oid(group_id), "status": "pending"}).sort("created_at", ASCENDING))
    for inv in invites:
        inv["id"] = str(inv["_id"])
        inv["days_short"] = [WEEKDAYS[d][1] for d in inv.get("working_days", []) if 0 <= d <= 4]
        inv["created_label"] = inv.get("created_at", datetime.utcnow()).strftime("%d %b %Y")
    return invites

def status_for(group_id, member_id, d):
    row = db.statuses.find_one({"group_id": oid(group_id), "member_id": oid(member_id), "date": to_iso(d)})
    return row.get("status", "available") if row else "available"


def is_available(member, d):
    wd = d.weekday()
    if wd in WEEKEND:
        return False
    if wd not in member.get("working_days", []):
        return False
    return status_for(member["group_id"], member["_id"], d) == "available"


def assignment_for(group_id, d):
    return db.assignments.find_one({"group_id": oid(group_id), "date": to_iso(d)})


def duty_counts(group_id, from_date=None, to_date=None):
    query = {"group_id": oid(group_id)}
    if from_date and to_date:
        query["date"] = {"$gte": to_iso(from_date), "$lte": to_iso(to_date)}
    counts = defaultdict(int)
    for a in db.assignments.find(query):
        if a.get("member_id"):
            counts[str(a["member_id"])] += 1
    return counts


def pick_driver(group_id, d, exclude_member_id=None):
    """Pick the fairest available driver for a specific date.

    Rules:
    - Only confirmed members who come on that weekday are eligible.
    - Skip people who marked Can’t bring car / May not come / Off today.
    - Prefer the lowest duty count for the current week.
    - Normalize by number of coming days so 3-day members are not overloaded.
    - Avoid assigning the same person on the previous working day when possible.
    """
    members = enriched_members(group_id)
    week_start = monday_of_week(d)
    week_end = week_start + timedelta(days=4)
    week_counts = duty_counts(group_id, week_start, week_end)
    all_counts = duty_counts(group_id)

    previous_driver_id = None
    prev = d - timedelta(days=1)
    while prev.weekday() in WEEKEND and prev >= week_start:
        prev -= timedelta(days=1)
    if prev >= week_start:
        prev_assignment = assignment_for(group_id, prev)
        if prev_assignment and prev_assignment.get("member_id"):
            previous_driver_id = str(prev_assignment["member_id"])

    candidates = []
    for m in members:
        mid = str(m["_id"])
        if exclude_member_id and mid == str(exclude_member_id):
            continue
        if not is_available(m, d):
            continue

        days_count = max(1, len(m.get("working_days", [])))
        week_duties = week_counts.get(mid, 0)
        total_duties = all_counts.get(mid, 0)
        fairness_score = week_duties / days_count
        repeat_penalty = 1 if previous_driver_id == mid else 0
        candidates.append((fairness_score, repeat_penalty, week_duties, total_duties, m.get("created_at", datetime.utcnow()), m))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    return candidates[0][5]


def ensure_assignment(group_id, d):
    if d.weekday() in WEEKEND:
        return None
    existing = assignment_for(group_id, d)
    if existing and existing.get("member_id"):
        driver = db.members.find_one({"_id": existing["member_id"]})
        if driver and is_available(driver, d):
            return existing
    exclude = existing.get("member_id") if existing else None
    driver = pick_driver(group_id, d, exclude_member_id=exclude)
    doc = {
        "group_id": oid(group_id),
        "date": to_iso(d),
        "member_id": driver["_id"] if driver else None,
        "type": "suggested" if driver else "empty",
        "reason": "Fair rotation based on availability and duty count" if driver else "No available members",
        "updated_at": datetime.utcnow(),
    }
    db.assignments.update_one({"group_id": oid(group_id), "date": to_iso(d)}, {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}}, upsert=True)
    return assignment_for(group_id, d)


def week_context(group, base_date=None):
    days = []
    for d in week_dates(base_date):
        assign = ensure_assignment(group["_id"], d) if d.weekday() < 5 else None
        driver = None
        if assign and assign.get("member_id"):
            driver = db.members.find_one({"_id": assign["member_id"]})
            if driver:
                driver["avatar"] = member_avatar(driver)
        days.append({"date": d, "iso": to_iso(d), "short": d.strftime("%a"), "day_num": d.day, "month": d.strftime("%b"), "is_today": d == today_date(), "is_weekend": d.weekday() in WEEKEND, "driver": driver, "assignment": assign})
    return days



def refresh_week_assignments(group_id, base_date=None):
    """Rebuild this week's Mon-Fri assignments so newly added members appear in the weekly schedule."""
    start = monday_of_week(base_date or today_date())
    end = start + timedelta(days=4)
    db.assignments.delete_many({"group_id": oid(group_id), "date": {"$gte": to_iso(start), "$lte": to_iso(end)}})
    for i in range(5):
        ensure_assignment(group_id, start + timedelta(days=i))


def build_public_url(endpoint, **values):
    """Build a public URL for links sent by email."""
    path = url_for(endpoint, **values)
    return f"{APP_BASE_URL}{path}"


def mail_ready():
    return all([MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM])


def _format_from_header():
    sender_name = os.getenv("MAIL_FROM_NAME", "Car Pool Manager").strip() or "Car Pool Manager"
    return f"{sender_name} <{MAIL_FROM}>"


def _add_email_footer(body):
    body = (body or "").strip()
    footer = f"""

---
Car Pool Manager
This notification was sent because you are a member of a car pool group or received an invitation.
If this email is in Spam, mark it as "Not spam" once so Gmail learns to trust this sender.
""".strip()
    if "This notification was sent" in body:
        return body
    return f"{body}\n\n{footer}\n"


def send_email(to_email, subject, body):
    """Send a clean plain-text email. Returns (ok, message). Never crashes the app."""
    to_email = (to_email or "").strip().lower()
    if not to_email:
        return False, "Missing recipient email."
    if not mail_ready():
        missing = [name for name, value in [("MAIL_SERVER", MAIL_SERVER), ("MAIL_USERNAME", MAIL_USERNAME), ("MAIL_PASSWORD", MAIL_PASSWORD), ("MAIL_FROM", MAIL_FROM)] if not value]
        return False, "Mail settings missing: " + ", ".join(missing)
    try:
        clean_subject = str(subject or "Car Pool Manager notification").replace("\n", " ").strip()
        clean_body = _add_email_footer(body)
        msg = EmailMessage()
        msg["Subject"] = clean_subject
        msg["From"] = _format_from_header()
        msg["To"] = to_email
        msg["Reply-To"] = MAIL_FROM
        msg["X-Mailer"] = "Car Pool Manager"
        msg["List-ID"] = "Car Pool Manager Notifications <car-pool-manager.local>"
        msg.set_content(clean_body)

        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=25) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
            smtp.send_message(msg)
        return True, "Email sent."
    except Exception as exc:
        app.logger.warning("Email failed to %s: %s", to_email, exc)
        return False, str(exc)




def local_now():
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE))
    except Exception:
        return datetime.now()


def send_login_notification(user):
    if not ENABLE_LOGIN_EMAILS or not user:
        return
    login_time = local_now().strftime("%d %b %Y, %I:%M %p")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    body = f"""Hi {user.get('name', 'there')},

Your Car Pool Manager account was logged in.

Time: {login_time}
IP: {ip}

If this was you, no action is needed. If this was not you, reset your password immediately.

- Car Pool Manager
"""
    send_email(user.get("email"), "New login to your Car Pool Manager account", body)


def create_password_reset(user):
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(minutes=45)
    db.password_resets.insert_one({
        "token": token,
        "user_id": user["_id"],
        "email": user.get("email", "").lower(),
        "used": False,
        "created_at": datetime.utcnow(),
        "expires_at": expires,
    })
    return token


def send_password_reset_email(user):
    token = create_password_reset(user)
    reset_link = build_public_url("reset_password", token=token)
    body = f"""Hi {user.get('name', 'there')},

We received a request to reset your Car Pool Manager password.

Reset your password here:
{reset_link}

This link expires in 45 minutes. If you did not request this, you can ignore this email.

- Car Pool Manager
"""
    return send_email(user.get("email"), "Reset your Car Pool Manager password", body)

def group_member_emails(group_id, exclude_emails=None):
    exclude = {str(e).lower() for e in (exclude_emails or []) if e}
    emails = []
    for member in db.members.find({"group_id": oid(group_id), "status": "confirmed"}):
        email = str(member.get("email", "")).lower().strip()
        if email and email not in exclude and email not in emails:
            emails.append(email)
    return emails


def send_group_email(group_id, subject, body, exclude_emails=None):
    sent = 0
    failed = 0
    for email in group_member_emails(group_id, exclude_emails=exclude_emails):
        ok, _ = send_email(email, subject, body)
        if ok:
            sent += 1
        else:
            failed += 1
    return sent, failed

def create_alert(group_id, title, message, kind="info"):
    db.alerts.insert_one({"group_id": oid(group_id), "title": title, "message": message, "kind": kind, "read": False, "created_at": datetime.utcnow()})


def get_today_panel(group):
    d = today_date()
    assign = ensure_assignment(group["_id"], d) if d.weekday() < 5 else None
    driver = None
    if assign and assign.get("member_id"):
        driver = db.members.find_one({"_id": assign["member_id"]})
        if driver:
            driver["avatar"] = member_avatar(driver)
    statuses = []
    for m in enriched_members(group["_id"]):
        if d.weekday() < 5 and d.weekday() in m.get("working_days", []):
            st = status_for(group["_id"], m["_id"], d)
            statuses.append({"member": m, "status": st, "label": STATUS_LABELS.get(st, st)})
    return {"date": d, "assignment": assign, "driver": driver, "statuses": statuses}


@app.context_processor
def inject_globals():
    user = current_user()
    if user:
        user["avatar"] = avatar_for_gender(user.get("gender", "male"))
    group = get_active_group(user) if user else None
    unread = db.alerts.count_documents({"group_id": group["_id"], "read": False}) if group else 0
    return {"current_user": user, "active_group": group, "unread_alerts": unread, "weekdays": WEEKDAYS, "status_labels": STATUS_LABELS}


@app.route("/")
def index():
    return redirect(url_for("dashboard") if current_user() else url_for("register"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        gender = request.form.get("gender", "male")
        if not name or not email or not password:
            flash("Please fill all fields.", "error")
            return redirect(url_for("register"))
        pending_token = session.get("pending_invite")
        pending_invite = db.invites.find_one({"token": pending_token, "status": "pending"}) if pending_token else None
        if pending_invite and email != pending_invite.get("email", "").lower():
            flash(f"This invite is for {pending_invite.get('email')}. Please create an account with that same email.", "error")
            return redirect(url_for("register"))
        if db.users.find_one({"email": email}):
            flash("Account already exists. Please login.", "error")
            return redirect(url_for("login"))
        user_doc = {"name": name, "email": email, "password_hash": generate_password_hash(password), "gender": gender, "avatar": avatar_for_gender(gender), "created_at": datetime.utcnow()}
        res = db.users.insert_one(user_doc)
        session["user_id"] = str(res.inserted_id)
        if session.get("pending_invite"):
            flash("Account created. Now accept your group invite.", "success")
            return redirect(url_for("accept_invite"))
        flash("Account created. Create or join your carpool group.", "success")
        return redirect(url_for("onboarding"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        user = db.users.find_one({"email": email})
        if not user or not check_password_hash(user.get("password_hash", ""), password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))
        pending_token = session.get("pending_invite")
        pending_invite = db.invites.find_one({"token": pending_token, "status": "pending"}) if pending_token else None
        if pending_invite and email != pending_invite.get("email", "").lower():
            flash(f"This invite is for {pending_invite.get('email')}. Please login with that same email.", "error")
            return redirect(url_for("login"))
        session["user_id"] = str(user["_id"])
        send_login_notification(user)
        if session.get("pending_invite"):
            flash("Welcome back. You can accept your invite now.", "success")
            return redirect(url_for("accept_invite"))
        group = get_active_group(user)
        flash("Welcome back.", "success")
        return redirect(url_for("dashboard") if group else url_for("onboarding"))
    return render_template("auth.html", mode="login")




@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        user = db.users.find_one({"email": email}) if email else None
        # Show same message even if email does not exist, so nobody can guess accounts.
        if user:
            ok, msg = send_password_reset_email(user)
            if not ok:
                flash(f"Reset email could not be sent: {msg}", "error")
                return redirect(url_for("forgot_password"))
        flash("If that email exists, a reset link has been sent. Check Inbox/Spam.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = db.password_resets.find_one({"token": token, "used": False})
    if not reset or reset.get("expires_at", datetime.utcnow()) < datetime.utcnow():
        flash("Reset link is invalid or expired.", "error")
        return redirect(url_for("forgot_password"))
    user = db.users.find_one({"_id": reset["user_id"]})
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("reset_password", token=token))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password", token=token))
        db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": generate_password_hash(password), "updated_at": datetime.utcnow()}})
        db.password_resets.update_one({"_id": reset["_id"]}, {"$set": {"used": True, "used_at": datetime.utcnow()}})
        flash("Password reset successful. Please login.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", token=token, email=user.get("email"))

@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("register"))


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    user = current_user()
    if request.method == "POST":
        group_name = request.form.get("group_name", "Office Crew").strip() or "Office Crew"
        days = [int(x) for x in request.form.getlist("working_days")] or [0, 1, 2, 3, 4]
        group_doc = {"name": group_name, "created_by": user["_id"], "share_code": secrets.token_urlsafe(6), "created_at": datetime.utcnow()}
        res = db.groups.insert_one(group_doc)
        group = db.groups.find_one({"_id": res.inserted_id})
        ensure_user_member(group, user, days)
        session["group_id"] = str(group["_id"])
        refresh_week_assignments(group["_id"])
        create_alert(group["_id"], "Group created", f"{user['name']} created {group_name}.", "success")
        flash("Group created successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("onboarding.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    try:
        week_offset = int(request.args.get("week", 0))
    except (TypeError, ValueError):
        week_offset = 0
    display_base = today_date() + timedelta(days=week_offset * 7)
    week_start = monday_of_week(display_base)
    refresh_week_assignments(group["_id"], display_base)
    days = week_context(group, display_base)
    members = enriched_members(group["_id"])
    today = get_today_panel(group)
    week_counts = duty_counts(group["_id"], week_start, week_start + timedelta(days=6))
    for m in members:
        m["week_count"] = week_counts.get(str(m["_id"]), 0)
    suggested = today.get("driver")
    return render_template("dashboard.html", group=group, days=days, members=members, today=today, suggested=suggested, week_offset=week_offset)




@app.route("/email-test")
@login_required
def email_test():
    user = current_user()
    ok, msg = send_email(
        user.get("email"),
        "Car Pool Manager test email",
        f"Hi {user.get('name', 'there')},\n\nThis is a test email from Car Pool Manager. If you received this, Gmail SMTP is working.\n\n- Car Pool Manager"
    )
    if ok:
        flash("Test email sent to your login email. Check Inbox/Spam.", "success")
    else:
        flash(f"Test email failed: {msg}", "error")
    return redirect(url_for("people"))


@app.route("/people", methods=["GET", "POST"])
@login_required
def people():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        gender = request.form.get("gender", "male")
        days = [int(x) for x in request.form.getlist("working_days")] or [0, 1, 2, 3, 4]
        if not name or not email:
            flash("Enter member name and email.", "error")
            return redirect(url_for("people"))
        if db.members.find_one({"group_id": group["_id"], "email": email, "status": "confirmed"}):
            flash("This person is already a confirmed group member.", "error")
            return redirect(url_for("people"))
        token = secrets.token_urlsafe(24)
        db.invites.update_one({"group_id": group["_id"], "email": email, "status": "pending"}, {"$set": {"group_id": group["_id"], "name": name, "email": email, "gender": gender, "avatar": avatar_for_gender(gender), "working_days": days, "weekly_days_count": len(days), "token": token, "status": "pending", "invited_by": user["_id"], "updated_at": datetime.utcnow()}, "$setOnInsert": {"created_at": datetime.utcnow()}}, upsert=True)
        create_alert(group["_id"], "Invite sent", f"{user['name']} invited {name} to join {group['name']}.", "info")
        invite_link = build_public_url("invite", token=token)
        subject = f"You’re invited to join {group['name']} on Car Pool Manager"
        body = f"""Hi {name},

{user['name']} invited you to join the car pool group "{group['name']}" on Car Pool Manager.

Accept your invite here:
{invite_link}

Suggested coming days: {', '.join([WEEKDAYS[d][1] for d in days if 0 <= d <= 4])}

If you did not expect this invite, you can ignore this email.

- Car Pool Manager
"""
        ok, mail_msg = send_email(email, subject, body)
        if ok:
            flash("Invite created and email sent.", "success")
        else:
            flash("Invite created, but email could not be sent. Copy the invite link manually.", "warning")
        return redirect(url_for("people"))
    members = enriched_members(group["_id"])
    pending = pending_invites_for(group["_id"])
    current_member = db.members.find_one({"group_id": group["_id"], "user_id": user["_id"], "status": "confirmed"})
    current_member_id = str(current_member["_id"]) if current_member else ""
    return render_template("people.html", group=group, members=members, pending=pending, current_member_id=current_member_id)

@app.route("/invite/<invite_id>/cancel", methods=["POST"])
@login_required
def cancel_invite(invite_id):
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    invite_row = db.invites.find_one({"_id": oid(invite_id), "group_id": group["_id"], "status": "pending"})
    if not invite_row:
        flash("Invite not found.", "error")
        return redirect(url_for("people"))
    db.invites.update_one({"_id": invite_row["_id"]}, {"$set": {"status": "cancelled", "cancelled_at": datetime.utcnow(), "cancelled_by": user["_id"]}})
    create_alert(group["_id"], "Invite cancelled", f"Invite for {invite_row.get('email')} was cancelled.", "warning")
    send_email(invite_row.get("email"), "Car Pool Manager invite cancelled", f"Hi {invite_row.get('name', 'there')},\n\nYour invite to join {group['name']} was cancelled.\n\n- Car Pool Manager")
    flash("Invite cancelled.", "success")
    return redirect(url_for("people"))


@app.route("/member/<member_id>/remove", methods=["POST"])
@login_required
def remove_member(member_id):
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    member = db.members.find_one({"_id": oid(member_id), "group_id": group["_id"], "status": "confirmed"})
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("people"))
    if str(member.get("user_id", "")) == str(user["_id"]):
        flash("Use Leave group for your own account.", "error")
        return redirect(url_for("people"))
    db.members.update_one({"_id": member["_id"]}, {"$set": {"status": "removed", "removed_at": datetime.utcnow(), "removed_by": user["_id"]}})
    db.assignments.delete_many({"group_id": group["_id"], "member_id": member["_id"]})
    refresh_week_assignments(group["_id"])
    create_alert(group["_id"], "Member removed", f"{member.get('name', 'A member')} was removed from the group by {user['name']}.", "warning")
    send_email(member.get("email"), "You were removed from a car pool group", f"""Hi {member.get('name', 'there')},

You were removed from the car pool group "{group['name']}" by {user['name']}.

- Car Pool Manager""")
    send_group_email(group["_id"], "Member removed from Car Pool Manager", f"""{member.get('name', 'A member')} was removed from "{group['name']}" by {user['name']}. The weekly schedule was refreshed.

- Car Pool Manager""", exclude_emails=[member.get("email")])
    flash("Member removed, schedule refreshed, and email notifications sent if configured.", "success")
    return redirect(url_for("people"))


@app.route("/group/leave", methods=["POST"])
@login_required
def leave_group():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    member = db.members.find_one({"group_id": group["_id"], "user_id": user["_id"], "status": "confirmed"})
    if not member:
        flash("You are not a confirmed member of this group.", "error")
        return redirect(url_for("people"))
    db.members.update_one({"_id": member["_id"]}, {"$set": {"status": "left", "left_at": datetime.utcnow()}})
    db.assignments.delete_many({"group_id": group["_id"], "member_id": member["_id"]})
    refresh_week_assignments(group["_id"])
    create_alert(group["_id"], "Member left", f"{user['name']} left the group.", "warning")
    send_group_email(group["_id"], "Member left Car Pool Manager group", f"""{user['name']} left the car pool group "{group['name']}". The weekly schedule was refreshed.

- Car Pool Manager""", exclude_emails=[user.get("email")])
    session.pop("group_id", None)
    flash("You left the group. Email notifications were sent if configured.", "success")
    return redirect(url_for("onboarding"))


@app.route("/member/<member_id>/schedule", methods=["GET", "POST"])
@login_required
def member_schedule(member_id):
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    member = db.members.find_one({"_id": oid(member_id), "group_id": group["_id"]})
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("people"))
    if request.method == "POST":
        days = [int(x) for x in request.form.getlist("working_days")]
        db.members.update_one({"_id": member["_id"]}, {"$set": {"working_days": days, "weekly_days_count": len(days), "updated_at": datetime.utcnow()}})
        refresh_week_assignments(group["_id"])
        create_alert(group["_id"], "Schedule updated", f"{member.get('name', 'A member')} updated coming days.", "info")
        send_group_email(group["_id"], "Car Pool Manager schedule updated", f"""{member.get('name', 'A member')} updated coming days in "{group['name']}". The weekly rotation was refreshed.

- Car Pool Manager""")
        flash("Schedule updated and group notified if email is configured.", "success")
        return redirect(url_for("people"))
    member["avatar"] = member_avatar(member)
    return render_template("member_schedule.html", group=group, member=member)


@app.route("/history")
@login_required
def history():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    rows = []
    for a in db.assignments.find({"group_id": group["_id"]}).sort("date", ASCENDING):
        driver = db.members.find_one({"_id": a.get("member_id")}) if a.get("member_id") else None
        rows.append({"assignment": a, "driver": driver})
    return render_template("history.html", group=group, rows=rows)


@app.route("/alerts")
@login_required
def alerts():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    rows = list(db.alerts.find({"group_id": group["_id"]}).sort("created_at", -1).limit(50))
    return render_template("alerts.html", group=group, alerts=rows)


@app.route("/status/<action>", methods=["GET", "POST"])
@login_required
def status_action(action):
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    member = db.members.find_one({"group_id": group["_id"], "user_id": user["_id"], "status": "confirmed"})
    if not member:
        flash("Your member profile is missing.", "error")
        return redirect(url_for("dashboard"))
    if action not in ["cant_bring_car", "may_not_come", "off_today", "available"]:
        action = "available"
    if today_date().weekday() in WEEKEND:
        flash("Today is a holiday. No car rotation action is needed.", "info")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        d = today_date()
        db.statuses.update_one({"group_id": group["_id"], "member_id": member["_id"], "date": to_iso(d)}, {"$set": {"group_id": group["_id"], "member_id": member["_id"], "date": to_iso(d), "status": action, "reason": reason, "updated_at": datetime.utcnow()}}, upsert=True)
        old = assignment_for(group["_id"], d)
        if old and old.get("member_id") == member["_id"] and action in ["cant_bring_car", "may_not_come", "off_today"]:
            replacement = pick_driver(group["_id"], d, exclude_member_id=member["_id"])
            db.assignments.update_one({"group_id": group["_id"], "date": to_iso(d)}, {"$set": {"member_id": replacement["_id"] if replacement else None, "type": "reassigned", "reason": f"{member['name']} marked {STATUS_LABELS.get(action, action)}", "updated_at": datetime.utcnow()}}, upsert=True)
            if replacement:
                create_alert(group["_id"], "Driver reassigned", f"{replacement['name']} is now suggested because {member['name']} marked {STATUS_LABELS.get(action)}.", "warning")
                send_group_email(group["_id"], "Driver reassigned in Car Pool Manager", f"{replacement['name']} is now suggested as today's driver because {member['name']} marked: {STATUS_LABELS.get(action)}.\n\nGroup: {group['name']}\nDate: {d.strftime('%A, %d %b %Y')}\n\n- Car Pool Manager")
            else:
                create_alert(group["_id"], "No replacement found", f"{member['name']} is unavailable and no replacement was found.", "warning")
                send_group_email(group["_id"], "No replacement driver found", f"{member['name']} marked: {STATUS_LABELS.get(action)}. No replacement driver was found for today.\n\nGroup: {group['name']}\nDate: {d.strftime('%A, %d %b %Y')}\n\n- Car Pool Manager")
        else:
            create_alert(group["_id"], "Status updated", f"{member['name']} marked {STATUS_LABELS.get(action, action)}.", "info")
            send_group_email(group["_id"], "Car Pool Manager status update", f"{member['name']} marked: {STATUS_LABELS.get(action, action)}.\n\nGroup: {group['name']}\nDate: {d.strftime('%A, %d %b %Y')}\n\n- Car Pool Manager")
        flash("Status updated and group notified.", "success")
        return redirect(url_for("dashboard"))
    return render_template("status.html", group=group, action=action, label=STATUS_LABELS.get(action, action))


@app.route("/invite/<token>")
def invite(token):
    invite_row = db.invites.find_one({"token": token, "status": "pending"})
    if not invite_row:
        flash("Invite link is invalid or already used.", "error")
        return redirect(url_for("login"))
    session["pending_invite"] = token
    if not current_user():
        flash("Create an account or login to accept this invite.", "info")
        return redirect(url_for("register"))
    return redirect(url_for("accept_invite"))


@app.route("/accept-invite", methods=["GET", "POST"])
@login_required
def accept_invite():
    token = session.get("pending_invite")
    invite_row = db.invites.find_one({"token": token, "status": "pending"}) if token else None
    if not invite_row:
        return redirect(url_for("onboarding"))
    group = db.groups.find_one({"_id": invite_row["group_id"]})
    user = current_user()
    if user.get("email", "").lower() != invite_row.get("email", "").lower():
        flash(f"This invite is for {invite_row.get('email')}. Your current account is {user.get('email')}. Please use the invited email to accept.", "error")
        session.pop("pending_invite", None)
        group_for_user = get_active_group(user)
        return redirect(url_for("dashboard") if group_for_user else url_for("onboarding"))
    if request.method == "POST":
        days = [int(x) for x in request.form.getlist("working_days")] or invite_row.get("working_days", [0, 1, 2, 3, 4])
        member = ensure_user_member(group, user, days)
        db.members.update_one({"_id": member["_id"]}, {"$set": {"gender": invite_row.get("gender", user.get("gender", "male")), "avatar": avatar_for_gender(invite_row.get("gender", user.get("gender", "male"))), "updated_at": datetime.utcnow()}})
        db.invites.update_one({"_id": invite_row["_id"]}, {"$set": {"status": "accepted", "accepted_at": datetime.utcnow(), "accepted_by": user["_id"]}})
        session["group_id"] = str(group["_id"])
        session.pop("pending_invite", None)
        refresh_week_assignments(group["_id"])
        create_alert(group["_id"], "New member joined", f"{user['name']} accepted the invite and joined the group.", "success")
        send_group_email(group["_id"], "New member joined Car Pool Manager", f"""{user['name']} joined the car pool group "{group['name']}". The weekly schedule was refreshed.

- Car Pool Manager""", exclude_emails=[user.get("email")])
        flash("Invite accepted. You joined the group.", "success")
        return redirect(url_for("dashboard"))
    invite_row["days_short"] = [WEEKDAYS[d][1] for d in invite_row.get("working_days", []) if 0 <= d <= 4]
    return render_template("accept_invite.html", group=group, invite=invite_row)




def daily_turn_email_for_group(group):
    d = today_date()
    key = f"daily-turn:{group['_id']}:{to_iso(d)}"
    try:
        claimed = db.job_logs.update_one(
            {"key": key},
            {"$setOnInsert": {"key": key, "created_at": datetime.utcnow(), "status": "started"}},
            upsert=True,
        )
        if not claimed.upserted_id:
            return False
    except Exception:
        return False

    if d.weekday() in WEEKEND:
        subject = f"Today is a holiday - {group.get('name', 'Car Pool')}"
        body = f"""Hi team,

Today is {d.strftime('%A, %d %b %Y')}. Saturday and Sunday are holidays, so there is no car rotation today.

- Car Pool Manager
"""
    else:
        assign = ensure_assignment(group["_id"], d)
        driver = db.members.find_one({"_id": assign.get("member_id")}) if assign and assign.get("member_id") else None
        if driver:
            subject = f"Today’s car turn: {driver.get('name')}"
            body = f"""Hi team,

Today’s car turn for {group.get('name', 'your group')}:

Driver: {driver.get('name')}
Email: {driver.get('email')}
Date: {d.strftime('%A, %d %b %Y')}

Please coordinate if plans change.

- Car Pool Manager
"""
        else:
            subject = "No driver assigned today"
            body = f"""Hi team,

No available driver was found for {d.strftime('%A, %d %b %Y')} in {group.get('name', 'your group')}.

Please update member availability in Car Pool Manager.

- Car Pool Manager
"""
    sent, failed = send_group_email(group["_id"], subject, body)
    db.job_logs.update_one({"key": key}, {"$set": {"status": "done", "sent": sent, "failed": failed, "finished_at": datetime.utcnow()}})
    return True


def send_daily_turn_emails():
    for group in db.groups.find({}):
        daily_turn_email_for_group(group)


def daily_email_loop():
    while True:
        try:
            now = local_now()
            target_hour, target_minute = [int(x) for x in DAILY_EMAIL_TIME.split(":")[:2]]
            if now.hour == target_hour and now.minute == target_minute and mail_ready():
                send_daily_turn_emails()
                time.sleep(70)
            else:
                time.sleep(20)
        except Exception as exc:
            app.logger.warning("Daily email loop error: %s", exc)
            time.sleep(60)


def start_daily_email_scheduler():
    if not ENABLE_DAILY_EMAILS:
        return
    thread = threading.Thread(target=daily_email_loop, name="daily-turn-email", daemon=True)
    thread.start()


@app.route("/daily-email-test")
@login_required
def daily_email_test():
    user = current_user()
    group = get_active_group(user)
    if not group:
        return redirect(url_for("onboarding"))
    d = today_date()
    if d.weekday() in WEEKEND:
        subject = f"Today is a holiday - {group.get('name', 'Car Pool')}"
        body = f"""Today is {d.strftime('%A, %d %b %Y')}.
There is no car rotation today.

- Car Pool Manager"""
    else:
        assign = ensure_assignment(group["_id"], d)
        driver = db.members.find_one({"_id": assign.get("member_id")}) if assign and assign.get("member_id") else None
        subject = f"Today’s car turn: {driver.get('name') if driver else 'No driver'}"
        body = f"""Today’s driver: {driver.get('name') if driver else 'No driver'}
Date: {d.strftime('%A, %d %b %Y')}
Group: {group.get('name')}

- Car Pool Manager"""
    ok, msg = send_email(user.get("email"), subject, body)
    flash("Daily turn test email sent to you. Check Inbox/Spam." if ok else f"Daily email test failed: {msg}", "success" if ok else "error")
    return redirect(url_for("dashboard"))

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


init_indexes()
start_daily_email_scheduler()

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000, use_reloader=False)
