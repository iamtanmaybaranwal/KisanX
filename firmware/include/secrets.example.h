// Network settings - copy this file to secrets.h and fill in your Wi-Fi.
// secrets.h is kept out of git so the password is never committed.
#pragma once

#define WIFI_SSID "YOUR-WIFI"
#define WIFI_PASS "YOUR-PASSWORD"
// The Pi is found by name (mDNS), so a changed IP on the hotspot does not matter.
#define MQTT_HOST "raspberrypi.local"
#define MQTT_FALLBACK_IP "192.168.1.50"   // used only if the name lookup fails
#define MQTT_PORT 1883
