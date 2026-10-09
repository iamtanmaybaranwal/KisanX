// Jal-Drishti core logic - NO Arduino dependencies, so it is unit-tested on the laptop
// (pio test -e native). Everything that can flood a farm lives here:
//   * every pump run is timed and capped (default 120 s)
//   * no heartbeat from the Pi for 30 s  -> all pumps OFF, FALLBACK mode
//   * FALLBACK: only a very dry zone gets a short pulse, at most every few hours
// All times are millis()-style uint32_t and survive the 49-day wrap-around.
#pragma once
#include <stdint.h>

namespace jal {

constexpr int N_ZONES = 2;   // table-top demo farm: 2 zones

struct Config {
    uint32_t maxPumpMs = 120000;          // hard cap per command
    uint32_t heartbeatTimeoutMs = 30000;  // Pi silent this long -> fallback
    bool fallbackEnabled = true;
    int fallbackDryRaw = 2700;            // raw ADC above this = very dry (higher raw = drier)
    uint32_t fallbackPulseMs = 8000;
    uint32_t fallbackMinGapMs = 4UL * 3600UL * 1000UL;
    int noiseSpreadMax = 400;             // samples spread more than this -> sensor unplugged
};

enum class Mode { Remote, Fallback };

struct Reading {
    int raw;      // median of the samples
    bool valid;   // false if the samples are too noisy (floating / unplugged pin)
};

// Median + plausibility of one burst of ADC samples (sorts `samples` in place).
Reading summarize(int* samples, int n, int noiseSpreadMax);

class Core {
public:
    explicit Core(const Config& cfg = Config()) : cfg_(cfg) {}

    void begin(uint32_t now);

    // ---- from the Pi (MQTT) ---------------------------------------------------
    // zone is 1-based. seconds <= 0 stops the pump. Returns false if rejected.
    bool commandPump(int zone, float seconds, uint32_t now);
    void commandAllOff();
    void heartbeat(uint32_t now);

    // ---- periodic -------------------------------------------------------------------
    // Safety tick (expiry + watchdog). Call often (every 50 ms) from the safety task.
    void tick(uint32_t now);
    // Fallback irrigation; call with fresh readings (every 2 s). Returns zones started.
    int fallback(uint32_t now, const Reading readings[N_ZONES]);

    // ---- state ------------------------------------------------------------------------
    bool pumpOn(int idx, uint32_t now) const;   // idx is 0-based
    Mode mode() const { return mode_; }
    uint32_t remainingMs(int idx, uint32_t now) const;
    uint32_t fallbackRuns() const { return fallbackRuns_; }
    const Config& config() const { return cfg_; }

private:
    void startPump(int idx, uint32_t durMs, uint32_t now);
    Config cfg_;
    bool active_[N_ZONES] = {};
    uint32_t start_[N_ZONES] = {};
    uint32_t dur_[N_ZONES] = {};
    bool everFallback_[N_ZONES] = {};
    uint32_t lastFallback_[N_ZONES] = {};
    uint32_t lastHeartbeat_ = 0;
    uint32_t fallbackRuns_ = 0;
    Mode mode_ = Mode::Remote;
};

}  // namespace jal
