// Jal-Drishti ESP32 firmware - "the hands": sensors in, pumps out, safety always on.
//
// Protocol (must match server/jaldrishti_server/link.py):
//   publish   jal/sensors   every 2 s  {"raw":[..4],"temp":..,"rh":..,"pumps":[..4],"mode":"remote",...}
//   publish   jal/status    "online" / "offline" (retained, last-will)
//   subscribe jal/cmd       {"zone":3,"seconds":5,"id":17} | {"zone":3,"seconds":0} | {"all_off":true}
//   subscribe jal/heartbeat {"t":...,"mode":"auto","z":[{"c":"Tomato","m":45,"lo":36,"hi":62,"f":false},..]}
//                           every 5 s from the Pi; "z" is what the 16x2 LCD shows per zone
//
// Safety does NOT depend on the network: pump timers and the heartbeat watchdog run in a
// separate high-priority task every 50 ms, so even if Wi-Fi/MQTT code blocks, a pump stops
// on time. Logic lives in lib/jal_core (unit-tested on the laptop: pio test -e native).
#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <esp_task_wdt.h>

#include "JalCore.h"
#include "LcdText.h"
#include "config.h"      // must come before the USE_DHT / LCD_MODE checks below
#include "secrets.h"
#if USE_DHT
#include <DHT.h>
#endif
#if LCD_MODE == LCD_PARALLEL
#include <LiquidCrystal.h>
#elif LCD_MODE == LCD_I2C
#include <LiquidCrystal_I2C.h>
#include <Wire.h>
#endif

static_assert(sizeof(SENSOR_PINS) / sizeof(SENSOR_PINS[0]) == jal::N_ZONES, "one sensor pin per zone");
static_assert(sizeof(RELAY_PINS) / sizeof(RELAY_PINS[0]) == jal::N_ZONES, "one relay pin per zone");

static const char* TOPIC_SENSORS = "jal/sensors";
static const char* TOPIC_STATUS = "jal/status";
static const char* TOPIC_CMD = "jal/cmd";
static const char* TOPIC_HEARTBEAT = "jal/heartbeat";

static jal::Config makeConfig() {
    jal::Config c;
    c.maxPumpMs = MAX_PUMP_MS;
    c.heartbeatTimeoutMs = HEARTBEAT_TIMEOUT_MS;
    c.fallbackEnabled = FALLBACK_ENABLED;
    c.fallbackDryRaw = FALLBACK_DRY_RAW;
    c.fallbackPulseMs = FALLBACK_PULSE_MS;
    c.fallbackMinGapMs = FALLBACK_MIN_GAP_MS;
    return c;
}

static jal::Core core(makeConfig());
static portMUX_TYPE coreMux = portMUX_INITIALIZER_UNLOCKED;   // core is shared with the safety task

static WiFiClient wifiClient;
static PubSubClient mqtt(wifiClient);
#if USE_DHT
static DHT dht(DHT_PIN, DHT_TYPE);
#endif

static jal::Reading readings[jal::N_ZONES];
static float lastTemp = NAN, lastRh = NAN;
static uint32_t lastPublish = 0, lastMqttTry = 0, lastWifiTry = 0;
static String clientId;
static String serialLine;
static int relayTestStep = -1;
static bool sensorPowerHold = false;     // serial "power on": keep sensors powered for wiring checks
static uint32_t relayTestNext = 0;

// ---- LCD: what the Pi last told us (heartbeat), used while it is fresh -----------------
static jal::lcd::ZoneView piView[jal::N_ZONES];
static char piMode[8] = "";
static uint32_t piViewAt = 0;
static bool piViewSeen = false;
static uint32_t lastHeartbeatAt = 0;           // any heartbeat, with or without LCD data
static bool heartbeatSeen = false;
static char lcdShown[2][jal::lcd::COLS + 1];   // what is on the glass now (redraw only on change)
#if LCD_MODE == LCD_PARALLEL
static LiquidCrystal lcd(LCD_RS, LCD_E, LCD_D4, LCD_D5, LCD_D6, LCD_D7);
#elif LCD_MODE == LCD_I2C
static LiquidCrystal_I2C* lcd = nullptr;
#endif

