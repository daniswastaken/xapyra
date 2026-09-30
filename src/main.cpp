#include <Arduino.h>
#include <math.h>
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
#define TARGET_SAMPLE_RATE 32000
#define DMA_BUF_COUNT   8
#define DMA_BUF_LEN     512

#define RING_BUF_SIZE   (32 * 1024)
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

// === INPUT TRIM (headroom for MAX4466 gain mentok) ===
// MAX4466 pot full ~125x: ADC gampang rail. Trim dulu sebelum filter
// biar flat-top clip tidak masuk rantai DSP.
#define INPUT_TRIM_BASE 0.5f    // -6dB tetap
static float input_trim_auto = 1.0f;  // 0.25..1.0, adaptif via clip detector
static uint32_t clip_count = 0;
static uint32_t total_samples = 0;

// === HIGH-PASS 2-POLE (vocal, cut <200Hz, 12dB/oct) ===
// 1-pole lama loyo: pop/wind lolos, picu pumping. Cascade 2x.
static float hp1_x_prev = 0.0f, hp1_y_prev = 0.0f;
static float hp2_x_prev = 0.0f, hp2_y_prev = 0.0f;
#define HP_ALPHA 0.9622f

static inline float highpass_stage(float x, float *x_prev, float *y_prev) {
    float y = HP_ALPHA * (*y_prev + x - *x_prev);
    *x_prev = x;
    *y_prev = y;
    return y;
}

// === LOW-PASS 2-POLE (cut >4kHz hiss, 12dB/oct) ===
static float lp1_y_prev = 0.0f;
static float lp2_y_prev = 0.0f;
// fc=4000Hz, fs=32kHz → alpha = exp(-2*pi*fc/fs) ≈ 0.7788
#define LP_ALPHA 0.7788f

static inline float lowpass_stage(float x, float *y_prev) {
    float y = LP_ALPHA * (*y_prev) + (1.0f - LP_ALPHA) * x;
    *y_prev = y;
    return y;
}

// === COMPRESSOR (soft-knee, anti-pecah transien) ===
// Threshold -12dBFS, ratio 6:1, attack ~2ms, release ~150ms.
// Ini yang jaga suara stabil saat teriak/dekat mic.
static float comp_env = 0.0f;
static float comp_gain = 1.0f;
#define COMP_THRESH  8000.0f   // ~-12dBFS
#define COMP_RATIO   6.0f
#define COMP_ATK_ENV 0.02f     // envelope attack (~2ms)
#define COMP_REL_ENV 0.0002f   // envelope release (~150ms)
#define COMP_ATK_GAIN 0.1f     // gain turun cepat
#define COMP_REL_GAIN 0.005f   // gain naik pelan

static inline float compressor_process(float sample) {
    float abs_in = fabsf(sample);
    float a = (abs_in > comp_env) ? COMP_ATK_ENV : COMP_REL_ENV;
    comp_env += a * (abs_in - comp_env);

    float desired = 1.0f;
    if (comp_env > COMP_THRESH) {
        float compressed = COMP_THRESH + (comp_env - COMP_THRESH) / COMP_RATIO;
        desired = compressed / comp_env;
    }
    float ga = (desired < comp_gain) ? COMP_ATK_GAIN : COMP_REL_GAIN;
    comp_gain += ga * (desired - comp_gain);
    return sample * comp_gain;
}

// === AGC (slow leveler + noise gate) ===
// MAX_GAIN turun 8.0 -> 3.0: cegah hiss ikut kencang saat sepi.
// Gate 500: di bawah itu gain freeze, tidak pumping.
static float agc_peak = 1.0f;
static float agc_gain = 1.0f;
#define AGC_TARGET    20000.0f   // target peak level (~-6dB)
#define AGC_ATTACK    0.1f      // fast attack (gain down)
#define AGC_RELEASE   0.001f    // slow release (gain up)
#define AGC_MAX_GAIN  3.0f
#define AGC_MIN_GAIN  0.1f
#define AGC_GATE      500.0f    // freeze di bawah ini

static inline float agc_process(float sample) {
    float abs_sample = fabsf(sample);

    if (abs_sample > agc_peak) {
        agc_peak = abs_sample;
    } else {
        agc_peak = agc_peak * 0.9999f;
    }
    if (agc_peak < 1.0f) agc_peak = 1.0f;

    float desired_gain = AGC_TARGET / agc_peak;
    if (desired_gain > AGC_MAX_GAIN) desired_gain = AGC_MAX_GAIN;
    if (desired_gain < AGC_MIN_GAIN) desired_gain = AGC_MIN_GAIN;

    // Noise gate: jangan naikkan gain saat senyap
    if (agc_peak < AGC_GATE && desired_gain > agc_gain) {
        desired_gain = agc_gain;
    }

    float alpha = (desired_gain < agc_gain) ? AGC_ATTACK : AGC_RELEASE;
    agc_gain = agc_gain + alpha * (desired_gain - agc_gain);
    return sample * agc_gain;
}

