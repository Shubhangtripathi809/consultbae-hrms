# ConsultBae — AI Automation Assignment

Merge 3 messy CSV data sources into one clean database, automate with n8n, and build a mini audio collection app.

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/consultbae-hrms.git
cd consultbae-hrms

# 2. Install dependencies
pip install flask pydub

# 3. Install ffmpeg (for audio metadata extraction)
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg
# Windows: download from https://ffmpeg.org/download.html

# 4. Run the merge pipeline (creates consultbae.db)
python merge_pipeline.py

# 5. Start the audio collection app
python app.py
# Open http://localhost:5000
```

---

## Task 1 — Merge Pipeline

**File:** `merge_pipeline.py`

**Strategy:**
- **Primary match key:** Email (case-insensitive, trimmed). Source 1 and 2 both have email fields.
- **Fallback match key:** Phone (normalized to 10-digit). Source 3 (CBNexus) has NO email — only phone. So we build a phone→email index from sources 1+2 and match source 3 records against it.
- **Same person = same email OR same phone number** across files.

**Normalizations applied:**
- Phone: strip `+91`, `91`, leading `0`, hyphens, spaces → bare 10-digit
- Email: lowercase, trim
- City: title-case, map Gurgaon→Gurugram, Bangalore→Bengaluru, Delhi NCR stays Delhi NCR
- Names: title-case
- CTC: detect lakhs shorthand (values < 100 are interpreted as ₹X LPA × 100,000)
- Dates: parse 4 different formats → ISO YYYY-MM-DD
- Gig rates: parse `1415/hr` and `15k/month` into value + unit
- Skills: lowercase, sort, dedupe
- Status/Verified: normalize to consistent lowercase or boolean

---

## Task 2 — n8n Automation

**File:** `n8n_flows/duplicate_check_flow.json`

**Flow:** CSV Duplicate Checker via Webhook
1. **Webhook Trigger** — receives a POST with a CSV file
2. **Parse CSV** — extracts rows from the uploaded CSV
3. **Normalize Fields** — lowercases emails, strips phone prefixes (same logic as merge pipeline)
4. **SQLite Lookup** — queries the `persons` table for matching email OR phone
5. **If Duplicate?** — branches on whether a match was found
6. **Send Duplicate Alert** — emails the team with details of the duplicate
7. **Respond to Webhook** — returns JSON result

**To use:** Import the JSON into n8n (Settings → Import Workflow), configure the SQLite credential to point at `consultbae.db`, and POST a CSV to the webhook URL.

---

## Task 3 — Mini Audio Collection App

**Files:** `app.py`, `templates/index.html`, `templates/submissions.html`

**Stack:** Flask + ffprobe/ffmpeg

**Features:**
- Submit page: enter name + phone, record audio in browser (MediaRecorder API) OR upload a file
- On submit: audio saved to `audio_uploads/`, metadata extracted automatically
- Extracted metadata: duration (sec), sample rate (kHz), bitrate (kbps), loudness (LUFS via EBU R128), noise quality estimate (based on loudness range)
- Submissions page: table with play button, all metadata columns, color-coded noise badges

**Audio analysis approach:**
- `ffprobe` for duration, sample rate, bitrate (JSON output parsed)
- `ffmpeg -af ebur128` for integrated loudness (LUFS) and loudness range (LRA)
- Noise quality estimated from LRA: <5 LU = clean, 5-10 = moderate, >10 = noisy

---

## Task 4 — Data Issues Report

### Source 1: `source1_naukri_applicants.csv`

| # | Issue | Example | Action Taken |
|---|-------|---------|-------------|
| 1 | **Phone format inconsistent** | `+919000000254`, `9000000237`, `09000000287` | Stripped `+91`, `91`, leading `0` → bare 10-digit |
| 2 | **City casing inconsistent** | `GURGAON`, `gurugram `, `pune`, `NOIDA`, `Bengaluru` | Title-cased all; trailing spaces stripped |
| 3 | **Gurgaon vs Gurugram** — same city, different names | `GURGAON` and `gurugram` | Mapped all Gurgaon → Gurugram |
| 4 | **Bangalore vs Bengaluru** | `bangalore`, `Bengaluru` | Mapped all Bangalore → Bengaluru |
| 5 | **Delhi / New Delhi / Delhi NCR** — 3 variants | Various | Kept Delhi and New Delhi distinct; Delhi NCR standardized |
| 6 | **Date formats — 4 different formats** | `24-07-2026`, `2026-08-08`, `07/13/2026`, `7 Jul 2026` | Parsed all to ISO `YYYY-MM-DD` |
| 7 | **CTC in mixed units** — some raw annual, some in lakhs | `417964` vs `4.2` vs `8.3` | If value < 100, interpreted as lakhs (×100,000). Logged in `data_notes`. |
| 8 | **Duplicate person within file** | "R. Verma" and "Rohit Verma" — same email `rohit.verma13@mailtest.example.org`, same phone `9000000294` | Merged into one record; kept full name, noted abbreviated variant |
| 9 | **Abbreviated name** | `R. Verma` instead of `Rohit Verma` | Detected `X.` pattern; preferred full name |
| 10 | **Duplicate: Nikhil Chopra** | Two rows: email `nikhil.chopra70@example.com` and `alt.nikhil.chopra70@example.com` — same phone `09000000103` | Both kept as separate email records; alt-email prefix flagged |
| 11 | **Trailing spaces in city** | `Noida `, `gurugram ` | Stripped |
| 12 | **Skills as quoted comma-separated string** | `"n8n, LangChain, REST APIs"` | Parsed, lowercased, sorted, deduped |

### Source 2: `source2_gig_workers.csv`

| # | Issue | Example | Action Taken |
|---|-------|---------|-------------|
| 1 | **Email casing inconsistent** | `ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` vs `isha.chopra95@...` | Lowercased all emails |
| 2 | **Rate format inconsistent** | `1415/hr` vs `15k/month` vs `72k/month` | Parsed into numeric value + unit (`hr`/`month`); `k` multiplied by 1000 |
| 3 | **Status casing** | `active`, `Active`, `ACTIVE`, `paused`, `Inactive` | Lowercased all |
| 4 | **Completely blank row** | Row of just commas: `,,,,,` | Skipped during ingestion |
| 5 | **Shifted/misaligned row (CRITICAL)** | Row: `"react, javascript, mysql", ISHA.CHOPRA95@..., Isha Chopra, 1406/hr, Pune, active` — skills leaked into email_id column, all fields shifted left | Detected by checking if `email_id` contains `@`; re-aligned columns programmatically |
| 6 | **Duplicate Isha Chopra** | Appears correctly once + once as the shifted row | After fixing the shift, same email → merged |
| 7 | **Two different Deepak Nairs** | `deepak.nair44@example.com` (Bengaluru) and `deepak.nair57@example.in` (New Delhi) | Different emails → treated as separate people (likely different people despite same name) |
| 8 | **Duplicate block at end of file** | Rows 1-8 appear to repeat near the end | Handled by email-based dedup during merge |

### Source 3: `source3_cbnexus_contacts.csv`

| # | Issue | Example | Action Taken |
|---|-------|---------|-------------|
| 1 | **No email column** | Only Name, Phone, City, Verified, Projects | Matched to existing records via normalized phone number |
| 2 | **Duplicate header row mid-file** | Row 15 is `Name,Phone Number,City,Verified,Projects Completed` again | Detected and skipped |
| 3 | **Phone format inconsistent** | `9000000268`, `919000000231`, `+91-9000000131` | Same normalization as source 1 |
| 4 | **Name casing** | `RITU SHARMA`, `KARAN BHATIA` vs `Rohit Nair` | Title-cased all |
| 5 | **Verified field inconsistent** | `Y`, `yes`, `No`, `N`, `Yes` | Normalized to boolean (true/false) |
| 6 | **City inconsistencies** | Same as other sources — `PUNE`, `pune`, `NOIDA` | Same normalization |
| 7 | **Two Arjun Mehtas** | Phone `9000000131` and `9000000272` — different phone numbers, same name | First matches `arjun.mehta9@example.in` (source 1); second has no email match — kept as separate phone-only record |
| 8 | **Manish Bhatia phone mismatch** | Source 3 phone `9000000161` vs source 2 email `manish.bhatia3@example.com` (no phone in s1/s2) | No phone index match → created as phone-only record. Potential same person but couldn't confirm without email. |
| 9 | **Trailing spaces** | `Noida ` | Stripped |

### Cross-Source Issues

| # | Issue | Action |
|---|-------|--------|
| 1 | **No universal ID** across all 3 files | Used email as primary key (sources 1+2), phone as fallback (source 3) |
| 2 | **Same person, different cities** | e.g., Tanvi Gupta: Bengaluru (s1) vs bangalore (s2) → both normalize to Bengaluru. Priya Singh: GURGAON (s1) vs gurugram (s3) → both Gurugram |
| 3 | **Skills casing mismatch** | Source 1: `LangChain, REST APIs` vs source 2: `langchain, rest apis` → all lowercased and merged |

---

## Task 5 — Stretch: Scaling to 5,000 Workers

**Scenario:** Launch the audio app to 5,000 gig workers over a single weekend.

### What breaks first

1. **Storage blows up.** At ~1MB per 30-second recording × 5,000 uploads = ~5 GB minimum. If workers re-record or submit multiple takes, easily 15-20 GB. A free-tier server (Render/Railway) has 512 MB–1 GB disk. Files must go to object storage (S3/GCS/R2).

2. **SQLite can't handle concurrent writes.** SQLite locks the whole database on every write. With 100+ simultaneous submissions, writes queue up and requests start timing out. Need to move to Postgres (or at minimum WAL mode + connection pooling).

3. **ffmpeg analysis blocks the request.** Extracting loudness via EBU R128 can take 2-5 seconds per file. At 100 concurrent uploads, the server is blocked running 100 ffmpeg processes. Need to push analysis to a background queue (Celery/RQ/BullMQ).

4. **No upload validation.** Someone submits a 500 MB WAV file or a non-audio file. Need: file size limit (e.g., 10 MB), MIME type check, max duration check.

5. **Duplicate submissions.** Workers accidentally submit twice, or refresh the page. Need: idempotency key (hash of phone + timestamp), or a "you already submitted" check.

6. **No authentication.** Anyone with the URL can submit. Workers could spoof names/phones. Need: OTP verification or a unique submission link per worker.

### What I'd change before launch

- **File storage → S3/R2** with presigned upload URLs (client uploads directly to S3, skipping the server entirely).
- **Database → Postgres** on a managed service (Supabase/Neon free tier works).
- **Audio analysis → async queue.** On submit: save file + create a "pending" DB record. A background worker picks it up, runs ffprobe/ffmpeg, updates the record. User sees "Processing..." until done.
- **Rate limiting** per phone number (max 3 submissions/hour).
- **Health check endpoint** + basic monitoring (uptime, queue depth, disk usage).
- **CDN for playback** — serve audio files through Cloudflare or CloudFront, not the app server.
- **Cost estimate:** S3 storage ~$0.12/GB/month. 20 GB = $2.40/month. Compute: a $5/month VPS handles the API. Main cost driver is egress if people replay audio a lot.

---

## Stuck Log

### 1. CTC field made no sense at first

Source 1 had CTC values like 417964 and then suddenly 4.2. I thought the data was broken. After staring at it and Googling "Naukri CTC format", it clicked — 4.2 means 4.2 lakhs. Indian job portals do this. So I made a simple rule: under 100 = lakhs, multiply by 1 lakh. Not perfect, but works for tech salaries. Logged every conversion for safety.

### 2. One row in source 2 was completely messed up

Spent the most time on this. One row had skills like "react, javascript, mysql" sitting in the email column — everything was shifted left. First thought was to just skip it, but didn't want to lose a real person's data. So I added a check — if email doesn't have @, the row is probably shifted. Re-aligned it programmatically. Verified by comparing with the correct entry above it.

### 3. Source 3 had no email column

This scared me because my whole matching was email-based. Then I realized phone numbers work too — they're unique per person. Built a phone-to-email reverse map from sources 1 and 2, matched source 3 against it. Rejected name matching after seeing two "Arjun Mehta" with different phones — clearly different people.

---

## Repo Structure

```
consultbae-hrms/
├── README.md                  ← You are here
├── merge_pipeline.py          ← Task 1: ingestion + merge script
├── app.py                     ← Task 3: Flask audio collection app
├── consultbae.db              ← SQLite database (generated)
├── requirements.txt
├── data/
│   ├── source1_naukri_applicants.csv
│   ├── source2_gig_workers.csv
│   └── source3_cbnexus_contacts.csv
├── templates/
│   ├── index.html             ← Audio submission page
│   └── submissions.html       ← Submissions list + playback
├── audio_uploads/             ← Stored audio files
└── n8n_flows/
    └── duplicate_check_flow.json  ← Task 2: n8n workflow export
```
