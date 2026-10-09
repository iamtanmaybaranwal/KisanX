# Jal-Drishti ESP32 firmware (PlatformIO)

The ESP32 is "the hands": it reads 2 resistive soil sensors (+ DHT11), switches 2 pumps through a
relay board, and talks to the Raspberry Pi over Wi-Fi/MQTT. **Safety runs on the ESP32
itself**, independent of Wi-Fi: every pump run is timed (max 120 s), and if the Pi is
silent for 30 s all pumps stop and the ESP32 switches to a cautious fallback mode.

## Wiring (ESP32 DevKit V1) - 2 zones

```
 SOIL SENSORS - resistive: metal fork + small blue board (pins VCC, GND, DO, AO)
   fork -> blue board: the 2-wire cable (either way round)
   blue board VCC -> D27   (BOTH sensors' VCC on D27: powered only while reading)
   blue board GND -> GND
   zone 1 board AO -> D32
   zone 2 board AO -> D33
   DO  -> not connected (the knob only affects DO; ignore it)

 DHT11 (3-pin module)
   + (VCC) -> 3V3   - (GND) -> GND   S / out -> D4

 RELAY BOARD (2-channel, 5 V)
   VCC -> VIN (5 V from USB)   GND -> GND
   IN1 <- D25   (pump zone 1)
   IN2 <- D26   (pump zone 2)

 PUMPS - on their OWN 5 V supply, never powered from the ESP32:
   supply +5V -> relay COM1 and COM2 (join them with a short wire)
   relay NO1 -> pump 1 (+)        relay NO2 -> pump 2 (+)
   pump 1 (-) and pump 2 (-) -> supply (-)

 16x2 LCD without I2C board (set LCD_MODE LCD_PARALLEL) - 16 pins, numbered from the left
   1 VSS -> GND        2 VDD -> VIN (5 V)
   3 V0  -> middle leg of a 10k pot (outer legs to 5 V and GND) = contrast
            no pot? 1k-2.2k resistor from V0 to GND
   4 RS  -> D13        5 RW  -> GND (write only - the LCD never drives 5 V into the ESP32)
   6 E   -> D14        7-10 D0-D3 -> not connected
  11 D4  -> D16 (RX2) 12 D5 -> D17 (TX2)   13 D6 -> D18   14 D7 -> D19
  15 A   -> 5 V through a 220 ohm resistor (backlight +)    16 K -> GND
 LCD with an I2C board on the back (4 pins) - THIS IS THE ONE WE USE (LCD_MODE LCD_I2C):
   GND -> GND   VCC -> VIN (5 V)   SDA -> D21   SCL -> D22
   contrast = the blue screw-pot on the I2C board; address 0x27 / 0x3F is found automatically
```
Why D27 for the sensor VCC: a resistive probe corrodes when it is powered all the time;
the firmware switches it on for 0.1 s per reading. (Capacitive sensors also work: set
`SENSOR_POWER_PIN -1` in `include/config.h` and wire VCC to 3V3.)
Sensor pins must be ADC1 (GPIO 32-39) because ADC2 stops working with Wi-Fi.
If pumps run when they should be off → set `RELAY_ACTIVE_LOW false` in `include/config.h`.

## Build, test, flash (VS Code + PlatformIO, or terminal)

```
cd firmware
pio test -e native                 # 11 safety-logic tests on the laptop (no board needed)
pio run -e esp32dev                # build
pio run -e esp32dev -t upload      # flash - ESP32 on USB (DATA cable, not charge-only)
pio device monitor                 # serial console, 115200 baud
```
Upload stuck at "Connecting....."? Hold the **BOOT** button on the ESP32 until it starts.

Wi-Fi: `include/secrets.h` (not in git; template `include/secrets.example.h`). The Pi is
found by name, `raspberrypi.local` (mDNS), with `MQTT_FALLBACK_IP` as a backup.
The ESP32 only joins **2.4 GHz** Wi-Fi.

## Serial console (hardware day - works without the Pi)
| Command | Use |
|---|---|
| `read` | raw value of each sensor (resistive fork): **in air / dry soil ≈ 3500-4095, in water ≈ 1000-1800**; "NOISY / UNPLUGGED?" = check that wire |
| `test` | clicks relay 1 then 2, 2 s each - check each pump waters the right zone |
| `pump 2 5` | run pump 2 for 5 s |
| `off` | all pumps off |
| `status` | Wi-Fi, MQTT, mode, pumps |
| `lcd` | prints what the LCD shows (check the text without the screen) |

## LCD (16x2)
Power-up: **KisanX** → **Welcome!** (2.5 s each), then every 3 s:
`Z1 45%* Z2 38%` / `31°C H:48%  AUTO` → `Z1 Tomato    45%` / `Ideal 36-62%  ON` → zone 2.
`*` = pump running. Numbers and the ideal range come from the Pi (same as the dashboard);
if the Pi is silent the ESP32 shows its own reading and "Pi offline". Blank screen or a row
of boxes = turn the contrast pot.

## LED
Solid = connected to the Pi · slow blink = Wi-Fi OK, no Pi · fast blink = no Wi-Fi.

## Files
* `src/main.cpp` - Wi-Fi, MQTT, sensors, relays, serial console
* `lib/jal_core/` - pump timers, watchdog, fallback, sensor median (no Arduino code →
  unit-tested on the laptop)
* `include/config.h` - pins and timing · `include/secrets.h` - Wi-Fi + Pi address
* `test/test_core/` - unit tests
