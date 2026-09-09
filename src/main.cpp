#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/ringbuf.h>

extern "C" {
#include "layer3.h"
}

// === CONFIGURATION ===
#define WIFI_SSID       "OPUS"
#define WIFI_PASSWORD   "567890123"
#define TCP_PORT        8080

#define PIN_RED_LED     25
#define PIN_GREEN_LED   26

#define I2S_PORT        I2S_NUM_0
#define ADC_CHANNEL     ADC1_CHANNEL_7
#define TARGET_SAMPLE_RATE 16000
#define DMA_BUF_COUNT   8
#define DMA_BUF_LEN     512

#define RING_BUF_SIZE   (16 * 1024)
#define LED_FLICKER_MS  100
#define WIFI_CHECK_MS   2000

// === GLOBALS ===
static RingbufHandle_t mp3_ring_buf = NULL;
static WiFiServer tcp_server(TCP_PORT);
static WiFiClient tcp_client;

static volatile bool wifi_connected = false;
static volatile bool client_connected = false;
static volatile bool streaming_active = false;
static volatile uint32_t actual_sample_rate = TARGET_SAMPLE_RATE;
static uint32_t frames_encoded = 0;
static uint32_t frames_dropped = 0;

static unsigned long last_led_toggle = 0;
static bool green_led_state = false;
static unsigned long last_wifi_check = 0;

// === LED STATE MACHINE (Core 0 only, non-blocking) ===
static wl_status_t last_wifi_status = WL_IDLE_STATUS;

static void led_update() {
    unsigned long now = millis();

    // Poll WiFi status directly
    wl_status_t status = WiFi.status();
    wifi_connected = (status == WL_CONNECTED);

    // Print status changes
    if (status != last_wifi_status) {
        Serial.printf("WiFi status changed: %d -> %d\n", last_wifi_status, status);
        last_wifi_status = status;
    }

    if (!wifi_connected) {
        digitalWrite(PIN_RED_LED, HIGH);
        digitalWrite(PIN_GREEN_LED, LOW);
        green_led_state = false;
        return;
    }

    digitalWrite(PIN_RED_LED, LOW);

    if (streaming_active && client_connected) {
        if (now - last_led_toggle >= LED_FLICKER_MS) {
            last_led_toggle = now;
            green_led_state = !green_led_state;
            digitalWrite(PIN_GREEN_LED, green_led_state ? HIGH : LOW);
        }
    } else {
        digitalWrite(PIN_GREEN_LED, HIGH);
        green_led_state = true;
    }
}

// === I2S ADC INIT ===
static void i2s_adc_init() {
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_ADC_BUILT_IN),
        .sample_rate = TARGET_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_MSB,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = DMA_BUF_COUNT,
        .dma_buf_len = DMA_BUF_LEN,
        .use_apll = true,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };
    esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("i2s_driver_install failed: %d\n", err);
        return;
    }

    i2s_set_adc_mode(ADC_UNIT_1, ADC_CHANNEL);
    i2s_adc_enable(I2S_PORT);
    Serial.println("I2S ADC initialized");
}

// === SAMPLE RATE (hardcoded, calibration causes watchdog reset) ===
static void calibrate_sample_rate() {
    actual_sample_rate = 16000;
    Serial.printf("Sample rate: %u Hz (hardcoded)\n", actual_sample_rate);
}

// === SHINE MP3 ENCODER ===
static shine_t shine_enc = NULL;

static bool shine_encoder_init() {
    shine_config_t config;
    config.wave.channels = PCM_MONO;
    config.wave.samplerate = actual_sample_rate;
    config.mpeg.mode = MONO;
    config.mpeg.bitr = 64;
    config.mpeg.emph = NONE;
    config.mpeg.copyright = 0;
    config.mpeg.original = 1;

    shine_enc = shine_initialise(&config);
    if (!shine_enc) {
        Serial.println("Shine encoder init FAILED");
        return false;
    }
    Serial.printf("Shine encoder: %d samples/frame, %d Hz, %d kbps mono\n",
                  shine_samples_per_pass(shine_enc), actual_sample_rate, 64);
    return true;
}

