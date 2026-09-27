"""Deterministic, context-grounded Vera challenge API (Python standard library only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

START = time.monotonic()
SCOPES = ("category", "merchant", "customer", "trigger")
MAX_BODY = 500_000
DB_PATH = os.environ.get("STATE_DB", "/tmp/magicpin-vera.sqlite3")
LOCK = threading.RLock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS contexts (scope TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(scope,id))")
    conn.execute("CREATE TABLE IF NOT EXISTS sent (recipient TEXT NOT NULL, suppression_key TEXT NOT NULL, PRIMARY KEY(recipient,suppression_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, merchant_id TEXT, customer_id TEXT, trigger_id TEXT, last_body TEXT, last_message TEXT, auto_count INTEGER DEFAULT 0, ended INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS blocked (recipient TEXT PRIMARY KEY)")
    conn.commit()
    return conn


@contextmanager
def connect():
    conn = db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def obj(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def clean(value, limit=160):
    s = re.sub(r"https?://\S+|www\.\S+", "", str(value or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].rstrip(" ,;:-")


def get_context(conn, scope, cid):
    if not isinstance(cid, str) or not cid:
        return None
    row = conn.execute("SELECT payload FROM contexts WHERE scope=? AND id=?", (scope, cid)).fetchone()
    return json.loads(row[0]) if row else None


def first_name(merchant, category):
    identity = obj(merchant.get("identity"))
    name = clean(identity.get("owner_first_name"), 50)
    if not name:
        name = clean(identity.get("name"), 60) or "there"
    if category.get("slug") == "dentists" and name != "there" and not name.lower().startswith("dr"):
        return "Dr. " + name
    return name


def active_offer(merchant, topic=""):
    active = [x for x in items(merchant.get("offers")) if isinstance(x, dict) and x.get("status") == "active" and x.get("title")]
    topic = clean(topic).lower().replace("_", " ")
    match = next((x for x in active if any(w in clean(x["title"]).lower() for w in topic.split() if len(w) > 3)), None)
    return clean((match or (active[0] if active else {})).get("title"), 65)


def digest_item(category, payload):
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    found = next((x for x in items(category.get("digest")) if isinstance(x, dict) and x.get("id") == wanted), None)
    if not found and payload.get("category") == category.get("slug"):
        found = next((x for x in items(category.get("digest")) if isinstance(x, dict)), None)
    return obj(found)


def percent(value):
    try:
        return f"{abs(float(value)) * 100:g}%"
    except (ValueError, TypeError):
        return ""


def metric_text(payload, merchant):
    metric = clean(payload.get("metric"), 30) or "activity"
    change = percent(payload.get("delta_pct"))
    direction = "down" if float(payload.get("delta_pct") or 0) < 0 else "up"
    window = clean(payload.get("window"), 20) or "recently"
    base = f"{metric} {direction} {change} over {window}" if change else f"{metric} changed over {window}"
    actual = obj(merchant.get("performance")).get(metric)
    if isinstance(actual, (float, int)) and not isinstance(actual, bool):
        base += f"; your current 30-day {metric}: {actual:g}"
    return base


def customer_allowed(customer, kind):
    if not customer or not obj(customer.get("consent")).get("opted_in_at"):
        return False
    if obj(customer.get("preferences")).get("reminder_opt_in") is False:
        return False
    scopes = items(obj(customer.get("consent")).get("scope"))
    required = ("recall_reminders",) if kind == "recall_due" else (
        ("appointment_reminders",) if kind == "appointment_tomorrow" else
        ("refill_reminders", "prescription_reminders", "appointment_reminders") if kind == "chronic_refill_due" else
        ("promotional_offers", "followup", "followup_reminders", "appointment_reminders")
    )
    return any(s in scopes for s in required)


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Compose from current context; never claim an action was performed or invent an offer."""
    category, merchant, trigger = obj(category), obj(merchant), obj(trigger)
    payload = obj(trigger.get("payload"))
    kind = clean(trigger.get("kind"), 50)
    name = first_name(merchant, category)
    business = clean(obj(merchant.get("identity")).get("name"), 65)
    locality = clean(obj(merchant.get("identity")).get("locality"), 40)
    cat = category.get("slug", "")
    offer = active_offer(merchant, str(payload.get("service_due") or payload.get("intent_topic") or ""))
    item = digest_item(category, payload)
    fact = ""
    task = "draft a short message you can review"
    cta = "binary_yes_no"
    extra = ""

    if kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert"):
        title = clean(item.get("title"), 145)
        source = clean(item.get("source"), 75)
        if kind == "supply_alert":
            batch = ", ".join(clean(x, 30) for x in items(payload.get("affected_batches"))[:3])
            fact = f"Stock alert for {clean(payload.get('molecule'), 35)}" + (f"; batches {batch}" if batch else "")
            task = "draft a stock-check and customer notice for your review"
        else:
            fact = title or clean(payload.get("category_relevance"), 80) or "A category update arrived"
            task = "prepare a concise summary and a draft you can review"
        if source:
            extra = f" Source: {source}."
        if kind == "research_digest" and item.get("summary"):
            extra += f" Finding: {clean(item['summary'], 145)}."
            segment = str(item.get("patient_segment", ""))
            cohort = obj(merchant.get("customer_aggregate")).get("high_risk_adult_count")
            if "high_risk" in segment and isinstance(cohort, int):
                extra += f" Your roster has {cohort} high-risk adults."
        if kind == "regulation_change" and payload.get("deadline_iso"):
            extra += f" Deadline: {clean(payload['deadline_iso'], 30)}."
        if kind == "cde_opportunity" and payload.get("credits") is not None:
            extra += f" {payload['credits']} credits; fee: {clean(payload.get('fee'), 30).replace('_', ' ')}."
    elif kind in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
        fact = metric_text(payload, merchant)
        if kind == "seasonal_perf_dip" and payload.get("is_expected_seasonal"):
            extra = " The trigger marks this as a seasonal pattern."
            task = "draft a retention-focused post to review"
        else:
            task = "review one practical next step from these numbers"
    elif kind == "renewal_due":
        fact = f"{clean(payload.get('plan'), 30)} plan: {payload.get('days_remaining')} days remaining"
        if payload.get("renewal_amount") is not None:
            fact += f"; renewal amount ₹{payload['renewal_amount']}"
        task = "prepare the renewal details for your review"
    elif kind == "festival_upcoming":
        fact = f"{clean(payload.get('festival'), 35)} on {clean(payload.get('date'), 30)}"
        if offer:
            extra = f" Your active offer is {offer}."
        task = "draft a timely listing post using your existing offer"
    elif kind == "ipl_match_today":
        fact = f"{clean(payload.get('match'), 40)} at {clean(payload.get('venue'), 50)}"
        if payload.get("match_time_iso"):
            extra = f" Kickoff: {clean(payload['match_time_iso'], 35)}."
        if offer and not (payload.get("is_weeknight") is False and re.search(r"Tue\s*[-–]\s*Thu", offer, re.I)):
            extra += f" Your active offer is {offer}."
        else:
            offer = ""
        task = "draft a match-day message around your current offer" if offer else "draft a match-day listing update"
    elif kind == "review_theme_emerged":
        fact = f"{payload.get('occurrences_30d', 'Several')} recent reviews mention {clean(payload.get('theme'), 45).replace('_', ' ')}"
        if payload.get("common_quote"):
            extra = f" One says: “{clean(payload['common_quote'], 90)}.”"
        task = "draft a measured response for your review"
    elif kind == "milestone_reached":
        current, target = payload.get("value_now"), payload.get("milestone_value")
        fact = f"{clean(payload.get('metric'), 40).replace('_', ' ')}: {current}"
        if isinstance(current, (int, float)) and isinstance(target, (int, float)):
            fact += f"; {max(0, target-current):g} to {target:g}" if current < target else f"; milestone {target:g} reached"
        task = "draft a thank-you post to review"
    elif kind == "active_planning_intent":
        topic = clean(payload.get("intent_topic"), 60).replace("_", " ")
        fact = f"You asked about {topic}"
        if offer:
            extra = f" Existing offer: {offer}."
        task = "put together a first draft using your current details"
    elif kind in ("winback_eligible", "dormant_with_vera"):
        if kind == "winback_eligible":
            fact = f"Subscription expired {payload.get('days_since_expiry')} days ago"
            if payload.get("lapsed_customers_added_since_expiry") is not None:
                extra = f" {payload['lapsed_customers_added_since_expiry']} customers became lapsed since then."
        else:
            fact = f"It has been {payload.get('days_since_last_merchant_message')} days since your last Vera message"
        task = "review a small next step for your listing"
    elif kind == "curious_ask_due":
        fact = f"Quick question for {business or 'your business'}"
        task = "turn your answer into a short listing post"
        cta = "open_ended"
    elif kind == "category_seasonal":
        fact = f"{clean(payload.get('season'), 45).replace('_',' ')} demand shift"
        trends = [clean(x, 35).replace("_", " ") for x in items(payload.get("trends"))[:3]]
        if trends:
            extra = f" Signals: {', '.join(trends)}."
        task = "draft a shelf-planning checklist"
    elif kind == "gbp_unverified":
        fact = f"{business or 'Your listing'} is still unverified"
        if payload.get("verification_path"):
            extra = f" Verification path: {clean(payload['verification_path'], 45).replace('_', ' ')}."
        task = "prepare the verification steps for your review"
    elif kind == "competitor_opened":
        fact = f"{clean(payload.get('competitor_name'), 55)} opened {payload.get('distance_km')} km away"
        if payload.get("their_offer"):
            extra = f" Their listed offer: {clean(payload['their_offer'], 65)}."
        task = "review your listing and current offer"
    elif kind == "customer_lapsed_soft":
        fact = "A customer follow-up is due"
    else:
        fields = [(k, v) for k, v in payload.items() if isinstance(v, (str, int, float)) and k not in ("placeholder", "metric_or_topic") and v not in (None, "")]
        fact = "; ".join(f"{clean(k, 25).replace('_',' ')}: {clean(v, 65)}" for k, v in fields[:2]) or kind.replace("_", " ")

    if customer is not None:
        customer_name = clean(obj(customer.get("identity")).get("name"), 50) or "there"
        salutation = f"Hi {customer_name}, {business or 'your local business'} here. "
        slots = [clean(s.get("label"), 35) for s in items(payload.get("available_slots") or payload.get("next_session_options")) if isinstance(s, dict) and s.get("label")]
        if kind == "recall_due":
            fact = f"Your {clean(payload.get('service_due'), 50).replace('_', ' ')} is due {clean(payload.get('due_date'), 30)}"
            if payload.get("last_service_date"):
                extra = f" Your last visit was {clean(payload['last_service_date'], 30)}."
            task = "Reply YES if you'd like us to check an appointment"
        elif kind == "chronic_refill_due":
            molecules = ", ".join(clean(x, 30) for x in items(payload.get("molecule_list"))[:5])
            fact = f"Your refill reminder for {molecules} is due" if molecules else "Your refill reminder is due"
            if payload.get("stock_runs_out_iso"):
                extra = f" Date in your reminder: {clean(payload['stock_runs_out_iso'], 35)}."
            task = "Reply YES if you'd like us to check availability with the pharmacist"
        elif kind == "appointment_tomorrow":
            fact = "Your appointment reminder is here"
            if payload.get("date"):
                extra = f" Date: {clean(payload['date'], 35)}."
            task = "Reply YES to confirm, or contact the business if plans changed"
        elif kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
            days = payload.get("days_since_last_visit")
            fact = f"It's been {days} days since your last visit" if isinstance(days, int) else "We'd be glad to see you again"
            task = "Reply YES if you'd like to hear what's available"
        elif kind == "trial_followup":
            fact = f"Thank you for visiting on {clean(payload.get('trial_date'), 30)}"
            task = "Reply YES if you'd like to arrange another session"
        elif kind == "wedding_package_followup":
            fact = f"Following up on your trial before {clean(payload.get('wedding_date'), 30)}"
            task = "Reply YES if you'd like to review the next step"
        if offer and kind not in ("chronic_refill_due", "appointment_tomorrow"):
            extra += f" Current offer: {offer}."
        if slots:
            extra += f" Available options: {', '.join(slots[:2])}."
            task = "Reply with the option that suits you" if len(slots) > 1 else "Reply YES if you'd like us to check that time"
            cta = "multi_choice_slot" if len(slots) > 1 else "binary_yes_no"
        if "hi" in str(obj(customer.get("identity")).get("language_pref", "")).lower():
            task = task.replace("Reply YES", "YES reply karein").replace("Reply with the option that suits you", "Jo slot theek lage, wahi reply karein")
        body = salutation + clean(fact, 170).rstrip(".") + "." + extra + " " + task + "."
        send_as = "merchant_on_behalf"
    else:
        place = f" in {locality}" if locality and kind in ("competitor_opened", "gbp_unverified") else ""
        if kind == "curious_ask_due":
            body = f"{name}, {fact}. What's the most requested service this week? I'll {task}."
        elif kind == "active_planning_intent":
            body = f"{name}, {fact}. Here is a starting point: highlight the service, who it's for, and your confirmed terms.{extra} Reply CONFIRM and I'll refine this into a post for you."
        else:
            body = f"{name}, {fact}{place}." + extra + f" Want me to {task}? Reply YES or STOP."
        send_as = "vera"

    body = re.sub(r"\s+", " ", body).strip()
    return {"body": body, "cta": cta, "send_as": send_as,
            "suppression_key": clean(trigger.get("suppression_key"), 200) or clean(trigger.get("id"), 200),
            "rationale": f"{kind or 'event'} for {cat or 'merchant'}; uses the current trigger and merchant context" + (" with opted-in customer context" if customer else "")}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def err(reason, details):
    return {"accepted": False, "reason": reason, "details": details}


