// What the 16x2 LCD shows - exact characters. Run on the laptop: pio test -e native
#include <math.h>
#include <string.h>
#include <unity.h>

#include "LcdText.h"

using namespace jal::lcd;

static char l1[COLS + 1], l2[COLS + 1];

void setUp() { memset(l1, 'x', sizeof l1); memset(l2, 'x', sizeof l2); }
void tearDown() {}

static void assertLen16() {
    TEST_ASSERT_EQUAL(16, (int)strlen(l1));
    TEST_ASSERT_EQUAL(16, (int)strlen(l2));
}

static ZoneView zone(const char* crop, int m, int lo, int hi, bool pump = false) {
    ZoneView z;
    strncpy(z.crop, crop, sizeof z.crop - 1);
    z.moisture = m;
    z.lo = lo;
    z.hi = hi;
    z.pumpOn = pump;
    return z;
}

void test_boot_screens() {
    splash(l1, l2);
    TEST_ASSERT_EQUAL_STRING("     KisanX     ", l1);
    TEST_ASSERT_EQUAL_STRING("Smart Irrigation", l2);
    welcome(l1, l2);
    assertLen16();
    TEST_ASSERT_EQUAL_STRING("    Welcome!    ", l1);
}

void test_zone_page() {
    zonePage(1, zone("Tomato", 45, 36, 62), l1, l2);
    TEST_ASSERT_EQUAL_STRING("Z1 Tomato    45%", l1);
    TEST_ASSERT_EQUAL_STRING("Ideal 36-62% OFF", l2);
    zonePage(2, zone("Cauliflower", 100, 48, 100, true), l1, l2);   // long name, 100 %
    TEST_ASSERT_EQUAL_STRING("Z2 Cauliflo 100%", l1);
    TEST_ASSERT_EQUAL_STRING("48-100%       ON", l2);
}

void test_zone_page_missing_data() {
    zonePage(1, zone("", -1, -1, -1), l1, l2);
    TEST_ASSERT_EQUAL_STRING("Z1 Moisture  --%", l1);
    TEST_ASSERT_EQUAL_STRING("No reading   OFF", l2);
    zonePage(1, zone("Wheat", 30, -1, -1), l1, l2);
    TEST_ASSERT_EQUAL_STRING("Pi offline   OFF", l2);
    ZoneView f = zone("Wheat", 30, 29, 45, false);
    f.fault = true;
    zonePage(1, f, l1, l2);
    TEST_ASSERT_EQUAL_STRING("CHECK TANK/PIPE!", l2);
}

void test_summary_page() {
    ZoneView zs[2] = {zone("Tomato", 45, 36, 62, true), zone("Wheat", 38, 29, 45)};
    summaryPage(zs, 2, 31.4f, 48.0f, "AUTO", l1, l2);
    TEST_ASSERT_EQUAL_STRING("Z1 45%* Z2 38%  ", l1);
    TEST_ASSERT_EQUAL_STRING("31\xDF" "C H:48%  AUTO", l2);
    summaryPage(zs, 2, NAN, NAN, "MANUAL", l1, l2);
    TEST_ASSERT_EQUAL_STRING("--\xDF" "C H:--%MANUAL", l2);
    zs[0].moisture = 100;
    zs[1].moisture = -1;
    summaryPage(zs, 2, -12.0f, 100.0f, "NO PI", l1, l2);
    assertLen16();
    TEST_ASSERT_EQUAL_STRING("Z1 100%*Z2 --%  ", l1);
}

void test_local_percent_matches_the_pi_scale() {
    TEST_ASSERT_EQUAL(0, localPercent(4095, true));
    TEST_ASSERT_EQUAL(0, localPercent(4000, true));
    TEST_ASSERT_EQUAL(50, localPercent(2200, true));
    TEST_ASSERT_EQUAL(100, localPercent(400, true));
    TEST_ASSERT_EQUAL(100, localPercent(250, true));    // soaked fork reads ~220-400
    TEST_ASSERT_EQUAL(-1, localPercent(100, true));     // board without power
    TEST_ASSERT_EQUAL(-1, localPercent(2000, false));   // unplugged / noisy
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_boot_screens);
    RUN_TEST(test_zone_page);
    RUN_TEST(test_zone_page_missing_data);
    RUN_TEST(test_summary_page);
    RUN_TEST(test_local_percent_matches_the_pi_scale);
    return UNITY_END();
}
