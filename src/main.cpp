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
#define WIFI_HOSTNAME   "Xapyra-o3"
#define TCP_PORT        8080

#define PIN_RED_LED     25
#define PIN_GREEN_LED   26

// === LED PWM (ESP32 LEDC hardware PWM) ===
// Driver LED di GPIO25/26 disetel digitalWrite HIGH = 100% duty = blazing.
// Pakai LEDC supaya bisa dimorph via duty cycle, tanpa software ISR.
// 5kHz: jauh di atas batas kedip mata/kamera, dan Aman untuk audio
// (I2S + encoder jalan terus, PWM tak boleh jadi sumber desis).
#define LED_PWM_FREQ        5000
#define LED_PWM_BITS        8
#define LED_PWM_MAX         ((1 << LED_PWM_BITS) - 1)   // 255 == duty penuh

// Brightness relatif 0..255. Turunin = redup.
// Hijau kelihatan lebih terang dari merah pada arus sama, jadi
// nilainya dibedain sedikit.
#define LED_BRIGHT_RED      25    // ~10%
#define LED_BRIGHT_GREEN    30    // ~12%

// Channel LEDC: 4 dan 6 -> timer berbeda (2 dan 3), jadi freq/resolution
// masing-masing LED bisa diubah sendiri-sendiri nanti.
#define LED_CH_RED          4
#define LED_CH_GREEN        6

#define I2S_PORT        I2S_NUM_0
#define ADC_CHANNEL     ADC1_CHANNEL_7
#define TARGET_SAMPLE_RATE 32000
#define DMA_BUF_COUNT   8
#define DMA_BUF_LEN     512

#define RING_BUF_SIZE   (32 * 1024)  // nilai github terbukti. 64KB sempat
                                     // dicoba tapi menekan heap LWIP/WiFi.
// Green LED saat streaming: bukan flicker lagi, tapi "blip" activity.
// Nyala sebentar (ON), lalu mati lama (OFF). Sekitar 0.5 Hz.
// Sekadar bukti audio mengalir, tidak menyilaukan ruangan
// waktu stream berjalan lama.
#define LED_BEEP_ON_MS   60
#define LED_BEEP_OFF_MS  2000
#define LED_BEEP_LEAD_MS 120   // jeda sebelum blip pertama pas mulai transfer
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

static uint32_t next_green_change = 0;   // ms epoch untuk transisi blip berikutnya
static bool green_led_state = false;
static bool green_was_streaming = false; // detek masuk/keluar state streaming
static unsigned long last_wifi_check = 0;

// === INPUT TRIM (headroom, WAJIB kuat karena pot MAX4466 mentok) ===
// Pot mentok ~125x: ADC sering rail saat vokal keras. Trim otomatis wajib
// turun DALAM dan CEPAT (bukan 0.7), kalau tidak flat-top clip masuk DSP
// -> kompresor/limiter kerja rodi = putus-putus. Rentang total trim:
// 0.5*1.0=0.5 (-6dB) s/d 0.5*0.12=0.06 (-24dB). Pulihnya pelan biar tidak
// pumping saat kalimat naik-turun.
#define INPUT_TRIM_BASE 0.5f    // -6dB tetap
static float input_trim_auto = 1.0f;  // 0.12..1.0, adaptif via clip detector
#define TRIM_MIN  0.12f
#define TRIM_DOWN 0.7f    // serang cepat saat clip (tiap 0.25 detik)
#define TRIM_UP   1.03f   // pulih pelan (~14 detik full-range)
static uint32_t clip_count = 0;
static uint32_t total_samples = 0;

// === HIGH-PASS 2-POLE (vocal, cut <120Hz, 12dB/oct) ===
// Potong DC + dengung/ruang di bawah pita suara, tapi JANGAN makan
// fundamental vokal (85-255Hz). Dulu 200Hz: suara cowok tipis.
// Sekarang 120Hz: alpha = exp(-2*pi*120/32000) = 0.9767.
static float hp1_x_prev = 0.0f, hp1_y_prev = 0.0f;
static float hp2_x_prev = 0.0f, hp2_y_prev = 0.0f;
#define HP_ALPHA 0.9767f

static inline float highpass_stage(float x, float *x_prev, float *y_prev) {
    float y = HP_ALPHA * (*y_prev + x - *x_prev);
    *x_prev = x;
    *y_prev = y;
    return y;
}

