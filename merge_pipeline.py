"""
Task 1 — Merge Pipeline
Ingests 3 CSV files with messy, overlapping data into a single clean SQLite database.
Matching strategy: email (case-insensitive) as primary key, phone (normalized) as fallback.
"""

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "consultbae.db"
DATA_DIR = Path(__file__).parent / "data"


# ── Normalizers ──────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    """Strip +91, leading 0, hyphens, spaces → bare 10-digit number."""
    if not raw:
        return ""
    digits = re.sub(r"[^0-9]", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits


def normalize_email(raw: str) -> str:
    return raw.strip().lower() if raw else ""


def normalize_city(raw: str) -> str:
    """Collapse Gurgaon/Gurugram, Delhi variants, case issues."""
    if not raw:
        return ""
    city = raw.strip().title()
    mapping = {
        "Gurgaon": "Gurugram",
        "Delhi Ncr": "Delhi NCR",
        "New Delhi": "New Delhi",
        "Bangalore": "Bengaluru",
    }
    return mapping.get(city, city)


def normalize_name(raw: str) -> str:
    """Title-case and strip whitespace."""
    if not raw:
        return ""
    return raw.strip().title()


def normalize_verified(raw: str) -> bool | None:
    if not raw:
        return None
    return raw.strip().lower() in ("y", "yes", "true", "1")


def parse_date(raw: str) -> str | None:
    """Try multiple date formats, return ISO YYYY-MM-DD or None."""
    if not raw:
        return None
    raw = raw.strip()
    formats = [
        "%d-%m-%Y",    # 24-07-2026
        "%Y-%m-%d",    # 2026-08-08
        "%m/%d/%Y",    # 07/13/2026
        "%d %b %Y",    # 7 Jul 2026
        "%d %B %Y",    # 7 July 2026
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # return as-is if nothing matched


def parse_ctc(raw: str) -> float | None:
    """
    CTC appears as either:
      - raw annual number: 417964
      - lakhs shorthand: 4.2 (meaning ₹4.2 LPA)
    Heuristic: if the number is < 100, it's in lakhs → multiply by 100000.
    """
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val < 100:  # clearly in lakhs
        return val * 100000
    return val


def parse_rate(raw: str) -> dict:
    """Parse gig worker rate like '1415/hr' or '15k/month'."""
    if not raw:
        return {"rate_value": None, "rate_unit": None}
    raw = raw.strip().lower()
    m = re.match(r"(\d+\.?\d*)(k?)\s*/\s*(hr|hour|month)", raw)
    if m:
        val = float(m.group(1))
        if m.group(2) == "k":
            val *= 1000
        return {"rate_value": val, "rate_unit": m.group(3).replace("hour", "hr")}
    return {"rate_value": None, "rate_unit": None}


def normalize_status(raw: str) -> str:
    if not raw:
        return ""
    return raw.strip().lower()  # active / inactive / paused


def normalize_skills(raw: str) -> str:
    """Lowercase, sort, dedupe skill list."""
    if not raw:
        return ""
    skills = sorted(set(s.strip().lower() for s in raw.split(",") if s.strip()))
    return ", ".join(skills)


# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    email           TEXT,
    phone           TEXT,
    city            TEXT,
    experience_yrs  REAL,
    current_ctc     REAL,
    applied_date    TEXT,
    skills          TEXT,
    gig_rate_value  REAL,
    gig_rate_unit   TEXT,
    gig_status      TEXT,
    verified        INTEGER,
    projects_done   INTEGER,
    source_files    TEXT,
    data_notes      TEXT
);

CREATE TABLE IF NOT EXISTS audio_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_name     TEXT NOT NULL,
    phone           TEXT NOT NULL,
    filename        TEXT NOT NULL,
    filepath        TEXT NOT NULL,
    duration_sec    REAL,
    sample_rate_khz REAL,
    bitrate_kbps    REAL,
    loudness_db     REAL,
    noise_quality   TEXT,
    submitted_at    TEXT DEFAULT (datetime('now'))
);
"""


# ── Ingestion ────────────────────────────────────────────────────────────────

def read_csv_safe(path: Path) -> list[dict]:
    """Read CSV, skip blank/header-repeat rows."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_fields = reader.fieldnames
        for row in reader:
            # Skip fully blank rows
            if all(not v.strip() for v in row.values()):
                continue
            # Skip repeated header rows (source3 has one mid-file)
            first_val = list(row.values())[0].strip()
            if first_val == header_fields[0]:
                continue
            rows.append(row)
    return rows


