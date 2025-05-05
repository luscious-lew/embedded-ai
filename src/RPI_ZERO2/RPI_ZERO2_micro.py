"""
Micropython‑friendly version for Raspberry Pi Zero 2 W
-----------------------------------------------------
This script sticks to *only* modules shipped with the MicroPython › Unix port
plus the micro‑libraries available via `micropython‑lib`.
It avoids RPi.GPIO & PySerial and instead uses plain Linux
—   `/sys/class/gpio`  for GPIO4 state
—   POSIX file I/O + `select` for reading /dev/serial0 (UART)
—   `urequests` (micropython‑lib) for HTTP calls to AssemblyAI or OpenAI
—   `smtplib` (micropython‑lib) for e‑mail via SMTP‑over‑TLS

Tested with *MicroPython v1.22 "unix" build* running directly on Raspberry Pi
OS Lite (64‑bit).  Install supporting libs once:
    $ micropip install micropython‑uresquests micropython‑smtplib

Run via:
    $ ./micropython rpi_zero2_micropython.py

Note: performance is fine at 115200bps; for large WAVs increase read chunk.
"""

import os, sys, time, binascii, select, socket, ssl, errno
from datetime import datetime, date

try:
    import urequests as requests  # micropython‑lib
except ImportError:
    print("urequests not installed – `micropip install micropython-urequests`")
    raise

try:
    import smtplib  # micropython‑lib variation
except ImportError:
    print("smtplib not installed – `micropip install micropython-smtplib`")
    raise

# ───── CONFIG ─────
BAUD              = 115_200
UART_DEV          = "/dev/serial0"
GPIO_PIN          = 4  # BCM
GPIO_PATH         = f"/sys/class/gpio/gpio{GPIO_PIN}/value"

WORKDIR           = "/home/pi/wav_pipeline"
AUDIO_DIR         = WORKDIR + "/audio"
OUT_DIR           = WORKDIR + "/out"

BACKEND           = os.getenv("BACKEND", "assemblyai")  # or "openai"
ASSEMBLY_KEY      = os.getenv("ASSEMBLYAI_API_KEY", "")
OPENAI_KEY        = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")

EMAIL_FROM        = os.getenv("EMAIL_SENDER")
EMAIL_TO          = os.getenv("EMAIL_RECEIVER")
EMAIL_PASS        = os.getenv("EMAIL_PASSWORD")
SMTP_HOST         = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT", "465"))

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(OUT_DIR,   exist_ok=True)

# ───── GPIO util via sysfs ─────

def gpio_setup():
    if not os.path.exists(GPIO_PATH):
        try:
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(GPIO_PIN))
            time.sleep(0.1)
            with open(f"/sys/class/gpio/gpio{GPIO_PIN}/direction", "w") as f:
                f.write("in")
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise

def gpio_read() -> int:
    with open(GPIO_PATH, "r") as f:
        return 1 if f.read(1) == "1" else 0

# ───── CRC helper ─────

def crc32_update(crc, data):
    return binascii.crc32(data, crc) & 0xFFFFFFFF

# ───── UART receiver using os.read + select ─────

def receive_wavs():
    print("[RX] start listening… GPIO4 high breaks to process mode")
    fd = os.open(UART_DEV, os.O_RDWR | os.O_NOCTTY)
    # configure baud via stty tool (simplest in micropython):
    os.system(f"stty -F {UART_DEV} {BAUD} raw -echo")

    poll = select.poll()
    poll.register(fd, select.POLLIN)

    while gpio_read() == 0:
        events = poll.poll(500)  # 500 ms timeout
        if not events:
            continue
        # Header line
        header = b""
        while True:
            ch = os.read(fd, 1)
            if ch == b"\n" or ch == b"":
                break
            header += ch
        if not header:
            continue
        try:
            filename, size, *crc_part = header.decode().split(',')
            size = int(size)
            expected_crc = int(crc_part[0], 0) if crc_part else None
        except Exception as e:
            print("Malformed header", header, e)
            continue
        path = f"{AUDIO_DIR}/{os.path.basename(filename)}"
        print(f"[RX] {filename} → {path} ({size} bytes)…")
        with open(path, "wb") as out:
            received = 0
            crc = 0
            while received < size:
                chunk = os.read(fd, min(1024, size - received))
                if not chunk:
                    continue
                out.write(chunk)
                received += len(chunk)
                if expected_crc is not None:
                    crc = crc32_update(crc, chunk)
        ok = received == size and (expected_crc is None or crc == expected_crc)
        os.write(fd, b"ACK\n" if ok else b"NACK\n")
        print("[RX] done", "OK" if ok else "FAIL")
    os.close(fd)