// === LOW-PASS 3-POLE (cut >5kHz hiss, 18dB/oct) ===
// Rekaman lama bocor: ~70% energi di atas 4kHz = hiss murni, bukan vokal.
// 2-pole lama terlalu landai. 3x cascade di 5kHz: vokal (s/d fricative)
// lolos, hiss 8kHz+ ketekan ~-18dB.
// fc=5000Hz, fs=32kHz → alpha = exp(-2*pi*fc/fs) ≈ 0.3746
static float lp1_y_prev = 0.0f;
static float lp2_y_prev = 0.0f;
static float lp3_y_prev = 0.0f;
#define LP_ALPHA 0.3746f

static inline float lowpass_stage(float x, float *y_prev) {
    float y = LP_ALPHA * (*y_prev) + (1.0f - LP_ALPHA) * x;
    *y_prev = y;
    return y;
}

// === PRESENCE SHELF (+6dB di atas ~2kHz, murah tanpa biquad) ===
// Suara 7m kehilangan konsonan (2-5kHz = inteligibilitas). Shelf ini
// dongkrak pita itu. y = x + G*(x - lp2k(x)). Hiss sudah dipotong LP
// di atas, jadi yang naik cuma pita vokal.
static float pres_lp_prev = 0.0f;
#define PRES_LP_ALPHA 0.6753f   // lp follower 2kHz @32k
#define PRESENCE_GAIN 1.0f      // ~+6dB shelf

static inline float presence_process(float x) {
    pres_lp_prev = PRES_LP_ALPHA * pres_lp_prev + (1.0f - PRES_LP_ALPHA) * x;
    return x + PRESENCE_GAIN * (x - pres_lp_prev);
}

// === NOISE SUPPRESSOR (expander + VAD hangover, anti-hiss saat sepi) ===
// Estimasi lantai noise adaptif (min-follower lambat), hitung SNR sesaat.
// SNR tinggi -> lewat (gain 1). SNR rendah -> tekan ke -18dB.
// Hangover 350ms: akhir kata tidak kepotong. Semua time-domain,
// ~15 flop/sample, Aman bareng encoder MP3.
static float ns_env = 0.0f;     // envelope cepat sinyal pita-vokal
static float ns_floor = 300.0f; // estimasi lantai noise (start konservatif)
static float ns_gain = 1.0f;
static uint32_t ns_hold = 0;    // sisa hangover, dalam sample
static bool ns_voice_open = true;
#define NS_ATK_ENV   0.05f      // envelope attack (~1ms)
#define NS_REL_ENV   0.001f     // envelope release (~30ms)
#define NS_FLOOR_UP  0.000005f  // floor naik SANGAT pelan (TC ~6 detik).
    // WAJIB lambat + tanpa drift konstan: kalau cepat, floor mengejar
    // vokal kontinyu lalu gate menutup sendiri = putus-putus.
#define NS_FLOOR_DN  0.005f     // floor turun cepat ke minimum baru
#define NS_SNR_HI    3.5f       // di atas ini = vokal, lewat penuh
#define NS_SNR_LO    1.2f       // di bawah ini = noise, tekan
#define NS_FLOOR_SUPPRESS 0.12f // -18dB saat murni noise
#define NS_HOLD_SAMPLES (11200u) // 350ms @32kHz, akhir kata Aman
#define NS_ATK_GAIN  0.02f      // tutup PELAN (dulu 0.05: flutter = putus-putus)
#define NS_REL_GAIN  0.002f     // buka pelan (anti-chatter)

static inline float ns_process(float sample) {
    float a = fabsf(sample);
    float ea = (a > ns_env) ? NS_ATK_ENV : NS_REL_ENV;
    ns_env += ea * (a - ns_env);

    // Lantai noise ikuti minimum envelope: cepat turun, pelan naik.
    if (ns_env < ns_floor) {
        ns_floor += NS_FLOOR_DN * (ns_env - ns_floor);
    } else {
        ns_floor += NS_FLOOR_UP * (ns_env - ns_floor);
    }
    if (ns_floor < 20.0f) ns_floor = 20.0f;
    if (ns_floor > 5000.0f) ns_floor = 5000.0f;

    float snr = ns_env / (ns_floor + 1.0f);
    float target;
    if (snr >= NS_SNR_HI) {
        target = 1.0f;
    } else if (snr <= NS_SNR_LO) {
        target = NS_FLOOR_SUPPRESS;
    } else {
        // Interpolasi halus di lutut
        float t = (snr - NS_SNR_LO) / (NS_SNR_HI - NS_SNR_LO);
        target = NS_FLOOR_SUPPRESS + t * (1.0f - NS_FLOOR_SUPPRESS);
    }

    if (target > 0.9f) {
        ns_hold = NS_HOLD_SAMPLES; // vokal: isi ulang hangover
    } else if (ns_hold > 0) {
        ns_hold--;
        target = 1.0f; // dalam hangover: tetap buka
    }

    float ga = (target < ns_gain) ? NS_ATK_GAIN : NS_REL_GAIN;
    ns_gain += ga * (target - ns_gain);
    ns_voice_open = (ns_gain > 0.5f);
    return sample * ns_gain;
}