// === LIMITER + SOFT CLIP (brickwall -3dB, anti pecah digital) ===
// Sisa peak lewat kompresor/AGC dilembutkan di sini, bukan hard-clip.
#define LIMIT_THRESH 23170.0f  // -3dBFS
#define LIMIT_KNEE   6000.0f

static inline int16_t limiter_softclip(float sample) {
    float abs_in = fabsf(sample);
    if (abs_in <= LIMIT_THRESH) {
        return (int16_t)sample;
    }
    float sign = (sample >= 0.0f) ? 1.0f : -1.0f;
    float over = abs_in - LIMIT_THRESH;
    // 1-exp soft knee: makin keras makin padat, tidak flat
    float soft = LIMIT_THRESH + (32768.0f - LIMIT_THRESH) * (1.0f - expf(-over / LIMIT_KNEE));
    if (soft > 32767.0f) soft = 32767.0f;
    return (int16_t)(sign * soft);
}

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
    actual_sample_rate = TARGET_SAMPLE_RATE;
    Serial.printf("Sample rate: %u Hz\n", actual_sample_rate);
}

// === SHINE MP3 ENCODER ===
static shine_t shine_enc = NULL;

static bool shine_encoder_init() {
    shine_config_t config;
    config.wave.channels = PCM_MONO;
    config.wave.samplerate = actual_sample_rate;
    config.mpeg.mode = MONO;
    config.mpeg.bitr = 128;
    config.mpeg.emph = NONE;
    config.mpeg.copyright = 0;
    config.mpeg.original = 1;

    shine_enc = shine_initialise(&config);
    if (!shine_enc) {
        Serial.println("Shine encoder init FAILED");
        return false;
    }
    Serial.printf("Shine encoder: %d samples/frame, %d Hz, %d kbps mono\n",
                  shine_samples_per_pass(shine_enc), actual_sample_rate, 128);
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
            total_samples++;
            if (raw_adc <= 3 || raw_adc >= 4092) clip_count++;

            int16_t zero_centered = (int16_t)raw_adc - 2048;
            float sample = (float)(zero_centered << 4);

            // Trim headroom dulu (penting saat gain mentok)
            sample *= (INPUT_TRIM_BASE * input_trim_auto);

            // Audio pipeline: HPx2 → LPx2 → COMP → AGC → LIMIT/SOFTCLIP → encode
            sample = highpass_stage(sample, &hp1_x_prev, &hp1_y_prev);
            sample = highpass_stage(sample, &hp2_x_prev, &hp2_y_prev);
            sample = lowpass_stage(sample, &lp1_y_prev);
            sample = lowpass_stage(sample, &lp2_y_prev);
            sample = compressor_process(sample);
            sample = agc_process(sample);
            pcm_frame[pcm_pos++] = limiter_softclip(sample);

            // Auto-trim 1x/detik: clip >0.5% turunkan, <0.05% naikkan pelan
            if (total_samples % 32000 == 0) {
                static uint32_t last_clip = 0;
                static uint32_t last_total = 0;
                uint32_t d_clip = clip_count - last_clip;
                uint32_t d_tot = total_samples - last_total;
                last_clip = clip_count;
                last_total = total_samples;
                if (d_tot > 0) {
                    float rate = (float)d_clip / (float)d_tot;
                    if (rate > 0.005f && input_trim_auto > 0.26f) {
                        input_trim_auto *= 0.9f;
                    } else if (rate < 0.0005f && input_trim_auto < 1.0f) {
                        input_trim_auto *= 1.02f;
                        if (input_trim_auto > 1.0f) input_trim_auto = 1.0f;
                    }
                }
            }

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
        float clip_pct = total_samples ? (100.0f * (float)clip_count / (float)total_samples) : 0.0f;
        Serial.printf("Enc:%u Drop:%u Clip:%.2f%% trim:%.2f comp:%.2f agc:%.2f | Client:%s\n",
                      frames_encoded, frames_dropped, clip_pct,
                      INPUT_TRIM_BASE * input_trim_auto, comp_gain, agc_gain,
                      client_connected ? "YES" : "NO");
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
}