// ---------------------------------------------------------------------------------------
static void setRelay(int i, bool on) {
    digitalWrite(RELAY_PINS[i], (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW);
}

static void safetyTask(void*) {
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        uint32_t now = millis();
        bool on[jal::N_ZONES];
        portENTER_CRITICAL(&coreMux);
        core.tick(now);
        for (int i = 0; i < jal::N_ZONES; ++i) on[i] = core.pumpOn(i, now);
        portEXIT_CRITICAL(&coreMux);
        for (int i = 0; i < jal::N_ZONES; ++i) setRelay(i, on[i]);
        vTaskDelayUntil(&last, pdMS_TO_TICKS(SAFETY_TICK_MS));
    }
}

static bool pumpCommand(int zone, float seconds) {
    portENTER_CRITICAL(&coreMux);
    bool ok = core.commandPump(zone, seconds, millis());
    portEXIT_CRITICAL(&coreMux);
    return ok;
}

static void allOff() {
    portENTER_CRITICAL(&coreMux);
    core.commandAllOff();
    portEXIT_CRITICAL(&coreMux);
}

// ---------------------------------------------------------------------------------------
static void readSensors() {
    int samples[ADC_SAMPLES];
#if SENSOR_POWER_PIN >= 0
    digitalWrite(SENSOR_POWER_PIN, HIGH);          // power the probes only while reading
    delay(SENSOR_SETTLE_MS);
#endif
    for (int z = 0; z < jal::N_ZONES; ++z) {
        for (int k = 0; k < ADC_SAMPLES; ++k) {
            samples[k] = analogRead(SENSOR_PINS[z]);
            delayMicroseconds(200);
        }
        readings[z] = jal::summarize(samples, ADC_SAMPLES, core.config().noiseSpreadMax);
    }
#if SENSOR_POWER_PIN >= 0
    if (!sensorPowerHold) digitalWrite(SENSOR_POWER_PIN, LOW);
#endif
#if USE_DHT
    float t = dht.readTemperature(), h = dht.readHumidity();
    if (!isnan(t) && !isnan(h)) { lastTemp = t; lastRh = h; }
    else { lastTemp = NAN; lastRh = NAN; }
#endif
}

static void publishReadings(uint32_t now) {
    JsonDocument doc;
    JsonArray raw = doc["raw"].to<JsonArray>();
    for (int z = 0; z < jal::N_ZONES; ++z) {
        if (readings[z].valid) raw.add(readings[z].raw);
        else raw.add(nullptr);                       // unplugged / noisy -> the Pi skips this zone
    }
    if (isnan(lastTemp)) doc["temp"] = nullptr; else doc["temp"] = roundf(lastTemp * 10) / 10;
    if (isnan(lastRh)) doc["rh"] = nullptr; else doc["rh"] = roundf(lastRh * 10) / 10;
    JsonArray pumps = doc["pumps"].to<JsonArray>();
    portENTER_CRITICAL(&coreMux);
    bool on[jal::N_ZONES];
    for (int z = 0; z < jal::N_ZONES; ++z) on[z] = core.pumpOn(z, now);
    bool fallback = core.mode() == jal::Mode::Fallback;
    uint32_t fbRuns = core.fallbackRuns();
    portEXIT_CRITICAL(&coreMux);
    for (int z = 0; z < jal::N_ZONES; ++z) pumps.add(on[z] ? 1 : 0);
    doc["mode"] = fallback ? "fallback" : "remote";
    doc["uptime"] = now / 1000;
    doc["fw"] = FW_VERSION;
    doc["rssi"] = WiFi.RSSI();
    doc["fallback_runs"] = fbRuns;
    char buf[384];
    size_t n = serializeJson(doc, buf, sizeof(buf));
    if (mqtt.connected()) mqtt.publish(TOPIC_SENSORS, (const uint8_t*)buf, n, false);
}

