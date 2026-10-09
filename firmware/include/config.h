// Jal-Drishti ESP32 DevKit V1 - wiring and tuning. Change pins here only.
#pragma once

// ---- soil sensors: AO pin -> ADC1 pins only! -----------------------------------------
// Works with the 4-pin resistive module (VCC, GND, DO, AO - DO is not used) and with the
// 3-pin capacitive v1.2.
// (ADC2 pins stop working while Wi-Fi is on.) One entry per zone; to add zones later,
// use GPIO 34, 35 (input-only, fine for ADC) and raise jal::N_ZONES in lib/jal_core.
static const int SENSOR_PINS[] = {32, 33};

// ---- relay module IN1, IN2 (a 4-channel board works too: leave IN3/IN4 unconnected) ---
// Boot-safe pins (no glitches at power-up). Spare for more zones: 27, 23.
static const int RELAY_PINS[] = {25, 26};
// Most cheap relay boards switch ON when the input is LOW. If your pumps run when they
// should be off, set this to false.
#define RELAY_ACTIVE_LOW true

// ---- DHT11 temperature / humidity (optional; for a DHT22 set DHT_TYPE DHT22) ------
#define USE_DHT true
#define DHT_TYPE DHT11           // DHT11: +-2 C, +-5 % RH, reads at most once per second
#define DHT_PIN 4

// ---- 16x2 LCD (JHD162A or any HD44780 display) ---------------------------------------
// Shows "KisanX" -> "Welcome!" at power-up, then both zones (rotating every 3 s).
//   LCD_PARALLEL : bare 16-pin module wired straight to the ESP32 (4-bit mode, RW to GND)
//   LCD_I2C      : module with a small I2C board soldered on the back (4 pins GND VCC SDA SCL)
//   LCD_NONE     : no display
#define LCD_NONE 0
#define LCD_PARALLEL 1
#define LCD_I2C 2
#define LCD_MODE LCD_I2C
#define LCD_RS 13                // parallel pins (all free, boot-safe outputs)
#define LCD_E 14
#define LCD_D4 16                // labelled RX2 on the DevKit
#define LCD_D5 17                // labelled TX2
#define LCD_D6 18
#define LCD_D7 19
#define LCD_SDA 21               // I2C pins
#define LCD_SCL 22
#define LCD_SPLASH_MS 2500UL     // "KisanX", then "Welcome!", each this long
#define LCD_PAGE_MS 3000UL       // then: both zones -> zone 1 -> zone 2 -> ...

// ---- status LED (on-board blue LED) ---------------------------------------------------
#define LED_PIN 2

// ---- sensor power switching -----------------------------------------------------------
// Resistive probes corrode if powered all the time. Both sensors' VCC go to this pin; it
// is switched on only for each reading (~0.1 s every 2 s). Set to -1 if VCC goes to 3V3.
#define SENSOR_POWER_PIN 27
#define SENSOR_SETTLE_MS 100

// ---- timing -----------------------------------------------------------------------------
#define SENSOR_PERIOD_MS 2000UL      // publish readings every 2 s
#define ADC_SAMPLES 16               // samples per sensor per reading (median)
#define SAFETY_TICK_MS 50            // pump timer resolution (separate high-priority task)
#define MQTT_RETRY_MS 5000UL

// ---- safety (must match the Pi: server/jaldrishti_server/settings.py) -------------------
#define MAX_PUMP_MS 120000UL         // no single pump run longer than this
#define HEARTBEAT_TIMEOUT_MS 30000UL // Pi silent this long -> all pumps OFF, fallback mode

// ---- fallback when the Pi is down (very conservative) ----------------------------------
// Raw ADC: HIGHER = DRIER. ~2700 is very dry for a typical v1.2 sensor (air ~2900-3100,
// water ~1300). Check your sensors with the serial command "read" and adjust.
#define FALLBACK_ENABLED true
#define FALLBACK_DRY_RAW 3600            // resistive probe: dry soil ~3500-4095
#define FALLBACK_PULSE_MS 8000UL
#define FALLBACK_MIN_GAP_MS (4UL * 3600UL * 1000UL)

#define FW_VERSION "1.2.0"   // 1.1: sensor power on D27, mDNS; 1.2: 16x2 LCD
