"""End-to-end over a REAL MQTT broker: Pi-side MqttLink <-> fake ESP32 speaking the
exact firmware protocol. Proves the message formats and the safety watchdog."""
import asyncio
import json
import socket
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from jaldrishti_server.link import TOPIC_CMD, TOPIC_HEARTBEAT, TOPIC_SENSORS, MqttLink
from jaldrishti_server.simdevice import SimFarm


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def broker_port():
    from jaldrishti_server.__main__ import start_broker
    port = free_port()
    loop = asyncio.new_event_loop()
    broker = loop.run_until_complete(start_broker(port))
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield port
    asyncio.run_coroutine_threadsafe(broker.shutdown(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)


class FakeEsp32:
    def __init__(self, port):
        self.farm = SimFarm(time.time())
        self.cmds = []
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-esp32")
        self.c.on_connect = lambda c, u, f, rc, p=None: c.subscribe([(TOPIC_CMD, 1), (TOPIC_HEARTBEAT, 0)])
        self.c.on_message = self.on_message
        self.c.connect("127.0.0.1", port)
        self.c.loop_start()

    def on_message(self, c, u, msg):
        data = json.loads(msg.payload)
        if msg.topic == TOPIC_CMD:
            self.cmds.append(data)
            self.farm.command(data)
        else:
            self.farm.heartbeat(time.time())

    def publish(self):
        self.farm.advance(time.time())
        self.c.publish(TOPIC_SENSORS, json.dumps(self.farm.sensor_message()))


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_sensor_data_and_pump_commands_round_trip(broker_port):
    esp = FakeEsp32(broker_port)
    link = MqttLink("127.0.0.1", broker_port, client_id="test-pi")
    assert wait_for(lambda: link.connected)
    time.sleep(0.3)
    got = []
    esp.publish()
    assert wait_for(lambda: got.extend(link.poll(time.time())) or got)
    msg = got[-1]
    assert len(msg["raw"]) == 2 and msg["mode"] == "remote" and "pumps" in msg

    link.send_heartbeat(time.time())
    link.send_pump(2, 2.5, 1)
    assert wait_for(lambda: any(c.get("zone") == 2 for c in esp.cmds))
    esp.farm.advance(time.time())
    assert esp.farm.pump_until[1] > esp.farm.t                    # zone 2 pump is on
    link.send_all_off()
    assert wait_for(lambda: any(c.get("all_off") for c in esp.cmds))
    esp.farm.advance(time.time())
    assert not (esp.farm.pump_until > esp.farm.t).any()
    link.close()
    esp.c.loop_stop()


def test_garbage_messages_do_not_crash(broker_port):
    link = MqttLink("127.0.0.1", broker_port, client_id="test-pi-2")
    assert wait_for(lambda: link.connected)
    time.sleep(0.3)
    pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="noise")
    pub.connect("127.0.0.1", broker_port)
    pub.loop_start()
    pub.publish(TOPIC_SENSORS, b"\xff\xfe not json").wait_for_publish()
    pub.publish(TOPIC_SENSORS, json.dumps({"raw": [1, 2, 3, 4]})).wait_for_publish()
    got = []
    assert wait_for(lambda: got.extend(link.poll(0)) or got)
    assert got == [{"raw": [1, 2, 3, 4]}]
    link.close()
    pub.loop_stop()


def test_esp32_watchdog_stops_pumps_without_heartbeat():
    farm = SimFarm(0.0)
    farm.heartbeat(0.0)
    farm.command({"zone": 1, "seconds": 100})
    farm.advance(20)
    assert farm.pump_until[0] > farm.t
    farm.advance(40)                                  # Pi silent for > 30 s
    assert not (farm.pump_until > farm.t).any() and farm.mode == "fallback"
    farm.command({"zone": 2, "seconds": 500})          # over-long command is capped at 120 s
    assert farm.pump_until[1] - farm.t == pytest.approx(120)