class API(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/healthz":
            with LOCK, connect() as conn:
                counts = {scope: conn.execute("SELECT count(*) FROM contexts WHERE scope=?", (scope,)).fetchone()[0] for scope in SCOPES}
            self.respond(200, {"status": "ok", "uptime_seconds": int(time.monotonic() - START), "contexts_loaded": counts})
        elif self.path == "/v1/metadata":
            members = [s.strip() for s in os.environ.get("TEAM_MEMBERS", "").split(",") if s.strip()]
            metadata = {"team_name": os.environ.get("TEAM_NAME", "Vera Context Engine"), "team_members": members,
                        "model": "deterministic-local-v1", "approach": "versioned context store and trigger-kind grounded composer",
                        "version": "1.0.0"}
            if os.environ.get("CONTACT_EMAIL"):
                metadata["contact_email"] = os.environ["CONTACT_EMAIL"]
            self.respond(200, metadata)
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdecimal() or int(length) > MAX_BODY:
            self.respond(413 if length and length.isdecimal() and int(length) > MAX_BODY else 400, err("invalid_body", "Content-Length is required and must be at most 500000 bytes"))
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, UnicodeDecodeError):
            self.respond(400, err("invalid_json", "Request must be valid UTF-8 JSON"))
            return
        if not isinstance(body, dict):
            self.respond(400, err("invalid_body", "Expected a JSON object"))
            return
        if self.path == "/v1/context":
            self.context(body)
        elif self.path == "/v1/tick":
            self.tick(body)
        elif self.path == "/v1/reply":
            self.reply(body)
        elif self.path == "/v1/teardown":
            with LOCK, connect() as conn:
                for table in ("contexts", "sent", "conversations", "blocked"):
                    conn.execute(f"DELETE FROM {table}")
            self.respond(200, {"cleared": True})
        else:
            self.respond(404, {"error": "not_found"})

    def context(self, body):
        scope, cid, version, payload = (body.get(k) for k in ("scope", "context_id", "version", "payload"))
        if scope not in SCOPES:
            self.respond(400, err("invalid_scope", "scope must be category, merchant, customer, or trigger")); return
        if not isinstance(cid, str) or not cid or len(cid) > 256 or not isinstance(version, int) or isinstance(version, bool) or version < 1 or not isinstance(payload, dict):
            self.respond(400, err("invalid_context", "context_id, positive integer version, and object payload are required")); return
        identity_field = {"category": "slug", "merchant": "merchant_id", "customer": "customer_id", "trigger": "id"}[scope]
        if payload.get(identity_field) != cid:
            self.respond(400, err("invalid_context", f"payload.{identity_field} must equal context_id")); return
        if scope == "trigger" and payload.get("scope") not in ("merchant", "customer"):
            self.respond(400, err("invalid_context", "trigger payload.scope must be merchant or customer")); return
        with LOCK, connect() as conn:
            row = conn.execute("SELECT version FROM contexts WHERE scope=? AND id=?", (scope, cid)).fetchone()
            if row and version <= row[0]:
                self.respond(409, {"accepted": False, "reason": "stale_version", "current_version": row[0]}); return
            conn.execute("INSERT INTO contexts(scope,id,version,payload) VALUES(?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET version=excluded.version,payload=excluded.payload", (scope, cid, version, json.dumps(payload, ensure_ascii=False)))
        ack = hashlib.sha256(f"{scope}:{cid}:{version}".encode()).hexdigest()[:16]
        self.respond(200, {"accepted": True, "ack_id": f"ack_{ack}", "stored_at": now_iso()})

    def tick(self, body):
        tids = body.get("available_triggers")
        if not isinstance(body.get("now"), str) or not isinstance(tids, list) or any(not isinstance(tid, str) for tid in tids):
            self.respond(400, {"error": "invalid_tick", "details": "now string and available_triggers string array are required"}); return
        actions = []
        with LOCK, connect() as conn:
            candidates = []
            for tid in dict.fromkeys(tids[:1000]):
                trigger = get_context(conn, "trigger", tid)
                if trigger:
                    candidates.append((int(trigger.get("urgency") or 0), tid, trigger))
            # The active hint is authoritative; sample trigger expiry dates can predate simulator wall time.
            for _, tid, trigger in sorted(candidates, key=lambda x: (-x[0], x[1])):
                if len(actions) >= 20:
                    break
                # A bare placeholder describes no real event; restraint beats a fabricated nudge.
                detail = obj(trigger.get("payload"))
                if not any(k not in ("placeholder", "metric_or_topic") and v not in (None, "", [], {}) for k, v in detail.items()):
                    continue
                mid = trigger.get("merchant_id") or obj(trigger.get("payload")).get("merchant_id")
                merchant = get_context(conn, "merchant", mid)
                if not merchant:
                    continue
                category = get_context(conn, "category", merchant.get("category_slug"))
                if not category:
                    continue
                cid = trigger.get("customer_id") if trigger.get("scope") == "customer" else None
                if trigger.get("scope") == "customer" and not cid:
                    continue
                customer = get_context(conn, "customer", cid) if cid else None
                if cid and (not customer or customer.get("merchant_id") != mid or not customer_allowed(customer, trigger.get("kind"))):
                    continue
                recipient = cid or mid
                if conn.execute("SELECT 1 FROM blocked WHERE recipient=?", (recipient,)).fetchone():
                    continue
                composed = compose(category, merchant, trigger, customer)
                key = composed["suppression_key"]
                if conn.execute("SELECT 1 FROM sent WHERE recipient=? AND suppression_key=?", (recipient, key)).fetchone():
                    continue
                conv = "conv_" + hashlib.sha256(f"{recipient}:{tid}:{key}".encode()).hexdigest()[:24]
                action = {"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                          "send_as": composed["send_as"], "trigger_id": tid,
                          "template_name": "merchant_context_v1" if cid else "vera_context_v1",
                          "template_params": [composed["body"]], **composed}
                conn.execute("INSERT OR IGNORE INTO sent VALUES(?,?)", (recipient, key))
                conn.execute("INSERT OR IGNORE INTO conversations(id,merchant_id,customer_id,trigger_id,last_body) VALUES(?,?,?,?,?)", (conv, mid, cid, tid, composed["body"]))
                actions.append(action)
        self.respond(200, {"actions": actions})

    def reply(self, body):
        conv_id, message, role = (body.get(k) for k in ("conversation_id", "message", "from_role"))
        if not isinstance(conv_id, str) or not conv_id or len(conv_id) > 256 or not isinstance(message, str) or not message.strip() or role not in ("merchant", "customer"):
            self.respond(400, {"error": "invalid_reply", "details": "conversation_id, from_role, and nonempty message are required"}); return
        low = message.strip().lower()
        auto = any(s in low for s in ("thank you for contacting", "we will respond shortly", "we'll get back to you", "this is an automated", "our team will respond"))
        stop = bool(re.search(r"\b(stop|unsubscribe|not interested|don't message|do not message|no more|spam)\b", low))
        defer = bool(re.search(r"\b(later|busy|tomorrow|next week|after some time)\b", low))
        accept = bool(re.search(r"\b(yes|confirm|go ahead|do it|let's do it|lets do it|proceed|send|draft|sure|okay|ok|haan)\b", low))
        with LOCK, connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            mid = body.get("merchant_id") or (row["merchant_id"] if row else None)
            cid = body.get("customer_id") or (row["customer_id"] if row else None)
            if row and row["ended"]:
                response = {"action": "end", "rationale": "Conversation already closed"}
            elif stop:
                if cid or mid:
                    conn.execute("INSERT OR IGNORE INTO blocked VALUES(?)", (cid or mid,))
                response = {"action": "end", "rationale": "Explicit opt-out; recipient suppressed"}
            elif auto:
                count = (row["auto_count"] if row else 0) + 1
                response = {"action": "end", "rationale": "Repeated automated response; ending"} if count >= 2 or not row else {"action": "wait", "wait_seconds": 14400, "rationale": "Detected a canned auto-reply; waiting for a person"}
            elif defer:
                response = {"action": "wait", "wait_seconds": 86400 if "tomorrow" in low or "next week" in low else 1800, "rationale": "Recipient asked for time"}
            elif accept:
                trigger = get_context(conn, "trigger", row["trigger_id"]) if row else None
                merchant = get_context(conn, "merchant", mid) if mid else None
                category = get_context(conn, "category", merchant.get("category_slug")) if merchant else None
                offer = active_offer(merchant or {}, obj((trigger or {}).get("payload")).get("intent_topic", ""))
                topic = clean((trigger or {}).get("kind", "your request")).replace("_", " ")
                specific = f" Current confirmed offer: {offer}." if offer else ""
                content = f"Here is a draft for {topic}: {clean(obj(merchant or {}).get('identity', {}).get('name'), 60) or 'Your business'} can help with this. {specific} Please review the wording and details before anything is sent."
                response = {"action": "send", "body": content, "cta": "binary_confirm_cancel", "rationale": "Accepted intent; provided a reviewable draft without claiming an external action"}
            elif any(s in low for s in ("gst", "tax filing", "unrelated")):
                response = {"action": "send", "body": "I can help with your listing and customer message drafts. A qualified adviser can help with tax filing. Would you like a draft for the topic we were discussing?", "cta": "binary_yes_no", "rationale": "Polite scope boundary and return to the existing conversation"}
            else:
                response = {"action": "send", "body": "Thanks for the detail. I can prepare a short draft for your review using the information on your listing. What would you like it to focus on?", "cta": "open_ended", "rationale": "Clarifies an unspecified request without inventing information"}
            if row and response.get("action") == "send" and response.get("body") == row["last_body"]:
                response = {"action": "wait", "wait_seconds": 1800, "rationale": "Avoiding a duplicate message while awaiting new information"}
            if row:
                count = (row["auto_count"] if row else 0) + 1 if auto else 0
                conn.execute("UPDATE conversations SET last_message=?,auto_count=?,ended=?,last_body=? WHERE id=?", (message, count, int(response["action"] == "end"), response.get("body", row["last_body"]), conv_id))
            else:
                conn.execute("INSERT INTO conversations(id,merchant_id,customer_id,last_message,auto_count,ended,last_body) VALUES(?,?,?,?,?,?,?)", (conv_id, mid, cid, message, 1 if auto else 0, int(response["action"] == "end"), response.get("body")))
        self.respond(200, response)


def main():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect():
        pass
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), API)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
