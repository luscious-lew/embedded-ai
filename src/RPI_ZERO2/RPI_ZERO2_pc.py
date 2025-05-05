"""
RPI_ZERO2_stateful.py – Dual‑state UART receiver & transcription pipeline
=======================================================================

*  State A  (GPIO4 **LOW**)
   ─────────────────────────
   • Continuously listen on /dev/serial0 (115 200 bps) for WAV‑transfer packets
     sent by the XIAO‑ESP32S3 (protocol: header line + raw data + ACK/NACK).
   • Save every received WAV into AUDIO_DIR.

*  State B  (GPIO4 **HIGH**)
   ──────────────────────────
   • Pause UART reception.
   • Transcribe **all** WAV files currently in AUDIO_DIR.
   • Prepend each section by a timestamp (recording start time derived from
     filename mtime) and concatenate transcripts into one TXT file.
   • Generate a daily summary via either AssemblyAI **or** OpenAI ChatGPT.
   • E‑mail the TXT file + summary to the configured recipient.
   • Optionally archive or delete input WAVs after success.

GPIO4 HIGH→LOW transition returns to State A.

NOTE:  Requires:
  sudo apt install python3-serial python3-rpi.gpio python3-openai
  pip3 install assemblyai
"""

from __future__ import annotations

import os, time, binascii, serial, wave, datetime as dt, smtplib, glob, shutil, zipfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import RPi.GPIO as GPIO

try:
    import assemblyai, openai  # installed via pip
except ImportError:
    assemblyai = None
    openai = None

# ──────────────────────────────────────────────────────────────────────
#  PATHS & CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BAUD            = 115_200
UART_DEVICE     = "/dev/serial0"
GPIO_STATE_PIN  = 4               # BCM numbering (physical pin 7)

BASE_DIR        = "/home/pi/wav_pipeline"
AUDIO_DIR       = os.path.join(BASE_DIR, "audio")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
OUTPUT_DIR      = os.path.join(BASE_DIR, "outgoing")

SUMMARY_BACKEND = os.getenv("SUMMARY_BACKEND", "assemblyai")  # "assemblyai" or "openai"

# 🔑 API keys pulled from env vars (export in ~/.bashrc or systemd unit)
ASSEMBLYAI_KEY  = os.getenv("ASSEMBLYAI_API_KEY")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
openai_org      = os.getenv("OPENAI_ORG")  # optional

EMAIL_SENDER    = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER  = os.getenv("EMAIL_RECEIVER")
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "465"))

# delete WAVs after emailing?
PURGE_AFTER_SEND = True

# ──────────────────────────────────────────────────────────────────────
#   UTILITY HELPERS
# ──────────────────────────────────────────────────────────────────────

def crc32_update(crc: int, data: bytes) -> int:
    return binascii.crc32(data, crc) & 0xFFFFFFFF


def ensure_dirs():
    for d in (AUDIO_DIR, TRANSCRIPTS_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)


def ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ──────────────────────────────────────────────────────────────────────
#   STATE A – RECEIVE WAVS OVER UART
# ──────────────────────────────────────────────────────────────────────

def uart_receiver(stop_when_high: bool = True):
    """Continuously receive WAV files until GPIO4 goes HIGH (if stop_when_high)."""
    ser = serial.Serial(UART_DEVICE, BAUD, timeout=1)
    print(f"[{ts()}] UART receiver ready on {UART_DEVICE} @ {BAUD}")

    while True:
        # leave loop if pin goes high and caller wants to stop
        if stop_when_high and GPIO.input(GPIO_STATE_PIN) == GPIO.HIGH:
            ser.flush()
            ser.close()
            print(f"[{ts()}] GPIO4 went HIGH → pause receiving")
            return

        header_line = ser.readline()
        if not header_line:
            continue  # timeout ‑ no data
        try:
            header = header_line.decode().strip()
        except UnicodeDecodeError:
            continue  # garbage

        parts = header.split(',')
        if len(parts) < 2:
            print(f"Malformed header: {header}")
            continue

        filename, size_str = parts[:2]
        try:
            file_size = int(size_str)
        except ValueError:
            print("Invalid size in header.")
            continue

        expected_crc = None
        if len(parts) >= 3:
            try:
                expected_crc = int(parts[2], 0)
            except ValueError:
                pass

        path = os.path.join(AUDIO_DIR, os.path.basename(filename))
        print(f"[{ts()}] Receiving {filename} ({file_size} bytes)…")

        with open(path, 'wb') as f:
            received = 0
            crc = 0
            while received < file_size:
                chunk = ser.read(min(file_size - received, 1024))
                if not chunk:
                    continue  # keep trying (timeout)
                f.write(chunk)
                received += len(chunk)
                if expected_crc is not None:
                    crc = crc32_update(crc, chunk)

        ok = received == file_size and (expected_crc is None or crc == expected_crc)
        ser.write(b"ACK\n" if ok else b"NACK\n")
        print(f"[{ts()}] {'OK' if ok else 'FAIL'} – saved to {path}\n")