// === COMPRESSOR (soft-knee, anti-pecah transien) ===
// Threshold -9dBFS, ratio 4:1, attack ~2ms, release ~150ms.
// Disengaja LEMBUT: sinyal ADC sekarang utuh, kompresor keras = pumping
// (terdengar putus-putus). Tugas berat pindah ke AGC+limiter.
static float comp_env = 0.0f;
static float comp_gain = 1.0f;
#define COMP_THRESH  12000.0f   // ~-9dBFS
#define COMP_RATIO   4.0f
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

// === AGC (slow leveler + noise gate, retune jarak jauh 7m) ===
// MAX_GAIN 3.0 -> 10.0: suara 7m (~-17dB vs 1m) butuh gain besar.
// Supaya hiss tidak ikut kencang: gain naik HANYA saat suppressor yakin
// ada vokal (ns_voice_open + envelope > 2x lantai noise).
static float agc_peak = 1.0f;
static float agc_gain = 1.0f;
#define AGC_TARGET    15000.0f   // target peak (~-9dBFS): tegas tapi tidak
                                 // gepeng di limiter. Pot mentok + target 20k
                                 // = rms terlalu panas.
#define AGC_ATTACK    0.1f      // fast attack (gain down)
#define AGC_RELEASE   0.002f    // medium release (gain up, ikut jarak)
#define AGC_MAX_GAIN  10.0f
#define AGC_MIN_GAIN  0.1f

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

    // Freeze gain naik kecuali VAD yakin ada vokal (open + envelope nyata
    // di atas lantai noise). Kriteria level tetap (dulu agc_peak<800) bikin
    // dua masalah: suara jauh tidak pernah diangkat, dan jeda kata malah
    // dipompa naik lalu kata berikutnya menghantam limiter = putus-putus.
    bool voiced = ns_voice_open && (ns_env > ns_floor * 2.0f);
    if (!voiced && desired_gain > agc_gain) {
        desired_gain = agc_gain;
    }

    float alpha = (desired_gain < agc_gain) ? AGC_ATTACK : AGC_RELEASE;
    agc_gain = agc_gain + alpha * (desired_gain - agc_gain);
    return sample * agc_gain;
}

// === LIMITER + SOFT CLIP (brickwall -3dB, anti pecah digital) ===
// Sisa peak lewat kompresor/AGC dilembutkan di sini, bukan hard-clip.
#define LIMIT_THRESH 23170.0f  // -3dBFS
#define LIMIT_KNEE   9000.0f   // lutut lebar: transien dibulatkan, bukan dipenggal

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

// Brightness 0..255 -> duty 0..LED_PWM_MAX (0 = mati, 255 = duty penuh)
static inline uint8_t led_duty(uint8_t brightness) {
    return (uint8_t)((uint32_t)brightness * LED_PWM_MAX / 255u);
}

// Tulis duty lewat hardware PWM. on=false -> duty 0 (mati, bukan "redup").
static inline void led_red(bool on) {
    ledcWrite(LED_CH_RED, on ? led_duty(LED_BRIGHT_RED) : 0);
}

static inline void led_green(bool on) {
    ledcWrite(LED_CH_GREEN, on ? led_duty(LED_BRIGHT_GREEN) : 0);
}