// ---------------------------------------------------------------------------------------
static void onMqtt(char* topic, byte* payload, unsigned int len) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, len)) return;              // ignore garbage
    if (strcmp(topic, TOPIC_HEARTBEAT) == 0) {
        portENTER_CRITICAL(&coreMux);
        core.heartbeat(millis());
        portEXIT_CRITICAL(&coreMux);
        lastHeartbeatAt = millis();
        heartbeatSeen = true;
        JsonArrayConst zs = doc["z"].as<JsonArrayConst>();      // LCD data (optional)
        if (!zs.isNull()) {
            for (int i = 0; i < jal::N_ZONES && i < (int)zs.size(); ++i) {
                JsonObjectConst z = zs[i];
                jal::lcd::ZoneView& v = piView[i];
                strlcpy(v.crop, z["c"] | "", sizeof v.crop);
                v.moisture = z["m"].isNull() ? -1 : (z["m"] | -1);
                v.lo = z["lo"] | -1;
                v.hi = z["hi"] | -1;
                v.fault = z["f"] | false;
            }
            const char* mode = doc["mode"] | "";
            size_t k = 0;
            for (; k < sizeof piMode - 1 && mode[k]; ++k)         // "auto" -> "AUTO"
                piMode[k] = (char)toupper((unsigned char)mode[k]);
            piMode[k] = '\0';
            piViewAt = millis();
            piViewSeen = true;
        }
        return;
    }
    if (strcmp(topic, TOPIC_CMD) != 0) return;
    if (doc["all_off"] | false) {
        allOff();
        Serial.println("[cmd] all off");
        return;
    }
    int zone = doc["zone"] | 0;
    float seconds = doc["seconds"] | 0.0f;
    bool ok = pumpCommand(zone, seconds);
    Serial.printf("[cmd] zone %d for %.1f s -> %s\n", zone, seconds, ok ? "ok" : "REJECTED");
}

#ifndef MQTT_FALLBACK_IP
#define MQTT_FALLBACK_IP ""
#endif

// Find the Pi. "raspberrypi.local" is looked up with mDNS on every attempt, so the Pi can
// get a different IP from the phone hotspot on another day; MQTT_FALLBACK_IP is the backup.
static IPAddress brokerIp;
static bool resolveBroker() {
    String host = MQTT_HOST;
    if (host.endsWith(".local")) {
        static bool mdnsStarted = false;
        if (!mdnsStarted) mdnsStarted = MDNS.begin(clientId.c_str());
        IPAddress ip = MDNS.queryHost(host.substring(0, host.length() - 6).c_str(), 2000);
        if (ip != IPAddress(0, 0, 0, 0)) {
            brokerIp = ip;
            return true;
        }
        Serial.printf("[mdns] %s not found, trying %s\n", MQTT_HOST, MQTT_FALLBACK_IP);
        return strlen(MQTT_FALLBACK_IP) > 0 && brokerIp.fromString(MQTT_FALLBACK_IP);
    }
    return brokerIp.fromString(MQTT_HOST) || WiFi.hostByName(MQTT_HOST, brokerIp) == 1;
}

