// Unit tests for the safety-critical logic. Run on the laptop: pio test -e native
#include <unity.h>

#include "JalCore.h"

using namespace jal;

static Reading dry(int raw = 3000) { return {raw, true}; }
static Reading wet() { return {1500, true}; }

void setUp() {}
void tearDown() {}

void test_pump_runs_for_commanded_time() {
    Core c;
    c.begin(0);
    TEST_ASSERT_TRUE(c.commandPump(2, 5.0f, 1000));
    c.tick(1000);
    TEST_ASSERT_TRUE(c.pumpOn(1, 1000));
    c.tick(5999);
    TEST_ASSERT_TRUE(c.pumpOn(1, 5999));
    c.tick(6000);
    TEST_ASSERT_FALSE(c.pumpOn(1, 6000));
    TEST_ASSERT_FALSE(c.pumpOn(0, 3000));            // the other zone untouched
}

void test_command_capped_at_max() {
    Core c;
    c.begin(0);
    c.commandPump(1, 9999.0f, 0);
    TEST_ASSERT_EQUAL_UINT32(120000, c.remainingMs(0, 0));
    c.tick(120000);
    TEST_ASSERT_FALSE(c.pumpOn(0, 120000));
}

void test_invalid_commands_rejected() {
    Core c;
    c.begin(0);
    TEST_ASSERT_FALSE(c.commandPump(0, 5, 0));
    TEST_ASSERT_FALSE(c.commandPump(N_ZONES + 1, 5, 0));
    TEST_ASSERT_FALSE(c.commandPump(-1, 5, 0));
    float nan = 0.0f / 0.0f;
    TEST_ASSERT_FALSE(c.commandPump(1, nan, 0));
    for (int i = 0; i < N_ZONES; ++i) TEST_ASSERT_FALSE(c.pumpOn(i, 0));
}

void test_zero_seconds_stops_and_all_off() {
    Core c;
    c.begin(0);
    c.commandPump(1, 30, 0);
    c.commandPump(2, 30, 0);
    c.commandPump(1, 0, 100);
    TEST_ASSERT_FALSE(c.pumpOn(0, 100));
    TEST_ASSERT_TRUE(c.pumpOn(1, 100));
    c.commandAllOff();
    TEST_ASSERT_FALSE(c.pumpOn(1, 100));
}

void test_watchdog_stops_pumps_when_pi_silent() {
    Core c;
    c.begin(0);
    c.commandPump(1, 100, 1000);                      // heartbeat at 1000 via the command
    c.tick(31000);
    TEST_ASSERT_TRUE(c.pumpOn(0, 31000));
    TEST_ASSERT_TRUE(c.mode() == Mode::Remote);
    c.tick(31001);                                    // > 30 s silent
    TEST_ASSERT_TRUE(c.mode() == Mode::Fallback);
    TEST_ASSERT_FALSE(c.pumpOn(0, 31001));
    c.heartbeat(40000);                               // Pi is back
    TEST_ASSERT_TRUE(c.mode() == Mode::Remote);
}

void test_fallback_only_waters_very_dry_once_per_gap() {
    Config cfg;
    cfg.fallbackMinGapMs = 3600000;
    Core c(cfg);
    c.begin(0);
    c.tick(31000);
    TEST_ASSERT_TRUE(c.mode() == Mode::Fallback);
    Reading r[N_ZONES] = {{3000, false}, dry()};      // zone 1 sensor unplugged, zone 2 very dry
    TEST_ASSERT_EQUAL_INT(1, c.fallback(31000, r));
    TEST_ASSERT_TRUE(c.pumpOn(1, 31000));
    TEST_ASSERT_FALSE(c.pumpOn(0, 31000));            // never water on a bad sensor
    c.tick(39000);
    TEST_ASSERT_FALSE(c.pumpOn(1, 39000));            // 8 s pulse over
    TEST_ASSERT_EQUAL_INT(0, c.fallback(60000, r));   // gap not over yet
    TEST_ASSERT_EQUAL_INT(1, c.fallback(31000 + 3600000, r));
    TEST_ASSERT_EQUAL_UINT32(2, c.fallbackRuns());
}

void test_fallback_skips_wet_zones() {
    Core c;
    c.begin(0);
    c.tick(31000);
    Reading r[N_ZONES] = {wet(), wet()};
    TEST_ASSERT_EQUAL_INT(0, c.fallback(31000, r));
    TEST_ASSERT_FALSE(c.pumpOn(0, 31000));
    TEST_ASSERT_FALSE(c.pumpOn(1, 31000));
}

void test_fallback_disabled_in_remote_mode() {
    Core c;
    c.begin(0);
    Reading r[N_ZONES] = {dry(), dry()};
    TEST_ASSERT_EQUAL_INT(0, c.fallback(1000, r));    // the Pi is in charge
}

void test_one_fallback_zone_at_a_time() {
    Core c;
    c.begin(0);
    c.tick(31000);
    Reading r[N_ZONES] = {dry(), dry()};
    TEST_ASSERT_EQUAL_INT(1, c.fallback(31000, r));
    TEST_ASSERT_EQUAL_INT(0, c.fallback(32000, r));   // zone 1 still running
    c.tick(39000);
    TEST_ASSERT_EQUAL_INT(1, c.fallback(39000, r));   // next zone
    TEST_ASSERT_TRUE(c.pumpOn(1, 39000));
}

void test_millis_wraparound() {
    Core c;
    uint32_t t = 0xFFFFFFFFu - 2000;                  // 2 s before millis() wraps (day 49)
    c.begin(t);
    c.commandPump(1, 5, t);
    c.tick(t + 4000);                                  // wrapped
    TEST_ASSERT_TRUE(c.pumpOn(0, t + 4000));
    c.tick(t + 5000);
    TEST_ASSERT_FALSE(c.pumpOn(0, t + 5000));
    TEST_ASSERT_TRUE(c.mode() == Mode::Remote);
}

void test_summarize_median_and_noise() {
    int steady[16] = {2001, 1999, 2000, 2003, 1998, 2000, 2002, 2001, 1997, 2000, 2004, 1999, 2000, 2001, 2002, 2000};
    Reading r = summarize(steady, 16, 400);
    TEST_ASSERT_TRUE(r.valid);
    TEST_ASSERT_INT_WITHIN(2, 2000, r.raw);
    int spike[16] = {2000, 2000, 4095, 2001, 1999, 2000, 0, 2000, 2001, 1999, 2000, 2000, 2002, 1998, 2000, 2001};
    r = summarize(spike, 16, 400);                    // single spikes ignored
    TEST_ASSERT_TRUE(r.valid);
    TEST_ASSERT_INT_WITHIN(2, 2000, r.raw);
    int floating[16] = {0, 4095, 312, 3900, 1200, 50, 2800, 4095, 0, 2200, 900, 3500, 100, 4000, 1800, 600};
    TEST_ASSERT_FALSE(summarize(floating, 16, 400).valid);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_pump_runs_for_commanded_time);
    RUN_TEST(test_command_capped_at_max);
    RUN_TEST(test_invalid_commands_rejected);
    RUN_TEST(test_zero_seconds_stops_and_all_off);
    RUN_TEST(test_watchdog_stops_pumps_when_pi_silent);
    RUN_TEST(test_fallback_only_waters_very_dry_once_per_gap);
    RUN_TEST(test_fallback_skips_wet_zones);
    RUN_TEST(test_fallback_disabled_in_remote_mode);
    RUN_TEST(test_one_fallback_zone_at_a_time);
    RUN_TEST(test_millis_wraparound);
    RUN_TEST(test_summarize_median_and_noise);
    return UNITY_END();
}
