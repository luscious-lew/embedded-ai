import webrtcvad
import sounddevice as sd
import numpy as np
import wave
import time
import os
from collections import deque
from datetime import datetime

# --- Config ---
sample_rate = 16000
frame_duration = 30  # ms
frame_size = int(sample_rate * frame_duration / 1000)
vad_aggressiveness = 2
silence_threshold = 30.0      # seconds of silence before stopping
min_speech_duration = 5.0    # minimum actual speech required to save
pre_speech_padding = 0.5     # seconds before speech
post_speech_padding = 0.5    # seconds after speech

# --- Create a new session folder for this run ---
session_id = datetime.now().strftime("session_%Y-%m-%d_%H-%M-%S")
base_dir = os.path.join(os.getcwd(), "recordings", session_id)
os.makedirs(base_dir, exist_ok=True)
print(f"📁 Recording session started: {session_id}")

# --- Init state ---
vad = webrtcvad.Vad(vad_aggressiveness)
recording = False
audio_buffer = []
pre_speech_buffer = deque(maxlen=int(pre_speech_padding * 1000 / frame_duration))
silence_start_time = None
speech_frame_count = 0

# Save audio to session folder
def save_audio(frames, speech_frame_count):
    speech_duration = speech_frame_count * frame_duration / 1000.0
    if speech_duration < min_speech_duration:
        print("❌ Discarded: only %.2fs of speech" % speech_duration)
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"speech_{timestamp}.wav"
    filepath = os.path.join(base_dir, filename)

    with wave.open(filepath, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b''.join(frames))

    full_duration = len(frames) * frame_duration / 1000.0
    print(f"💾 Saved: {filepath} (%.2fs total, %.2fs speech)" % (full_duration, speech_duration))

# Audio stream callback
def callback(indata, frames, time_info, status):
    global recording, audio_buffer, silence_start_time, speech_frame_count

    if status:
        print("⚠️", status)

    pcm = (indata[:, 0] * 32768).astype(np.int16).tobytes()
    is_speech = vad.is_speech(pcm, sample_rate)

    if is_speech:
        if not recording:
            print("🎙️ Speech started")
            recording = True
            audio_buffer = list(pre_speech_buffer)
            speech_frame_count = 0
        speech_frame_count += 1
        audio_buffer.append(pcm)
        silence_start_time = None
    elif recording:
        audio_buffer.append(pcm)
        if silence_start_time is None:
            silence_start_time = time.time()
        elif time.time() - silence_start_time > silence_threshold:
            print("🤫 Speech ended, evaluating...")
            for _ in range(int(post_speech_padding * 1000 / frame_duration)):
                audio_buffer.append(pcm)
            save_audio(audio_buffer, speech_frame_count)
            recording = False
            audio_buffer = []
            silence_start_time = None
            speech_frame_count = 0
    else:
        pre_speech_buffer.append(pcm)

# Start listening
with sd.InputStream(channels=1, samplerate=sample_rate, blocksize=frame_size, dtype='float32', callback=callback):
    print("🎧 Listening for speech... Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n🛑 Exiting.")