static void ensureNetwork(uint32_t now) {
    if (WiFi.status() != WL_CONNECTED) {
        if (now - lastWifiTry > 15000) {
            lastWifiTry = now;
            Serial.printf("[wifi] connecting to %s ...\n", WIFI_SSID);
            WiFi.disconnect();
            WiFi.begin(WIFI_SSID, WIFI_PASS);
        }
        return;
    }
    if (!mqtt.connected() && now - lastMqttTry > MQTT_RETRY_MS) {
        lastMqttTry = now;
        if (!resolveBroker()) {
            Serial.printf("[mqtt] cannot find the Pi (%s)\n", MQTT_HOST);
            return;
        }
        mqtt.setServer(brokerIp, MQTT_PORT);
        Serial.printf("[mqtt] connecting to %s (%s):%d ...\n", MQTT_HOST, brokerIp.toString().c_str(), MQTT_PORT);
        if (mqtt.connect(clientId.c_str(), nullptr, nullptr, TOPIC_STATUS, 1, true, "offline")) {
            mqtt.publish(TOPIC_STATUS, "online", true);
            mqtt.subscribe(TOPIC_CMD, 1);
            mqtt.subscribe(TOPIC_HEARTBEAT, 0);
            Serial.println("[mqtt] connected");
        } else {
            Serial.printf("[mqtt] failed, state %d\n", mqtt.state());
        }
    }
}

static void updateLed(uint32_t now) {
    bool fallback;
    portENTER_CRITICAL(&coreMux);
    fallback = core.mode() == jal::Mode::Fallback;
    portEXIT_CRITICAL(&coreMux);
    bool on;
    if (mqtt.connected() && !fallback) on = true;                        // all good: solid
    else if (WiFi.status() == WL_CONNECTED) on = (now / 500) % 2;        // Wi-Fi only: slow blink
    else on = (now / 125) % 2;                                           // no Wi-Fi: fast blink
    digitalWrite(LED_PIN, on ? HIGH : LOW);
}

// ---- 16x2 LCD ---------------------------------------------------------------------------
static void lcdBegin() {
#if LCD_MODE == LCD_PARALLEL
    lcd.begin(jal::lcd::COLS, 2);
#elif LCD_MODE == LCD_I2C
    Wire.begin(LCD_SDA, LCD_SCL);
    uint8_t addr = 0;
    for (uint8_t a : {0x27, 0x3F}) {                  // the two usual backpack addresses
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) { addr = a; break; }
    }
    if (!addr) { Serial.println("[lcd] no I2C display found at 0x27 / 0x3F"); return; }
    Serial.printf("[lcd] I2C display at 0x%02X\n", addr);
    lcd = new LiquidCrystal_I2C(addr, jal::lcd::COLS, 2);
    lcd->init();
    lcd->backlight();
#endif
}

static void lcdShow(const char* l1, const char* l2) {
    const char* lines[2] = {l1, l2};
    for (int r = 0; r < 2; ++r) {
        if (strcmp(lcdShown[r], lines[r]) == 0) continue;
        strlcpy(lcdShown[r], lines[r], sizeof lcdShown[r]);
#if LCD_MODE == LCD_PARALLEL
        lcd.setCursor(0, r);
        lcd.print(lines[r]);
#elif LCD_MODE == LCD_I2C
        if (lcd) { lcd->setCursor(0, r); lcd->print(lines[r]); }
#endif
    }
}

