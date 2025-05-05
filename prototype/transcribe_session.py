import os
import glob
import time
import assemblyai as aai
from datetime import datetime

# -------------------------------
# 🔧 CONFIG
# -------------------------------

# Load API key from environment variable
aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
if not aai.settings.api_key:
    raise EnvironmentError("❌ Please set your ASSEMBLYAI_API_KEY environment variable.")

# Use bullet summary format
SUMMARY_CONTEXT = "Summarize the following conversation."
SUMMARY_FORMAT = "A concise summary in bullet points."

# -------------------------------
# 🔍 Find the most recent session folder
# -------------------------------

def get_latest_session_folder():
    base_dir = os.path.join(os.getcwd(), "recordings")
    sessions = sorted(
        glob.glob(os.path.join(base_dir, "session_*")),
        key=os.path.getmtime,
        reverse=True
    )
    if not sessions:
        raise FileNotFoundError("❌ No session folders found in /recordings.")
    return sessions[0]

# -------------------------------
# 🧠 Transcribe + Summarize
# -------------------------------

def process_audio_file(filepath):
    base, _ = os.path.splitext(filepath)
    transcript_file = base + "_transcript.txt"
    summary_file = base + "_summary.txt"

    # Skip files already processed
    if os.path.exists(transcript_file) and os.path.exists(summary_file):
        print(f"⏭ Already processed: {os.path.basename(filepath)}")
        return

    try:
        print(f"📤 Uploading: {os.path.basename(filepath)}")
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(filepath)

        if transcript.status == aai.TranscriptStatus.error:
            print(f"❌ Transcription failed: {transcript.error}")
            return

        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(transcript.text)
        print(f"📝 Transcript saved: {transcript_file}")

        summary = transcript.lemur.summarize(
        context=SUMMARY_CONTEXT,
        answer_format=SUMMARY_FORMAT,
        final_model="anthropic/claude-3-haiku"
        )


        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary.response)
        print(f"✅ Summary saved: {summary_file}\n")

    except Exception as e:
        print(f"❌ Error processing {os.path.basename(filepath)}: {e}")

# -------------------------------
# 🚀 Main
# -------------------------------

if __name__ == "__main__":
    print("🔎 Scanning for latest recording session...")
    session_dir = get_latest_session_folder()
    print(f"📂 Found latest session: {os.path.basename(session_dir)}\n")

    wav_files = sorted(glob.glob(os.path.join(session_dir, "*.wav")))

    if not wav_files:
        print("⚠️ No audio files found in the latest session folder.")
        exit(0)

    for wav in wav_files:
        process_audio_file(wav)

    print("🎉 Done transcribing and summarizing session.")
