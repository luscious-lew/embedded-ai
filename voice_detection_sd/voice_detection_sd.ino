#include <I2S.h>
#include "FS.h"
#include "SD.h"
#include "SPI.h"

// AUDIO SETTINGS
#define SAMPLE_RATE         16000U
#define SAMPLE_BITS         16
#define WAV_HEADER_SIZE     44
#define BUFFER_SIZE         256

// RECORDING SETTINGS
#define RECORD_TIME_INITIAL 20  // seconds
#define MAX_RECORD_TIME     240 // seconds (not needed now)

#define VOLUME_GAIN         2

// DETECTION SETTINGS
#define ALPHA_CALIBRATION   0.2
#define ALPHA_OPERATION     0.01
#define ALPHA_AMBIENT       0.001
#define THRESH_MULT         1.2
#define RECALIBRATE_RATIO   1.5
#define CALIBRATION_TIME_MS 10000

#define POST_RECORD_WINDOW  5000 // ms to watch for continuation

// FILE INDEX
int fileNumber = 1;
int continuationNumber = 0;

// STATE VARIABLES
float stable_noise_floor = 0;
float ambient_noise_floor = 0;
float detection_threshold = 0;
bool calibrated = false;
unsigned long start_time;

bool recording = false;
bool postRecordWaiting = false;
unsigned long postRecordStart = 0;

uint8_t* rec_buffer = nullptr;
uint32_t rec_buffer_size = 0;

// Optional LED flash while recording
#define LED_PIN 2
unsigned long lastLedToggle = 0;
bool ledState = false;
#define LED_FLASH_INTERVAL 500  // ms

void setup() {
  Serial.begin(115200);
  while (!Serial);

  // I2S setup
  I2S.setAllPins(-1, 42, 41, -1, -1);
  if (!I2S.begin(PDM_MONO_MODE, SAMPLE_RATE, SAMPLE_BITS)) {
    Serial.println("❌ Failed to initialize I2S!");
    while (1);
  }

  // SD setup with retry loop
  while (!SD.begin(21)) {
    Serial.println("❌ Failed to mount SD Card. Retrying in 1 second...");
    delay(1000);
  }
  Serial.println("✅ SD Card mounted successfully!");

  findLastFileIndex();

  // LED
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.println("🔌 Starting calibration...");
  startCalibration();
}

void findLastFileIndex() {
  File root = SD.open("/");
  if (!root) {
    Serial.println("❌ Failed to open SD root directory");
    return;
  }

  while (true) {
    File file = root.openNextFile();
    if (!file) break;

    String name = file.name();
    if (name.endsWith(".wav")) {
      if (name.startsWith("/")) name = name.substring(1);
      name = name.substring(0, name.length() - 4);

      int num = name.toInt();
      if (num > 0 && num >= fileNumber) {
        fileNumber = num + 1;
      }
    }
    file.close();
  }

  root.close();
  Serial.printf("📁 Next file will be %d.wav\n", fileNumber);
}

void startCalibration() {
  start_time = millis();
  stable_noise_floor = 0;
  ambient_noise_floor = 0;
  detection_threshold = 0;
  calibrated = false;
}

void startRecording() {
  Serial.println("🎙️ Starting Recording!");

  rec_buffer_size = SAMPLE_RATE * SAMPLE_BITS / 8 * RECORD_TIME_INITIAL;
  rec_buffer = (uint8_t*)ps_malloc(rec_buffer_size);

  if (!rec_buffer) {
    Serial.println("❌ Failed to allocate PSRAM buffer!");
    while (1);
  }

  // Turn LED ON
  digitalWrite(LED_PIN, HIGH);

  size_t bytes_read = 0;
  esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, rec_buffer, rec_buffer_size, &bytes_read, portMAX_DELAY);
  
  Serial.printf("🎙️ Recording complete: %d bytes read.\n", bytes_read);

  stopRecordingAndSave(bytes_read);

  // After saving, go to post-record wait
  postRecordWaiting = true;
  postRecordStart = millis();

  free(rec_buffer);
  rec_buffer = nullptr;
}