// Boot: "KisanX" -> "Welcome!"; then rotate: both zones -> zone 1 -> zone 2 -> ...
// While the Pi is fresh the numbers are the Pi's (same as the dashboard); when it is
// silent the ESP32 shows its own reading and "Pi offline".
static void updateLcd(uint32_t now) {
    static uint32_t lastDraw = 0;
    if (now - lastDraw < 250) return;
    lastDraw = now;
    char l1[jal::lcd::COLS + 1], l2[jal::lcd::COLS + 1];
    if (now < LCD_SPLASH_MS) { jal::lcd::splash(l1, l2); lcdShow(l1, l2); return; }
    if (now < 2 * LCD_SPLASH_MS) { jal::lcd::welcome(l1, l2); lcdShow(l1, l2); return; }

    bool on[jal::N_ZONES], fallback;
    portENTER_CRITICAL(&coreMux);
    for (int z = 0; z < jal::N_ZONES; ++z) on[z] = core.pumpOn(z, now);
    fallback = core.mode() == jal::Mode::Fallback;
    portEXIT_CRITICAL(&coreMux);
    bool fresh = piViewSeen && mqtt.connected() && now - piViewAt < HEARTBEAT_TIMEOUT_MS;

    jal::lcd::ZoneView v[jal::N_ZONES];
    for (int z = 0; z < jal::N_ZONES; ++z) {
        v[z] = piView[z];                              // crop name is kept even when stale
        if (!fresh) {
            v[z].moisture = jal::lcd::localPercent(readings[z].raw, readings[z].valid);
            v[z].lo = v[z].hi = -1;
            v[z].fault = false;
        }
        v[z].pumpOn = on[z];                           // the relay itself is the truth
    }
    bool piAlive = heartbeatSeen && mqtt.connected() && now - lastHeartbeatAt < HEARTBEAT_TIMEOUT_MS;
    const char* mode = fallback ? "SAFE" : fresh ? piMode : piAlive ? "ONLINE" : "NO PI";

    int page = ((now - 2 * LCD_SPLASH_MS) / LCD_PAGE_MS) % (jal::N_ZONES + 1);
    if (page == 0) jal::lcd::summaryPage(v, jal::N_ZONES, lastTemp, lastRh, mode, l1, l2);
    else jal::lcd::zonePage(page, v[page - 1], l1, l2);
    lcdShow(l1, l2);
}

// ---- serial console (for hardware day, works without the Pi) ----------------------------
static void printHelp() {
    Serial.println(F("Commands:\n"
                     "  read            raw sensor values (dry soil = HIGH, wet = LOW) + DHT11\n"
                     "  pump <z> <s>    run pump z (1-2) for s seconds\n"
                     "  off             all pumps off\n"
                     "  test            click each relay one after another (2 s each)\n"
                     "  status          Wi-Fi / MQTT / mode / pumps\n"
                     "  lcd             print what the LCD shows now\n"
                     "  power on|off    keep the soil sensors powered (LEDs on) for wiring checks"));
}

static void handleSerial(uint32_t now) {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c != '\n' && c != '\r') {
            if (serialLine.length() < 60) serialLine += c;
            continue;
        }
        String cmd = serialLine;
        serialLine = "";
        cmd.trim();
        if (cmd.isEmpty()) continue;
        if (cmd == "help") printHelp();
        else if (cmd == "read") {
            readSensors();
            for (int z = 0; z < jal::N_ZONES; ++z)
                Serial.printf("  sensor %d (GPIO %d): raw %4d  %s\n", z + 1, SENSOR_PINS[z], readings[z].raw,
                              readings[z].valid ? "ok" : "NOISY / UNPLUGGED?");
            Serial.printf("  DHT: %.1f C, %.0f %%\n", lastTemp, lastRh);
        } else if (cmd.startsWith("pump")) {
            int z = 0;
            float s = 0;
            if (sscanf(cmd.c_str(), "pump %d %f", &z, &s) == 2 && pumpCommand(z, s))
                Serial.printf("  pump %d on for %.1f s\n", z, s);
            else Serial.printf("  usage: pump <1-%d> <seconds>\n", jal::N_ZONES);
        } else if (cmd == "off") { allOff(); Serial.println("  all pumps off"); }
        else if (cmd == "lcd") Serial.printf("  |%s|\n  |%s|\n", lcdShown[0], lcdShown[1]);
        else if (cmd == "test") { relayTestStep = 0; relayTestNext = now; Serial.println("  relay test ..."); }
        else if (cmd == "power on" || cmd == "power off") {
#if SENSOR_POWER_PIN >= 0
            sensorPowerHold = (cmd == "power on");
            digitalWrite(SENSOR_POWER_PIN, sensorPowerHold ? HIGH : LOW);
            Serial.printf("  sensor power (GPIO %d) %s\n", SENSOR_POWER_PIN, sensorPowerHold ? "ON - board LEDs should light" : "back to normal (on only while reading)");
#else
            Serial.println("  sensors are wired to 3V3 (SENSOR_POWER_PIN -1)");
#endif
        }
        else if (cmd == "status") {
            Serial.printf("  Wi-Fi %s  IP %s  RSSI %d\n", WiFi.status() == WL_CONNECTED ? "connected" : "DOWN",
                          WiFi.localIP().toString().c_str(), WiFi.RSSI());
            Serial.printf("  MQTT %s (%s = %s:%d)  mode %s\n", mqtt.connected() ? "connected" : "DOWN", MQTT_HOST,
                          brokerIp.toString().c_str(),
                          MQTT_PORT, core.mode() == jal::Mode::Fallback ? "FALLBACK" : "remote");
            for (int z = 0; z < jal::N_ZONES; ++z)
                Serial.printf("  pump %d: %s\n", z + 1, core.pumpOn(z, now) ? "ON" : "off");
        } else { Serial.printf("  unknown: %s\n", cmd.c_str()); printHelp(); }
    }
}