def detect_shifted_row_source2(row: dict) -> dict | None:
    """
    Source 2 has a shifted row where skill_tags leaked into email_id column.
    Detect and re-align.
    """
    email = row.get("email_id", "").strip()
    # If 'email_id' field doesn't look like an email, row is shifted
    if email and "@" not in email and "," in email:
        # Columns shifted left: skill_tags→email_id, email_id→worker_name, etc.
        return {
            "email_id": row.get("worker_name", ""),
            "worker_name": row.get("rate", ""),
            "rate": row.get("location", ""),
            "location": row.get("status", ""),
            "status": row.get("skill_tags", ""),
            "skill_tags": email,  # the original email_id was actually skills
        }
    return None


def ingest_source1(rows: list[dict]) -> dict:
    """Naukri applicants → dict keyed by normalized email."""
    people = {}
    seen_emails = {}

    for row in rows:
        email = normalize_email(row.get("Email", ""))
        phone = normalize_phone(row.get("Phone", ""))
        name = normalize_name(row.get("Full Name", ""))
        notes = []

        if not email:
            notes.append("MISSING_EMAIL")
            continue

        # Detect abbreviated name (e.g., "R. Verma" vs "Rohit Verma")
        is_abbrev = bool(re.match(r"^[A-Z]\.\s", row.get("Full Name", "").strip()))
        if is_abbrev:
            notes.append(f"ABBREVIATED_NAME_ORIGINAL: {row.get('Full Name', '').strip()}")

        # Detect duplicate within same file
        if email in seen_emails:
            notes.append("DUPLICATE_WITHIN_SOURCE1")
            # Keep the entry but note it; merge skills
            existing = people[email]
            old_skills = set(existing["skills"].split(", ")) if existing["skills"] else set()
            new_skills = set(normalize_skills(row.get("Skills", "")).split(", "))
            merged = ", ".join(sorted(old_skills | new_skills - {""}))
            existing["skills"] = merged
            existing["data_notes"] = "; ".join(
                [existing.get("data_notes", ""), "DUPLICATE_ROW_IN_SOURCE1"]
            ).strip("; ")
            # Prefer the non-abbreviated full name
            if is_abbrev and existing["name"]:
                pass  # keep existing
            elif not is_abbrev and name:
                existing["name"] = name
            continue

        seen_emails[email] = True
        ctc = parse_ctc(row.get("Current CTC", ""))
        ctc_note = ""
        if ctc and ctc < 10000000:  # flag if CTC was in lakhs shorthand
            raw_ctc = row.get("Current CTC", "").strip()
            try:
                if float(raw_ctc) < 100:
                    ctc_note = f"CTC_INTERPRETED_AS_LAKHS(original={raw_ctc})"
            except ValueError:
                pass

        if ctc_note:
            notes.append(ctc_note)

        # Alt-email detection: "alt.nikhil.chopra70@..."
        if email.startswith("alt."):
            notes.append(f"ALT_EMAIL_PREFIX: original={email}")

        people[email] = {
            "name": name,
            "email": email,
            "phone": phone,
            "city": normalize_city(row.get("City", "")),
            "experience_yrs": float(row["Experience (Years)"]) if row.get("Experience (Years)") else None,
            "current_ctc": ctc,
            "applied_date": parse_date(row.get("Applied Date", "")),
            "skills": normalize_skills(row.get("Skills", "")),
            "gig_rate_value": None,
            "gig_rate_unit": None,
            "gig_status": None,
            "verified": None,
            "projects_done": None,
            "source_files": "source1",
            "data_notes": "; ".join(notes) if notes else "",
        }
    return people


def ingest_source2(rows: list[dict], people: dict) -> dict:
    """Gig workers → merge into people dict by email."""
    for row in rows:
        # Check for shifted row
        fixed = detect_shifted_row_source2(row)
        notes = []
        if fixed:
            notes.append("SHIFTED_ROW_REALIGNED")
            row = fixed

        email = normalize_email(row.get("email_id", ""))
        if not email:
            continue

        name = normalize_name(row.get("worker_name", ""))
        rate = parse_rate(row.get("rate", ""))
        status = normalize_status(row.get("status", ""))
        skills = normalize_skills(row.get("skill_tags", ""))
        city = normalize_city(row.get("location", ""))

        if email in people:
            # Merge into existing
            p = people[email]
            p["source_files"] += ", source2"
            if rate["rate_value"]:
                p["gig_rate_value"] = rate["rate_value"]
                p["gig_rate_unit"] = rate["rate_unit"]
            if status:
                p["gig_status"] = status
            # Merge skills
            old = set(p["skills"].split(", ")) if p["skills"] else set()
            new = set(skills.split(", ")) if skills else set()
            p["skills"] = ", ".join(sorted(old | new - {""}))
            if notes:
                p["data_notes"] = "; ".join(filter(None, [p.get("data_notes", ""), *notes]))
        else:
            people[email] = {
                "name": name,
                "email": email,
                "phone": "",
                "city": city,
                "experience_yrs": None,
                "current_ctc": None,
                "applied_date": None,
                "skills": skills,
                "gig_rate_value": rate["rate_value"],
                "gig_rate_unit": rate["rate_unit"],
                "gig_status": status,
                "verified": None,
                "projects_done": None,
                "source_files": "source2",
                "data_notes": "; ".join(notes) if notes else "",
            }
    return people


