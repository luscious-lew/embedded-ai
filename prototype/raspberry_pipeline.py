import os
import wave
import shutil
import zipfile
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import datetime

# ================= USER CONFIG ===================
AUDIO_DIR = "/home/pi/audio_files"  # where audio files are stored
TRANSCRIPTS_DIR = "/home/pi/transcripts"
ZIP_FILE = "/home/pi/daily_transcripts.zip"

ASSEMBLYAI_API_KEY = "YOUR_ASSEMBLYAI_API_KEY"
EMAIL_SENDER = "your_email@gmail.com"
EMAIL_PASSWORD = "your_email_password"  # app password if using Gmail
EMAIL_RECEIVER = "recipient_email@gmail.com"

MINIMUM_SPEECH_SECONDS = 10  # X seconds
SILENCE_THRESHOLD_DB = -40  # optional: for future smarter filtering
# ==================================================

# Helper to send a file to AssemblyAI
def transcribe_and_summarize(file_path):
    headers = {'authorization': ASSEMBLYAI_API_KEY}
    upload_url = 'https://api.assemblyai.com/v2/upload'

    # Upload file
    with open(file_path, 'rb') as f:
        response = requests.post(upload_url, headers=headers, files={'file': f})
    audio_url = response.json()['upload_url']

    # Request transcription + summary
    transcript_request = {
        'audio_url': audio_url,
        'summarization': True,
        'summary_model': 'informative',  # other option: 'conversational'
        'summary_type': 'bullets'  # or 'paragraph'
    }
    endpoint = "https://api.assemblyai.com/v2/transcript"
    transcript_response = requests.post(endpoint, json=transcript_request, headers=headers)
    transcript_id = transcript_response.json()['id']

    # Poll for completion
    polling_endpoint = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    while True:
        polling_response = requests.get(polling_endpoint, headers=headers)
        status = polling_response.json()['status']
        if status == 'completed':
            return polling_response.json()
        elif status == 'failed':
            raise Exception("Transcription failed!")
    
# Helper to check if WAV file is long enough
def is_valid_audio(file_path):
    with wave.open(file_path, 'rb') as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        duration = frames / float(rate)
        return duration >= MINIMUM_SPEECH_SECONDS

# Helper to send daily email
def send_email(summary_text, zip_path):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = f"Daily Conversation Summary - {datetime.date.today().strftime('%Y-%m-%d')}"

    msg.attach(MIMEText(summary_text, 'plain'))

    with open(zip_path, 'rb') as f:
        part = MIMEApplication(f.read(), Name=os.path.basename(zip_path))
        part['Content-Disposition'] = f'attachment; filename="{os.path.basename(zip_path)}"'
        msg.attach(part)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

# Main script
def main():
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    all_summaries = []
    transcript_files = []

    # Step 1: Scan and filter files
    for filename in os.listdir(AUDIO_DIR):
        if filename.endswith(".wav"):
            file_path = os.path.join(AUDIO_DIR, filename)
            if is_valid_audio(file_path):
                try:
                    result = transcribe_and_summarize(file_path)
                    # Save full transcript
                    transcript_path = os.path.join(TRANSCRIPTS_DIR, filename.replace('.wav', '.txt'))
                    with open(transcript_path, 'w') as f:
                        f.write(result.get('text', ''))
                    transcript_files.append(transcript_path)
                    # Collect summary
                    all_summaries.append(result.get('summary', ''))
                except Exception as e:
                    print(f"Error processing {filename}: {e}")
            else:
                # File too short, delete
                os.remove(file_path)

    # Step 2: Zip transcripts
    with zipfile.ZipFile(ZIP_FILE, 'w') as zipf:
        for file in transcript_files:
            zipf.write(file, arcname=os.path.basename(file))

    # Step 3: Send daily email
    summary_text = "\n\n".join(all_summaries) or "No significant conversations captured today."
    send_email(summary_text, ZIP_FILE)

    # Step 4: Cleanup
    shutil.rmtree(TRANSCRIPTS_DIR)
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    os.remove(ZIP_FILE)

if __name__ == "__main__":
    main()