// === CORE 1: AUDIO CAPTURE + ENCODE TASK ===
static void audio_encode_task(void *param) {
    (void)param;

    const int samples_per_pass = shine_samples_per_pass(shine_enc);
    uint16_t dma_buf[DMA_BUF_LEN];
    int16_t pcm_frame[samples_per_pass];
    int pcm_pos = 0;

    Serial.printf("Encoder task running on Core %d, samples_per_pass=%d\n",
                  xPortGetCoreID(), samples_per_pass);

    while (true) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_read(I2S_PORT, dma_buf, sizeof(dma_buf), &bytes_read, pdMS_TO_TICKS(100));
        if (err != ESP_OK || bytes_read == 0) continue;

        int samples_in_chunk = bytes_read / sizeof(uint16_t);

        for (int i = 0; i < samples_in_chunk; i++) {
            uint16_t raw_adc = (dma_buf[i] >> 4) & 0x0FFF;
            int16_t zero_centered = (int16_t)raw_adc - 2048;
            pcm_frame[pcm_pos++] = zero_centered << 4;

            if (pcm_pos >= samples_per_pass) {
                int written = 0;
                unsigned char *mp3_data = shine_encode_buffer_interleaved(
                    shine_enc, pcm_frame, &written);

                if (written > 0 && mp3_data != NULL) {
                    BaseType_t ret = xRingbufferSend(mp3_ring_buf, mp3_data, written, 0);
                    if (ret == pdTRUE) {
                        frames_encoded++;
                    } else {
                        frames_dropped++;
                    }
                }
                pcm_pos = 0;
            }
        }
    }
}

// === CORE 0: TCP SERVER + STREAMING TASK ===
static void tcp_stream_task(void *param) {
    (void)param;

    tcp_server.begin();
    Serial.printf("TCP server listening on port %d\n", TCP_PORT);

    while (true) {
        led_update();

        // WiFi reconnection check (non-blocking, outside event handler)
        if (!wifi_connected && (millis() - last_wifi_check >= WIFI_CHECK_MS)) {
            last_wifi_check = millis();
            Serial.printf("Retrying WiFi... (status=%d)\n", WiFi.status());
            WiFi.disconnect(true);
            delay(50);
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        }

        if (wifi_connected && !client_connected) {
            tcp_client = tcp_server.accept();
            if (tcp_client) {
                client_connected = true;
                streaming_active = true;
                tcp_client.setNoDelay(true);
                Serial.println("TCP client connected");
            }
        }

        if (client_connected) {
            if (!tcp_client.connected()) {
                client_connected = false;
                streaming_active = false;
                Serial.println("TCP client disconnected");
            } else {
                size_t item_size = 0;
                void *item = xRingbufferReceive(mp3_ring_buf, &item_size, pdMS_TO_TICKS(10));
                if (item != NULL && item_size > 0) {
                    tcp_client.write((const uint8_t *)item, item_size);
                    vRingbufferReturnItem(mp3_ring_buf, item);
                }
            }
        }

        led_update();
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

// === WIFI EVENT HANDLER (just flag, no reconnect here) ===
static void onWiFiEvent(arduino_event_id_t event) {
    switch (event) {
        case IP_EVENT_STA_GOT_IP:
            wifi_connected = true;
            Serial.printf("WiFi CONNECTED, IP: %s\n", WiFi.localIP().toString().c_str());
            break;
        case WIFI_EVENT_STA_DISCONNECTED:
            wifi_connected = false;
            client_connected = false;
            streaming_active = false;
            Serial.println("WiFi DISCONNECTED");
            break;
        default:
            break;
    }
}

// === SETUP ===
void setup() {
    Serial.begin(115200);
    delay(100);
    Serial.println("\n=== ESP32 Audio MP3 Streamer ===");

    pinMode(PIN_RED_LED, OUTPUT);
    pinMode(PIN_GREEN_LED, OUTPUT);
    digitalWrite(PIN_RED_LED, HIGH);
    digitalWrite(PIN_GREEN_LED, LOW);

    mp3_ring_buf = xRingbufferCreate(RING_BUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!mp3_ring_buf) {
        Serial.println("Ring buffer creation FAILED!");
        while (1) delay(1000);
    }

    WiFi.onEvent(onWiFiEvent);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(false);
    Serial.printf("Connecting to [%s]...\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    // Print connection status for 10 seconds
    for (int i = 0; i < 20; i++) {
        delay(500);
        Serial.printf("  WiFi status: %d (3=connected)\n", WiFi.status());
        if (WiFi.status() == WL_CONNECTED) break;
    }

    i2s_adc_init();
    calibrate_sample_rate();

    if (!shine_encoder_init()) {
        Serial.println("Encoder init failed!");
        while (1) delay(1000);
    }

    xTaskCreatePinnedToCore(audio_encode_task, "AudioEnc", 8192, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(tcp_stream_task, "TCPStream", 8192, NULL, 1, NULL, 0);

    Serial.println("System initialized.");
}

// === LOOP (runs on Core 1, minimal work) ===
void loop() {
    if (frames_encoded % 100 == 0 && frames_encoded > 0) {
        Serial.printf("Encoded: %u | Dropped: %u | Rate: %u Hz | Client: %s\n",
                      frames_encoded, frames_dropped, actual_sample_rate,
                      client_connected ? "YES" : "NO");
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
}