def ingest_source3(rows: list[dict], people: dict) -> dict:
    """
    CBNexus contacts — NO email column, only phone.
    Match by normalized phone number against existing records.
    """
    # Build phone→email index from existing people
    phone_index: dict[str, str] = {}
    for email, p in people.items():
        ph = p["phone"]
        if ph:
            phone_index[ph] = email

    for row in rows:
        phone = normalize_phone(row.get("Phone Number", ""))
        name = normalize_name(row.get("Name", ""))
        city = normalize_city(row.get("City", ""))
        verified = normalize_verified(row.get("Verified", ""))
        try:
            projects = int(row.get("Projects Completed", 0))
        except (ValueError, TypeError):
            projects = None

        notes = []

        if phone and phone in phone_index:
            # Match found — merge
            email = phone_index[phone]
            p = people[email]
            p["source_files"] += ", source3"
            p["verified"] = 1 if verified else 0
            p["projects_done"] = projects
            # Use the CBNexus city if missing
            if not p["city"] and city:
                p["city"] = city
            if notes:
                p["data_notes"] = "; ".join(filter(None, [p.get("data_notes", ""), *notes]))
        elif phone:
            # No email match — create phone-only record
            people[f"phone_{phone}"] = {
                "name": name,
                "email": "",
                "phone": phone,
                "city": city,
                "experience_yrs": None,
                "current_ctc": None,
                "applied_date": None,
                "skills": "",
                "gig_rate_value": None,
                "gig_rate_unit": None,
                "gig_status": None,
                "verified": 1 if verified else 0,
                "projects_done": projects,
                "source_files": "source3",
                "data_notes": "NO_EMAIL_PHONE_ONLY_RECORD",
            }
    return people


# ── Main ─────────────────────────────────────────────────────────────────────

def build_db():
    # Remove old DB
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # Read all sources
    s1 = read_csv_safe(DATA_DIR / "source1_naukri_applicants.csv")
    s2 = read_csv_safe(DATA_DIR / "source2_gig_workers.csv")
    s3 = read_csv_safe(DATA_DIR / "source3_cbnexus_contacts.csv")

    print(f"Source 1 rows read: {len(s1)}")
    print(f"Source 2 rows read: {len(s2)}")
    print(f"Source 3 rows read: {len(s3)}")

    # Ingest in order
    people = ingest_source1(s1)
    print(f"After source 1: {len(people)} unique people")

    people = ingest_source2(s2, people)
    print(f"After source 2: {len(people)} unique people")

    people = ingest_source3(s3, people)
    print(f"After source 3: {len(people)} unique people")

    # Insert into DB
    for key, p in people.items():
        conn.execute(
            """INSERT INTO persons
               (name, email, phone, city, experience_yrs, current_ctc,
                applied_date, skills, gig_rate_value, gig_rate_unit,
                gig_status, verified, projects_done, source_files, data_notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["name"], p["email"], p["phone"], p["city"],
                p["experience_yrs"], p["current_ctc"], p["applied_date"],
                p["skills"], p["gig_rate_value"], p["gig_rate_unit"],
                p["gig_status"], p["verified"], p["projects_done"],
                p["source_files"], p["data_notes"],
            ),
        )

    conn.commit()

    # Summary
    cur = conn.execute("SELECT COUNT(*) FROM persons")
    total = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM persons WHERE source_files LIKE '%source1%' AND source_files LIKE '%source2%'")
    overlap_12 = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM persons WHERE source_files LIKE '%source3%'")
    from_s3 = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM persons WHERE data_notes != ''")
    flagged = cur.fetchone()[0]

    print(f"\n=== MERGE SUMMARY ===")
    print(f"Total unique persons: {total}")
    print(f"Matched across source1 & source2: {overlap_12}")
    print(f"Matched/added from source3: {from_s3}")
    print(f"Records with data quality notes: {flagged}")

    conn.close()
    print(f"\nDatabase written to: {DB_PATH}")


if __name__ == "__main__":
    build_db()
