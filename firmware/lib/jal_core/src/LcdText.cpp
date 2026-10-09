#include "LcdText.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

namespace jal {
namespace lcd {

namespace {

const char DEGREE = (char)0xDF;   // the HD44780 character ROM has "°" at 0xDF

// Copy s into out, cut or padded with spaces to exactly COLS characters.
void fit(char* out, const char* s) {
    int n = 0;
    while (n < COLS && s[n]) { out[n] = s[n]; ++n; }
    while (n < COLS) out[n++] = ' ';
    out[COLS] = '\0';
}

void centre(char* out, const char* s) {
    char buf[COLS + 1];
    int len = (int)strlen(s);
    if (len > COLS) len = COLS;
    int pad = (COLS - len) / 2;
    memset(buf, ' ', pad);
    memcpy(buf + pad, s, len);
    buf[pad + len] = '\0';
    fit(out, buf);
}

int clampPct(int v) { return v < 0 ? 0 : v > 100 ? 100 : v; }

void pct(char* out, size_t size, int m) {
    if (m < 0) snprintf(out, size, "--%%");
    else snprintf(out, size, "%d%%", clampPct(m));
}

}  // namespace

int localPercent(int raw, bool valid) {
    const int dry = 4000, wet = 400, noPower = 180;
    if (!valid || raw < noPower) return -1;
    float p = 100.0f * (dry - raw) / (dry - wet);
    if (p < 0) p = 0;
    if (p > 100) p = 100;
    return (int)lroundf(p);
}

void splash(char l1[COLS + 1], char l2[COLS + 1]) {
    centre(l1, "KisanX");
    centre(l2, "Smart Irrigation");
}

void welcome(char l1[COLS + 1], char l2[COLS + 1]) {
    centre(l1, "Welcome!");
    centre(l2, "Starting up...");
}

void zonePage(int zoneNo, const ZoneView& z, char l1[COLS + 1], char l2[COLS + 1]) {
    char m[16], buf[48];
    pct(m, sizeof m, z.moisture);
    // "Z1 " + crop (8 wide) + " " + moisture (4 wide, right-aligned) = 16
    snprintf(buf, sizeof buf, "Z%d %-8.8s %4s", zoneNo, z.crop[0] ? z.crop : "Moisture", m);
    fit(l1, buf);

    const char* pump = z.pumpOn ? "ON" : "OFF";
    char left[32];
    if (z.fault) {
        fit(l2, "CHECK TANK/PIPE!");
        return;
    }
    if (z.moisture < 0) snprintf(left, sizeof left, "No reading");
    else if (z.lo < 0 || z.hi < 0) snprintf(left, sizeof left, "Pi offline");
    else {
        int lo = clampPct(z.lo), hi = clampPct(z.hi);
        snprintf(left, sizeof left, "Ideal %d-%d%%", lo, hi);
        if (strlen(left) > 12) snprintf(left, sizeof left, "%d-%d%%", lo, hi);   // "48-100%"
    }
    snprintf(buf, sizeof buf, "%-12.12s%4s", left, pump);
    fit(l2, buf);
}

void summaryPage(const ZoneView* zones, int n, float tempC, float rh, const char* mode,
                 char l1[COLS + 1], char l2[COLS + 1]) {
    char buf[48] = "", seg[24], m[16];
    for (int i = 0; i < n && i < 2; ++i) {          // two zones fit on one line
        pct(m, sizeof m, zones[i].moisture);
        snprintf(seg, sizeof seg, "Z%d %s%s", i + 1, m, zones[i].pumpOn ? "*" : "");
        snprintf(buf + strlen(buf), sizeof buf - strlen(buf), "%-8s", seg);
    }
    fit(l1, buf);

    char t[16], h[16];
    if (isnan(tempC)) snprintf(t, sizeof t, "--");
    else snprintf(t, sizeof t, "%d", (int)lroundf(tempC < -9 ? -9 : tempC > 99 ? 99 : tempC));
    if (isnan(rh)) snprintf(h, sizeof h, "--");
    else snprintf(h, sizeof h, "%d", (int)lroundf(rh < 0 ? 0 : rh > 99 ? 99 : rh));
    // "31°C H:48%" (10) + mode right-aligned (6) = 16
    snprintf(buf, sizeof buf, "%s%cC H:%s%%%6.6s", t, DEGREE, h, mode ? mode : "");
    fit(l2, buf);
}

}  // namespace lcd
}  // namespace jal