void stopRecordingAndSave(size_t bytesRecorded) {
  String fileName;
  if (continuationNumber == 0) {
    fileName = "/" + String(fileNumber) + ".wav";
  } else {
    fileName = "/" + String(fileNumber) + "_cont" + String(continuationNumber) + ".wav";
  }

  File file = SD.open(fileName.c_str(), FILE_WRITE);
  if (!file) {
    Serial.println("❌ Failed to open file for writing!");
    return;
  }

  uint8_t wav_header[WAV_HEADER_SIZE];
  generate_wav_header(wav_header, bytesRecorded, SAMPLE_RATE);
  file.write(wav_header, WAV_HEADER_SIZE);

  // Apply volume gain
  for (uint32_t i = 0; i < bytesRecorded; i += SAMPLE_BITS/8) {
    (*(uint16_t *)(rec_buffer+i)) <<= VOLUME_GAIN;
  }

  file.write(rec_buffer, bytesRecorded);
  file.close();

  Serial.println("✅ Recording saved.");

  // Turn LED OFF
  digitalWrite(LED_PIN, LOW);
}

void generate_wav_header(uint8_t *wav_header, uint32_t wav_size, uint32_t sample_rate) {
  uint32_t file_size = wav_size + WAV_HEADER_SIZE - 8;
  uint32_t byte_rate = sample_rate * SAMPLE_BITS / 8;
  const uint8_t set_wav_header[] = {
    'R','I','F','F',
    file_size, file_size>>8, file_size>>16, file_size>>24,
    'W','A','V','E',
    'f','m','t',' ',
    0x10,0x00,0x00,0x00,
    0x01,0x00,
    0x01,0x00,
    sample_rate, sample_rate>>8, sample_rate>>16, sample_rate>>24,
    byte_rate, byte_rate>>8, byte_rate>>16, byte_rate>>24,
    0x02,0x00,
    0x10,0x00,
    'd','a','t','a',
    wav_size, wav_size>>8, wav_size>>16, wav_size>>24
  };
  memcpy(wav_header, set_wav_header, sizeof(set_wav_header));
}

void loop() {
  int16_t samples[BUFFER_SIZE];
  int bytesRead = I2S.read((uint8_t*)samples, sizeof(samples));
  if (bytesRead <= 0) return;

  int peak = 0;
  for (int i = 0; i < bytesRead / 2; i++) {
    int val = abs(samples[i]);
    if (val > peak) peak = val;
  }

  unsigned long now = millis();
  float alpha_current = calibrated ? ALPHA_OPERATION : ALPHA_CALIBRATION;

  if (!calibrated && now - start_time < CALIBRATION_TIME_MS) {
    stable_noise_floor = alpha_current * peak + (1.0 - alpha_current) * stable_noise_floor;
    ambient_noise_floor = stable_noise_floor;
    detection_threshold = stable_noise_floor * THRESH_MULT;
    delay(50);
    return;
  }

  if (!calibrated) {
    calibrated = true;
    Serial.printf("✅ Calibration complete. Floor: %.2f | Threshold: %.2f\n", stable_noise_floor, detection_threshold);
  }

  ambient_noise_floor = ALPHA_AMBIENT * peak + (1.0 - ALPHA_AMBIENT) * ambient_noise_floor;

  if (peak < stable_noise_floor * 0.7) {
    stable_noise_floor = alpha_current * peak + (1.0 - alpha_current) * stable_noise_floor;
  }

  if (ambient_noise_floor > stable_noise_floor * RECALIBRATE_RATIO) {
    Serial.println("🌎 Environment change detected — Recalibrating noise floor...");
    stable_noise_floor = ambient_noise_floor;
  }

  detection_threshold = stable_noise_floor * THRESH_MULT;

  Serial.printf("📊 Peak: %d | Threshold: %.2f | Ambient: %.2f\n", peak, detection_threshold, ambient_noise_floor);


  bool speech_now = (peak > detection_threshold);

  if (!recording && speech_now) {
    if (postRecordWaiting) {
      continuationNumber++;
      Serial.println("🗣️ Speech detected during post-record wait — Starting continuation!");
    } else {
      continuationNumber = 0;
    }
    recording = true;
    startRecording();
    recording = false;
  }

  if (postRecordWaiting && millis() - postRecordStart >= POST_RECORD_WINDOW) {
    Serial.println("⌛ Post-record window expired. Ready for next normal file.");
    postRecordWaiting = false;
    continuationNumber = 0;
    fileNumber++;
  }

  delay(10);
}
