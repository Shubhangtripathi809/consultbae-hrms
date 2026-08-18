"""
Task 3 — Mini Audio Collection App
Flask app where users can record/upload audio, which gets stored with
extracted metadata (duration, sample rate, bitrate, loudness, noise quality).
"""

import json
import os
import sqlite3
import subprocess
import tempfile
import base64
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "consultbae.db"
UPLOAD_DIR = BASE_DIR / "audio_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def extract_audio_metadata(filepath: str) -> dict:
    """
    Use ffprobe to extract duration, sample rate, bitrate.
    Use ffmpeg loudness filter (ebur128) for loudness.
    Estimate noise quality from loudness range.
    """
    meta = {
        "duration_sec": None,
        "sample_rate_khz": None,
        "bitrate_kbps": None,
        "loudness_db": None,
        "noise_quality": None,
    }

    try:
        # ffprobe for basic info
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", filepath
            ],
            capture_output=True, text=True, timeout=30
        )
        info = json.loads(result.stdout)

        # Duration
        fmt = info.get("format", {})
        meta["duration_sec"] = round(float(fmt.get("duration", 0)), 2)

        # Bitrate (format-level)
        br = fmt.get("bit_rate")
        if br:
            meta["bitrate_kbps"] = round(int(br) / 1000, 1)

        # Sample rate from first audio stream
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                sr = stream.get("sample_rate")
                if sr:
                    meta["sample_rate_khz"] = round(int(sr) / 1000, 1)
                # Fallback bitrate from stream
                if not meta["bitrate_kbps"]:
                    sbr = stream.get("bit_rate")
                    if sbr:
                        meta["bitrate_kbps"] = round(int(sbr) / 1000, 1)
                break

    except Exception as e:
        print(f"ffprobe error: {e}")

    try:
        # Loudness via ebur128 filter
        result = subprocess.run(
            [
                "ffmpeg", "-i", filepath, "-af",
                "ebur128=framelog=verbose", "-f", "null", "-"
            ],
            capture_output=True, text=True, timeout=60
        )
        # Parse integrated loudness from stderr
        for line in result.stderr.split("\n"):
            if "I:" in line and "LUFS" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "I:":
                        try:
                            meta["loudness_db"] = float(parts[i + 1])
                        except (IndexError, ValueError):
                            pass
            if "LRA:" in line and "LU" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "LRA:":
                        try:
                            lra = float(parts[i + 1])
                            # Rough noise quality estimate based on loudness range
                            if lra < 5:
                                meta["noise_quality"] = "Low noise (clean)"
                            elif lra < 10:
                                meta["noise_quality"] = "Moderate noise"
                            else:
                                meta["noise_quality"] = "High noise (noisy)"
                        except (IndexError, ValueError):
                            pass
    except Exception as e:
        print(f"loudness analysis error: {e}")

    if not meta["noise_quality"]:
        meta["noise_quality"] = "Unknown"

    return meta


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submissions")
def submissions_page():
    return render_template("submissions.html")


@app.route("/api/submit", methods=["POST"])
def submit_audio():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()

    if not name or not phone:
        return jsonify({"error": "Name and phone are required"}), 400

    audio_file = request.files.get("audio")
    audio_blob = request.form.get("audio_blob")  # base64 from browser recording

    if not audio_file and not audio_blob:
        return jsonify({"error": "No audio provided"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c for c in name if c.isalnum() or c == "_")

    if audio_file:
        ext = Path(audio_file.filename).suffix or ".webm"
        filename = f"{safe_name}_{timestamp}{ext}"
        filepath = UPLOAD_DIR / filename
        audio_file.save(str(filepath))
    else:
        # Browser recorded blob (base64 webm)
        filename = f"{safe_name}_{timestamp}.webm"
        filepath = UPLOAD_DIR / filename
        audio_data = base64.b64decode(audio_blob)
        with open(filepath, "wb") as f:
            f.write(audio_data)

    # Extract metadata
    meta = extract_audio_metadata(str(filepath))

    # Insert into DB
    conn = get_db()
    conn.execute(
        """INSERT INTO audio_submissions
           (person_name, phone, filename, filepath,
            duration_sec, sample_rate_khz, bitrate_kbps, loudness_db, noise_quality)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, phone, filename, str(filepath),
            meta["duration_sec"], meta["sample_rate_khz"],
            meta["bitrate_kbps"], meta["loudness_db"], meta["noise_quality"]
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "filename": filename, "metadata": meta})


@app.route("/api/submissions")
def list_submissions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM audio_submissions ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
