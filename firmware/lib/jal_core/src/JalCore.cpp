#include "JalCore.h"

namespace jal {

Reading summarize(int* s, int n, int noiseSpreadMax) {
    if (n <= 0) return {0, false};
    for (int i = 1; i < n; ++i) {           // insertion sort: n is small (16)
        int v = s[i], j = i - 1;
        while (j >= 0 && s[j] > v) { s[j + 1] = s[j]; --j; }
        s[j + 1] = v;
    }
    int median = (n % 2) ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
    // spread of the middle half ignores single spikes but catches a floating pin
    int lo = s[n / 4], hi = s[(3 * n) / 4 - (n >= 4 ? 1 : 0)];
    return {median, (hi - lo) <= noiseSpreadMax};
}

void Core::begin(uint32_t now) {
    lastHeartbeat_ = now;
    mode_ = Mode::Remote;
    commandAllOff();
}

void Core::startPump(int idx, uint32_t durMs, uint32_t now) {
    if (durMs > cfg_.maxPumpMs) durMs = cfg_.maxPumpMs;
    active_[idx] = durMs > 0;
    start_[idx] = now;
    dur_[idx] = durMs;
}

bool Core::commandPump(int zone, float seconds, uint32_t now) {
    if (zone < 1 || zone > N_ZONES) return false;
    if (!(seconds == seconds)) return false;          // NaN
    heartbeat(now);                                    // a command proves the Pi is alive
    if (seconds <= 0.0f) {
        active_[zone - 1] = false;
        return true;
    }
    float ms = seconds * 1000.0f;
    uint32_t dur = ms >= (float)cfg_.maxPumpMs ? cfg_.maxPumpMs : (uint32_t)ms;
    startPump(zone - 1, dur, now);
    return true;
}

void Core::commandAllOff() {
    for (int i = 0; i < N_ZONES; ++i) active_[i] = false;
}

void Core::heartbeat(uint32_t now) {
    lastHeartbeat_ = now;
    if (mode_ == Mode::Fallback) {
        mode_ = Mode::Remote;
        commandAllOff();                               // the Pi decides again
    }
}

void Core::tick(uint32_t now) {
    for (int i = 0; i < N_ZONES; ++i)
        if (active_[i] && (uint32_t)(now - start_[i]) >= dur_[i]) active_[i] = false;
    if (mode_ == Mode::Remote && (uint32_t)(now - lastHeartbeat_) > cfg_.heartbeatTimeoutMs) {
        mode_ = Mode::Fallback;
        commandAllOff();
    }
}

int Core::fallback(uint32_t now, const Reading readings[N_ZONES]) {
    if (mode_ != Mode::Fallback || !cfg_.fallbackEnabled) return 0;
    for (int i = 0; i < N_ZONES; ++i)                   // one pump at a time (power supply)
        if (pumpOn(i, now)) return 0;
    int started = 0;
    for (int i = 0; i < N_ZONES; ++i) {
        if (!readings[i].valid) continue;
        if (readings[i].raw <= cfg_.fallbackDryRaw) continue;
        if (everFallback_[i] && (uint32_t)(now - lastFallback_[i]) < cfg_.fallbackMinGapMs) continue;
        startPump(i, cfg_.fallbackPulseMs, now);
        everFallback_[i] = true;
        lastFallback_[i] = now;
        ++fallbackRuns_;
        ++started;
        break;                                          // one zone at a time (power)
    }
    return started;
}

bool Core::pumpOn(int idx, uint32_t now) const {
    if (idx < 0 || idx >= N_ZONES || !active_[idx]) return false;
    return (uint32_t)(now - start_[idx]) < dur_[idx];
}

uint32_t Core::remainingMs(int idx, uint32_t now) const {
    if (!pumpOn(idx, now)) return 0;
    return dur_[idx] - (uint32_t)(now - start_[idx]);
}

}  // namespace jal
