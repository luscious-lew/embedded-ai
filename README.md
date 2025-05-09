# Personal Memory Assistant

## Embedded AI
### Team Members
- Felipe Andrade fga2116
- Lewis Clements lmc2300
- Michael John Flynn mf3657

- # Introduction
This repository contains two components for capturing, transcribing, and summarizing audio conversations:

1. **ESP32 Volume & Speech Detection** (`ESP32-volume_speech.ino`) – Arduino sketch that monitors audio levels via I2S microphone, uses an Edge Impulse model to distinguish speech from background noise, and streams raw audio to a Raspberry Pi over UART.
2. **Raspberry Pi Zero 2 W Recorder** (`RPI_ZERO2_micro.py`) – MicroPython/Python script that runs on a Raspberry Pi Zero 2 W, receives audio files from the ESP32, transcribes them using OpenAI or AssemblyAI, summarizes transcripts, and emails daily summaries.

---

## Repository Structure

```
.
├── ESP32-volume_speech.ino    # Arduino sketch for ESP32 speech detection
└── RPI_ZERO2_micro.py         # Python script for Pi Zero 2 W processing
```

---

## ESP32 Sketch (`ESP32-volume_speech.ino`)

### Overview

* Uses I2S peripheral to read audio from a MEMS microphone.
* Runs a continuous inference loop with an Edge Impulse speech detection model.
* When speech is detected (confidence > 0.8 for the speech class), toggles the onboard LED and begins streaming WAV-formatted audio over UART to the Pi.

### Key Functions (called automatically by Arduino runtime)

* `void setup()`

  * Initializes Serial (UART), I2S, GPIO pin modes, and the Edge Impulse inference engine.
  * Prints configuration details and waits 2 s before starting inference.

* `void loop()`

  * Captures one inference window via `microphone_inference_record()`.
  * Classifies the window and toggles `LED_BUILTIN` when speech is detected.
  * Handles buffering of samples and triggers `capture_samples()` tasks under the hood (Edge Impulse library).

* `static void capture_samples(void* arg)`

  * RTOS task that reads raw PCM samples from I2S into a circular buffer (`sampleBuffer`).

* `static void audio_inference_callback(uint32_t n_bytes)`

  * Called by the I2S reading task when new samples arrive.
  * Appends samples into the Edge Impulse inference buffer until enough data is collected.

> **Note:** Inference helper functions such as `microphone_inference_start()`, `microphone_inference_record()`, and `microphone_inference_end()` are provided by the Edge Impulse Arduino SDK included via `speech_detection3_inferencing.h`.

### Usage

1. Open `ESP32-volume_speech.ino` in the Arduino IDE.
2. Install the Edge Impulse Arduino Library (via Library Manager).
3. Select your ESP32 board, set the correct COM/Serial port.
4. Upload the sketch. The code will start automatically on boot.
5. Wire your I2S microphone (data-pin, clock, WS) and ground/VCC as per your board’s pinout.
6. Connect the ESP32’s TX/RX pins to the Pi’s RX/TX on `/dev/serial0` (GPIO14/15) for audio streaming.

---

## Raspberry Pi Zero 2 W Script (`RPI_ZERO2_micro.py`)

### Overview

1. **Wi‑Fi Connection**: Attempts to connect to a configured SSID via `nmcli`.
2. **GPIO Control**: Uses GPIO4 to coordinate with the ESP32—low = receive audio, high = process audio.
3. **UART Reception**: Listens on `/dev/serial0`, receives WAV headers and chunks, saves files to `audio/`.
4. **Transcription & Summarization**: Uses OpenAI Whisper or AssemblyAI APIs to transcribe and summarize text.
5. **Email Delivery**: Sends daily summary with attached transcripts via SMTP.
6. **Cleanup**: Deletes processed WAV files.

### Prerequisites

* **Hardware**: Raspberry Pi Zero 2 W with configured UART on `/dev/serial0`.
* **OS**: Raspberry Pi OS Lite (64‑bit).
* **Interpreter**:

  * MicroPython v1.22 Unix build (via `micropython`) *or* CPython 3 with `RPi.GPIO` and `requests`.
* **Libraries** (for CPython):

  ```bash
  pip install RPi.GPIO requests
  ```
* **Libraries** (for MicroPython):

  ```bash
  micropip install micropython-urequests micropython-smtplib
  ```

### Configuration (Environment Variables)

```bash
export BACKEND=assemblyai         # or "openai"
export ASSEMBLYAI_API_KEY=<key>   # if using AssemblyAI
export OPENAI_API_KEY=<key>       # if using OpenAI
export OPENAI_MODEL=gpt-3.5-turbo # or other supported model

export EMAIL_SENDER=you@domain.com
export EMAIL_RECEIVER=recipient@domain.com
export EMAIL_PASSWORD=<password>
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=465
export SMTP_USER=apikey          # for services like SendGrid
export SMTP_PASS=<smtp_password>

export WIFI_SSID=<ssid>
export WIFI_PASS=<wifi_password>
```

### Directory Layout

```
/home/pi/wav_pipeline/
├── audio/    # incoming .wav files from ESP32
└── out/      # transcripts and summary output
```

### Key Functions

* `wifi_connect()`

  * Checks if `wlan0` is associated; if not, uses `nmcli` to connect to `WIFI_SSID`.

* `gpio_setup()` & `gpio_read() -> int`

  * Configures and reads GPIO4; determines when to receive vs. process.

* `receive_wavs()`

  * Opens `/dev/serial0`, polls for header lines (`filename,size,crc`), reads chunks, writes `.wav` files.

* `transcribe_openai(path: str) -> str`

  * Uploads WAV to OpenAI Whisper endpoint; returns transcript text.

* `transcribe_aai(path: str) -> str`

  * Uploads to AssemblyAI; polls until transcript is ready; returns text.

* `summarize_openai(text: str) -> str`

  * Sends transcript to OpenAI chat completions; returns concise summary.

* `summarize_aai(text: str) -> str`

  * Uses AssemblyAI Lemur summarization; returns paragraph summary.

* `send_mail(summary: str, txt_path: str)`

  * Crafts a crude multipart email, attaches transcript, sends via SMTP\_SSL.

* `process_day()`

  * Finds all `.wav` in `audio/`; transcribes each; writes daily `transcripts_<date>.txt`; summarizes; emails; deletes `.wav` files.

* `main()`

  * High-level loop:

    1. `wifi_connect()`
    2. `gpio_setup()`
    3. If `gpio_read() == 0`, call `receive_wavs()`.
    4. If `gpio_read() == 1`, call `process_day()` and wait for pin to drop.

### Usage

```bash
# Standard Python
python3 RPI_ZERO2_micro.py

# MicroPython
./micropython RPI_ZERO2_micro.py
```

#### Interactive Examples

```python
>>> from RPI_ZERO2_micro import transcribe_openai, summarize_openai
>>> text = transcribe_openai('audio/test.wav')
>>> summary = summarize_openai(text)
>>> print(summary)
```

---

## License

Licensed under the MIT License (see LICENSE file for details).