static void led_pwm_init() {
    ledcSetup(LED_CH_RED,   LED_PWM_FREQ, LED_PWM_BITS);
    ledcSetup(LED_CH_GREEN, LED_PWM_FREQ, LED_PWM_BITS);
    ledcAttachPin(PIN_RED_LED,   LED_CH_RED);
    ledcAttachPin(PIN_GREEN_LED, LED_CH_GREEN);
    led_red(true);      // boot: merah nyalain, hijau mati
    led_green(false);
}

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
        led_red(true);
        led_green(false);
        green_led_state = false;
        green_was_streaming = false;
        next_green_change = 0;
        return;
    }

    led_red(false);

    if (streaming_active && client_connected) {
        if (!green_was_streaming) {
            // Baru masuk state streaming: padam dulu, blip menyusul
            // sebentar. Kalau ga di-reset, LED bakal nyala solid
            // sampai 2 detik sebelum blip pertama.
            green_was_streaming = true;
            green_led_state = false;
            led_green(false);
            next_green_change = now + LED_BEEP_LEAD_MS;
        } else if ((int32_t)(now - next_green_change) >= 0) {
            // Rollover-safe: signed diff, bukan unsigned `now >= deadline`.
            green_led_state = !green_led_state;
            led_green(green_led_state);
            next_green_change = now + (green_led_state ? LED_BEEP_ON_MS : LED_BEEP_OFF_MS);
        }
    } else {
        led_green(true);
        green_led_state = true;
        green_was_streaming = false;
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
            // Format DMA ADC_BUILT_IN 16-bit: data 12-bit di bit RENDAH,
            // mask langsung TANPA shift. (>>4 yang lama buang 4 bit = -24dB:
            // itu sebabnya pot mentok pun rekaman pelan + mendesis.)
            uint16_t raw_adc = dma_buf[i] & 0x0FFF;
            total_samples++;
            if (raw_adc <= 3 || raw_adc >= 4092) clip_count++;

            int16_t zero_centered = (int16_t)raw_adc - 2048;
            float sample = (float)(zero_centered << 4);

            // Trim headroom dulu (penting saat gain mentok)
            sample *= (INPUT_TRIM_BASE * input_trim_auto);

            // Audio pipeline: HPx2(120Hz) → LPx3(5k) → PRESENCE → NS-GATE → COMP → AGC → LIMIT
            sample = highpass_stage(sample, &hp1_x_prev, &hp1_y_prev);
            sample = highpass_stage(sample, &hp2_x_prev, &hp2_y_prev);
            sample = lowpass_stage(sample, &lp1_y_prev);
            sample = lowpass_stage(sample, &lp2_y_prev);
            sample = lowpass_stage(sample, &lp3_y_prev);
            sample = presence_process(sample);
            sample = ns_process(sample);
            sample = compressor_process(sample);
            sample = agc_process(sample);
            pcm_frame[pcm_pos++] = limiter_softclip(sample);

            // Auto-trim 4x/detik: clip >0.5% potong cepat (x0.7), sepi naik
            // pelan (x1.03). Dalam s/d 0.12: pot mentok butuh itu.
            if (total_samples % 8000 == 0) {
                static uint32_t last_clip = 0;
                static uint32_t last_total = 0;
                uint32_t d_clip = clip_count - last_clip;
                uint32_t d_tot = total_samples - last_total;
                last_clip = clip_count;
                last_total = total_samples;
                if (d_tot > 0) {
                    float rate = (float)d_clip / (float)d_tot;
                    if (rate > 0.005f && input_trim_auto > TRIM_MIN) {
                        input_trim_auto *= TRIM_DOWN;
                        if (input_trim_auto < TRIM_MIN) input_trim_auto = TRIM_MIN;
                    } else if (rate < 0.0005f && input_trim_auto < 1.0f) {
                        input_trim_auto *= TRIM_UP;
                        if (input_trim_auto > 1.0f) input_trim_auto = 1.0f;
                    }
                }
            }

            if (pcm_pos >= samples_per_pass) {
                int written = 0;
                unsigned char *mp3_data = shine_encode_buffer_interleaved(
                    shine_enc, pcm_frame, &written);

                if (written > 0 && mp3_data != NULL) {
                    // Tunggu s/d 20ms saat ring penuh (DMA tahan 128ms, Aman).
                    // Timeout 0 dulu = buang frame saat WiFi jitter = putus-putus.
                    BaseType_t ret = xRingbufferSend(mp3_ring_buf, mp3_data, written, pdMS_TO_TICKS(20));
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
// Watchdog anti-macet: kalau client connect tapi NOL byte terkirim 3 detik
// (write() nyangkut di socket mati), client di-stop paksa supaya task tetap
// jalan (LED ikut hidup) dan server reconnect bersih. Tanpa ini task macet
// di write() -> ring penuh -> encode drop semua -> LED beku.
#define TCP_STALL_MS 3000
static uint32_t tcp_bytes_sent = 0;
static unsigned long tcp_last_drain_ms = 0;

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
                tcp_last_drain_ms = millis();
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
                    size_t n = tcp_client.write((const uint8_t *)item, item_size);
                    vRingbufferReturnItem(mp3_ring_buf, item);
                    if (n > 0) {
                        tcp_bytes_sent += n;
                        tcp_last_drain_ms = millis();
                    }
                }
                // Socket nyangkut: connect tapi tidak ada byte keluar.
                if ((unsigned long)(millis() - tcp_last_drain_ms) >= TCP_STALL_MS) {
                    Serial.printf("TCP stall %.0fs, resetting client (sent=%u)\n",
                                  (millis() - tcp_last_drain_ms) / 1000.0,
                                  tcp_bytes_sent);
                    tcp_client.stop();
                    client_connected = false;
                    streaming_active = false;
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

// === ADC DIAGNOSTIC (cek level mentah 0.5 detik saat boot) ===
// Kebijakan: pot MAX4466 MENTOK. Jadi rail saat vokal keras = normal,
// trim otomatis yang turunkan. Bicara normal 1-2m saat boot untuk cek:
// peak <1000        -> sinyal terlalu kecil bahkan untuk pot mentok (cek kabel/mic)
// peak 1000-15000  -> trim otomatis urus, beres
// rail terus (min~0/max~4095 + mean jauh dari tengah) -> trim kerja keras,
// suara tetap keluar tapi ujung keras terkompres (wajar untuk pot mentok)
static void diagnose_adc() {
    uint16_t buf[512];
    uint16_t mn = 4095, mx = 0;
    uint32_t sum = 0, n = 0;
    uint32_t t0 = millis();
    while (millis() - t0 < 500) {
        size_t br = 0;
        if (i2s_read(I2S_PORT, buf, sizeof(buf), &br, pdMS_TO_TICKS(50)) != ESP_OK || !br) continue;
        for (size_t i = 0; i < br / 2; i++) {
            uint16_t v = buf[i] & 0x0FFF;
            if (v < mn) mn = v;
            if (v > mx) mx = v;
            sum += v; n++;
        }
    }
    uint16_t peak = (mx > 2048 ? mx - 2048 : 2048 - mn);
    if (mn > mx) mn = mx = 0;
    Serial.printf("ADC raw: min=%u max=%u mean=%u peak_dev=%u -> %s\n",
                  mn, mx, n ? (unsigned)(sum / n) : 0, peak,
                  peak < 1000 ? "SINYAL KECIL (cek mic/kabel)" :
                  peak <= 15000 ? "OK, trim otomatis urus" : "RAIL (normal pot mentok, trim+comp jaga)");
}

// === SETUP ===
void setup() {
    Serial.begin(115200);
    delay(100);
    Serial.println("\n=== ESP32 Audio MP3 Streamer ===");

    pinMode(PIN_RED_LED, OUTPUT);
    pinMode(PIN_GREEN_LED, OUTPUT);
    led_pwm_init();

    mp3_ring_buf = xRingbufferCreate(RING_BUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!mp3_ring_buf) {
        Serial.println("Ring buffer creation FAILED!");
        while (1) delay(1000);
    }

    WiFi.onEvent(onWiFiEvent);
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(WIFI_HOSTNAME);  // nama perangkat di hotspot/DHCP
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
    diagnose_adc();  // cek level mentah 0.5 detik, panduan pot di Serial

    if (!shine_encoder_init()) {
        Serial.println("Encoder init failed!");
        while (1) delay(1000);
    }

    xTaskCreatePinnedToCore(audio_encode_task, "AudioEnc", 8192, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(tcp_stream_task, "TCPStream", 8192, NULL, 1, NULL, 0);

    Serial.println("System initialized.");
}

// === LOOP (runs on Core 1, diagnosa throughput tiap 2 detik) ===
// Format: Enc frame terkirim-ke-ring, Drop frame dibuang, Clip%, trim,
// ns/comp/agc gain, free byte ring, Client. Baca ini saat macet:
//  - Enc diam di 0      -> capture/encode macet (cek ADC/I2S/shine)
//  - Enc jalan, free->0 -> stream/TCP macet (cek WiFi/socket)
//  - Drop naik terus     -> WiFi lambat, ring kebanjiran
static unsigned long last_stat_ms = 0;
void loop() {
    unsigned long now = millis();
    if (now - last_stat_ms >= 2000) {
        last_stat_ms = now;
        size_t ring_free = 0;
        if (mp3_ring_buf) ring_free = xRingbufferGetCurFreeSize(mp3_ring_buf);
        float clip_pct = total_samples ? (100.0f * (float)clip_count / (float)total_samples) : 0.0f;
        Serial.printf("Enc:%u Drop:%u Clip:%.2f%% trim:%.2f ns:%.2f%s comp:%.2f agc:%.2f free:%u tcp:%u | Client:%s\n",
                      frames_encoded, frames_dropped, clip_pct,
                      INPUT_TRIM_BASE * input_trim_auto, ns_gain,
                      ns_voice_open ? "" : "(shut)",
                      comp_gain, agc_gain, (unsigned)ring_free,
                      tcp_bytes_sent,
                      client_connected ? "YES" : "NO");
    }
    vTaskDelay(pdMS_TO_TICKS(500));
}
