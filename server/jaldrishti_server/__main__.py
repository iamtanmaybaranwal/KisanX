"""Start the Jal-Drishti server.

    python -m jaldrishti_server --sim                 laptop demo: virtual ESP32 + farm, no hardware
    python -m jaldrishti_server --mqtt localhost      Pi: real ESP32 via the Mosquitto broker
    python -m jaldrishti_server --mqtt localhost --broker
                                                      laptop: start a built-in MQTT broker too
                                                      (then run the fake ESP32:
                                                       python -m jaldrishti_server.simdevice)
Open http://localhost:8000 (on the Pi hotspot: http://192.168.4.1).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import uvicorn

from . import settings
from .app import create_app
from .controller import Controller
from .db import Database
from .link import MqttLink
from .simdevice import SimFarm, SimLink
from .weather_service import WeatherService


async def start_broker(port: int):
    from amqtt.broker import Broker
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"0.0.0.0:{port}"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    return broker


def main():
    ap = argparse.ArgumentParser(description="Jal-Drishti Pi server")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sim", action="store_true", help="use the built-in simulated ESP32 + farm")
    src.add_argument("--mqtt", metavar="HOST[:PORT]", help="talk to the ESP32 through this MQTT broker")
    ap.add_argument("--broker", action="store_true", help="also start a built-in MQTT broker (laptop testing)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=str(settings.DB_PATH))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db = Database(args.db)
    weather = WeatherService(db)
    now = time.time()
    if args.sim:
        link = SimLink(SimFarm(now))
    else:
        host, _, port = args.mqtt.partition(":")
        mqtt_port = int(port or 1883)
        if args.broker:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(start_broker(mqtt_port))
            import threading
            threading.Thread(target=loop.run_forever, daemon=True).start()
        link = MqttLink(host, mqtt_port)
    controller = Controller(db, link, weather, now=now)
    app = create_app(controller)
    logging.info("Jal-Drishti on http://%s:%d  (link: %s)", args.host, args.port, link.kind)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
