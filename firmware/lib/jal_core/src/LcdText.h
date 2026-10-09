// Text for the 16x2 LCD (JHD162A / any HD44780). Pure C++ (no Arduino), so the exact
// characters on screen are unit-tested on the laptop: pio test -e native
//
//   boot      "     KisanX     "    then   "    Welcome!    "
//             "Smart Irrigation"           " Starting up... "
//   summary   "Z1 45%* Z2 38%  "    * = pump running
//             "31°C H:48%  AUTO"    DHT11 + mode (AUTO / MANUAL / SAFE / NO PI)
//   zone      "Z1 Tomato    45%"
//             "Ideal 36-62%  ON"    pump ON / OFF; "Pi offline" when the Pi is silent
#pragma once

namespace jal {
namespace lcd {

constexpr int COLS = 16;

struct ZoneView {
    char crop[11] = "";    // short crop name from the Pi; "" = not known yet
    int moisture = -1;     // %, -1 = no reading
    int lo = -1, hi = -1;  // ideal range from the Pi; -1 = unknown (Pi silent)
    bool pumpOn = false;
    bool fault = false;    // Pi says: pump ran but moisture did not rise (tank / pipe)
};

// Moisture % from the raw ADC, used only while the Pi is silent. Same fixed scale as the
// Pi (server settings DEFAULT_RAW_DRY/WET: 4000 = 0 %, 400 = 100 %); below 180 = the
// sensor board has no power -> -1.
int localPercent(int raw, bool valid);

// Each fills two lines of exactly 16 characters (+ terminating 0), padded with spaces.
void splash(char l1[COLS + 1], char l2[COLS + 1]);
void welcome(char l1[COLS + 1], char l2[COLS + 1]);
void zonePage(int zoneNo, const ZoneView& z, char l1[COLS + 1], char l2[COLS + 1]);
// tempC / rh: NaN when the DHT11 has no reading. mode: "AUTO", "MANUAL", "SAFE", "NO PI".
void summaryPage(const ZoneView* zones, int n, float tempC, float rh, const char* mode,
                 char l1[COLS + 1], char l2[COLS + 1]);

}  // namespace lcd
}  // namespace jal