static void runRelayTest(uint32_t now) {
    if (relayTestStep < 0 || now < relayTestNext) return;
    if (relayTestStep >= jal::N_ZONES) { relayTestStep = -1; Serial.println("  relay test done"); return; }
    Serial.printf("  relay %d (GPIO %d) ON for 2 s\n", relayTestStep + 1, RELAY_PINS[relayTestStep]);
    pumpCommand(relayTestStep + 1, 2.0f);
    relayTestStep++;
    relayTestNext = now + 2500;
}

// ---------------------------------------------------------------------------------------
void setup() {
    for (int i = 0; i < jal::N_ZONES; ++i) {         // relays OFF before they become outputs
        setRelay(i, false);
        pinMode(RELAY_PINS[i], OUTPUT);
        setRelay(i, false);
    }
    pinMode(LED_PIN, OUTPUT);
#if SENSOR_POWER_PIN >= 0
    pinMode(SENSOR_POWER_PIN, OUTPUT);
    digitalWrite(SENSOR_POWER_PIN, LOW);
#endif
    lcdBegin();                                       // "KisanX" as early as possible
    updateLcd(millis());
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n=== Jal-Drishti ESP32 firmware %s ===\n", FW_VERSION);
#if SENSOR_POWER_PIN >= 0
    Serial.printf("soil sensors: AO -> GPIO %d, %d | VCC -> GPIO %d (switched)\n", SENSOR_PINS[0], SENSOR_PINS[1], SENSOR_POWER_PIN);
#endif

    analogReadResolution(12);
    for (int i = 0; i < jal::N_ZONES; ++i) analogSetPinAttenuation(SENSOR_PINS[i], ADC_11db);
#if USE_DHT
    dht.begin();
#endif
    core.begin(millis());
    xTaskCreatePinnedToCore(safetyTask, "safety", 3072, nullptr, 3, nullptr, 1);

    clientId = "jaldrishti-esp32-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                             // steadier MQTT
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    lastWifiTry = millis();
    wifiClient.setTimeout(2);                         // never block the loop for long
    mqtt.setCallback(onMqtt);
    mqtt.setBufferSize(512);
    mqtt.setKeepAlive(15);
    mqtt.setSocketTimeout(2);

    esp_task_wdt_init(15, true);                      // reboot if the loop ever hangs 15 s
    esp_task_wdt_add(nullptr);
    printHelp();
}

void loop() {
    uint32_t now = millis();
    esp_task_wdt_reset();
    ensureNetwork(now);
    if (mqtt.connected()) mqtt.loop();

    if (now - lastPublish >= SENSOR_PERIOD_MS) {
        lastPublish = now;
        readSensors();
        int started;
        portENTER_CRITICAL(&coreMux);
        started = core.fallback(now, readings);
        portEXIT_CRITICAL(&coreMux);
        if (started) Serial.println("[fallback] Pi silent - very dry zone got a short pulse");
        publishReadings(now);
    }
    handleSerial(now);
    runRelayTest(now);
    updateLed(now);
    updateLcd(now);
    delay(5);
}