# ──────────────────────────────────────────────────────────────────────
#   TRANSCRIPTION & SUMMARIZATION (STATE B)
# ──────────────────────────────────────────────────────────────────────

def assemblyai_transcribe(path: str) -> str:
    if not ASSEMBLYAI_KEY:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set")
    import assemblyai as aai
    aai.settings.api_key = ASSEMBLYAI_KEY

    uploader = aai.Transcriber()
    print(f"[AAI] Uploading {os.path.basename(path)}…")
    tx = uploader.transcribe(path)
    if tx.status == aai.TranscriptStatus.error:
        raise RuntimeError(tx.error)
    return tx.text


def openai_transcribe(path: str) -> str:
    """Transcribe via OpenAI Whisper model (requires OPENAI_API_KEY)."""
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    openai.api_key = OPENAI_KEY
    if openai_org:
        openai.organization = openai_org
    with open(path, 'rb') as f:
        resp = openai.audio.transcriptions.create(model="whisper-1", file=f)
    return resp.text  # type: ignore – openai‑python 1.x


def summarize_assemblyai(text: str) -> str:
    import assemblyai as aai
    aai.settings.api_key = ASSEMBLYAI_KEY
    summarizer = aai.Lemur()
    return summarizer.summarize(text, context="Summarize today’s conversations.", answer_format="Paragraph summary. ≤ 200 words").response


def summarize_openai(text: str) -> str:
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    openai.api_key = OPENAI_KEY
    if openai_org:
        openai.organization = openai_org

    resp = openai.chat.completions.create(
        model="gpt-4o-mini",  # cheaper; adjust as desired
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes daily conversation transcripts."},
            {"role": "user", "content": text}
        ],
        max_tokens=200,
        temperature=0.3
    )
    return resp.choices[0].message.content.strip()  # type: ignore


def process_day():
    """Transcribe all WAVs → concat TXT → summarize → e‑mail."""
    ensure_dirs()

    wavs = sorted(glob.glob(os.path.join(AUDIO_DIR, "*.wav")))
    if not wavs:
        print(f"[{ts()}] No WAVs to process.")
        return

    daily_txt = os.path.join(OUTPUT_DIR, f"transcripts_{dt.date.today()}.txt")

    with open(daily_txt, "w", encoding="utf-8") as out:
        for wav in wavs:
            stamp = dt.datetime.fromtimestamp(os.path.getmtime(wav)).strftime("[%Y-%m-%d %H:%M:%S]")
            out.write(f"\n{stamp}\n")
            try:
                if SUMMARY_BACKEND == "assemblyai":
                    text = assemblyai_transcribe(wav)
                else:
                    text = openai_transcribe(wav)
                out.write(text + "\n")
                print(f"Transcribed {os.path.basename(wav)} ({len(text)} chars)")
            except Exception as e:
                print(f"Error transcribing {wav}: {e}")

    # ───── summarize whole day ─────
    with open(daily_txt, "r", encoding="utf-8") as f:
        corpus = f.read()

    try:
        summary = summarize_assemblyai(corpus) if SUMMARY_BACKEND == "assemblyai" else summarize_openai(corpus)
    except Exception as e:
        summary = f"(Summary failed: {e})"

    # Email
    send_email(summary, daily_txt)

    # Cleanup if requested
    if PURGE_AFTER_SEND:
        for wav in wavs:
            os.remove(wav)
        print(f"[{ts()}] Purged {len(wavs)} WAVs after e‑mail.")

# ──────────────────────────────────────────────────────────────────────
#   E‑MAIL SENDER
# ──────────────────────────────────────────────────────────────────────

def send_email(summary: str, attachment_path: str):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        print("Email credentials not fully set – skipping email.")
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = f"Daily Summary – {dt.date.today()}"

    msg.attach(MIMEText(summary, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
        part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_path)}"'
        msg.attach(part)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

    print(f"[{ts()}] Email sent to {EMAIL_RECEIVER} with summary + transcripts.")

# ──────────────────────────────────────────────────────────────────────
#   MAIN LOOP – STATE MACHINE
# ──────────────────────────────────────────────────────────────────────

def main():
    ensure_dirs()

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_STATE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    try:
        print(f"[{ts()}] Pipeline started. GPIO4 LOW = receive; HIGH = process.")
        while True:
            if GPIO.input(GPIO_STATE_PIN) == GPIO.HIGH:
                # Debounce: wait stable 200 ms high
                time.sleep(0.2)
                if GPIO.input(GPIO_STATE_PIN) == GPIO.HIGH:
                    print(f"[{ts()}] === STATE B: processing day ===")
                    process_day()
                    # Wait until pin goes LOW again before resuming receiver
                    while GPIO.input(GPIO_STATE_PIN) == GPIO.HIGH:
                        time.sleep(0.5)
                    print(f"[{ts()}] Pin LOW again – resuming receiver…")
            else:
                # STATE A – receiver (will exit if pin flips high)
                uart_receiver(stop_when_high=True)
            time.sleep(0.1)
    finally:
        GPIO.cleanup()  


if __name__ == "__main__":
    main()