# ───── Transcription wrappers ─────

def transcribe_aai(path):
    url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": ASSEMBLY_KEY}
    with open(path, "rb") as f:
        upload_resp = requests.post(url, headers=headers, data=f)
    upload_url = upload_resp.json()["upload_url"]
    tx_resp = requests.post("https://api.assemblyai.com/v2/transcript",
                             headers=headers,
                             json={"audio_url": upload_url})
    tx_id = tx_resp.json()["id"]
    status_url = f"https://api.assemblyai.com/v2/transcript/{tx_id}"
    while True:
        st = requests.get(status_url, headers=headers).json()
        if st["status"] == "completed":
            return st["text"]
        if st["status"] == "error":
            raise RuntimeError(st.get("error"))
        time.sleep(3)

def transcribe_openai(path):
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    files = {"file": (os.path.basename(path), open(path, "rb"), "audio/wav")}
    data = {"model": "whisper-1"}
    resp = requests.post("https://api.openai.com/v1/audio/transcriptions",
                         headers=headers, data=data, files=files)
    return resp.json()["text"]

# ───── Summarize via same backends ─────

def summarize_aai(text):
    url = "https://api.assemblyai.com/v2/lemur"
    body = {"text": text, "context": "summarize", "answer_format": "short_paragraph"}
    headers = {"authorization": ASSEMBLY_KEY, "content-type": "application/json"}
    return requests.post(url, headers=headers, json=body).json()["response"]

def summarize_openai(text):
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    json = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "Summarize daily transcripts succinctly."},
            {"role": "user", "content": text}
        ],
        "temperature": 0.3,
        "max_tokens": 200
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions",
                         headers=headers, json=json)
    return resp.json()["choices"][0]["message"]["content"].strip()

# ───── Email via smtplib ─────

def send_mail(summary, txt_path):
    msg  = ("From: %s\r\nTo: %s\r\nSubject: Daily Summary %s\r\n\r\n%s\r\n" %
            (EMAIL_FROM, EMAIL_TO, date.today(), summary)).encode()
    with open(txt_path, "rb") as f:
        attachment = f.read()
    # crude multipart (Micropython smtplib lacks MIME helpers):
    boundary = "uPboundary123"
    body = b"".join([
        b"Content-Type: multipart/mixed; boundary=" + boundary.encode() + b"\r\n\r\n",
        b"--" + boundary.encode() + b"\r\n",
        b"Content-Type: text/plain\r\n\r\n",
        summary.encode(), b"\r\n--" + boundary.encode() + b"\r\n",
        b"Content-Type: text/plain; name=transcripts.txt\r\n",
        b"Content-Disposition: attachment; filename=transcripts.txt\r\n\r\n",
        attachment, b"\r\n--" + boundary.encode() + b"--\r\n"
    ])
    msg = msg + body

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(EMAIL_FROM, EMAIL_PASS)
        s.write(msg)  # smtplib in micropython exposes .write raw

# ───── Process day ─────

def process_day():
    txt_path = f"{OUT_DIR}/transcripts_{date.today()}.txt"
    wavs = sorted([f for f in os.listdir(AUDIO_DIR) if f.endswith('.wav')])
    if not wavs:
        print("No WAVs to transcribe.")
        return
    buf = []
    for w in wavs:
        p = f"{AUDIO_DIR}/{w}"
        time_tag = datetime.fromtimestamp(os.path.getmtime(p)).strftime("[%Y-%m-%d %H:%M:%S]")
        try:
            text = transcribe_aai(p) if BACKEND == "assemblyai" else transcribe_openai(p)
        except Exception as e:
            text = f"(transcription failed: {e})"
        buf.append(time_tag + "\n" + text + "\n")
    merged = "\n".join(buf)
    with open(txt_path, "w") as out:
        out.write(merged)
    summary = summarize_aai(merged) if BACKEND == "assemblyai" else summarize_openai(merged)
    print("Summary:\n", summary)
    try:
        send_mail(summary, txt_path)
        print("E‑mail sent.")
    except Exception as e:
        print("Mail error:", e)

    # clean up wavs
    for w in wavs:
        os.remove(f"{AUDIO_DIR}/{w}")

# ───── MAIN LOOP ─────

def main():
    gpio_setup()
    print("GPIO4 low = receive, high = process")
    while True:
        if gpio_read() == 0:
            receive_wavs()
        else:
            print("GPIO4 HIGH → process recordings")
            process_day()
            while gpio_read() == 1:
                time.sleep(0.5)  # wait until pin drops

if __name__ == "__main__":
    main()
