import logging
import sys
import yaml
import json
import argparse
import threading
import asyncio
from pathlib import Path
import time
import paho.mqtt.client as mqtt
from _login import get_account_sync
from findmy import FindMyAccessory
from findmy.reports import RemoteAnisetteProvider
from findmy.scanner import OfflineFindingScanner
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.DEBUG)

LAST_UPDATE_FILE = "last_update.json"

# Apple doesn't expose an exact battery percentage for AirTags over the
# Find My cloud network (the official app doesn't show one either) -- only
# a coarse 2-bit level packed into the top of the status byte carried by
# every location report. This is the same status byte / bit layout the
# findmy library's Bluetooth scanner module decodes locally, so it works
# just as well on cloud-fetched reports.
BATTERY_LEVELS = {0b00: "Full", 0b01: "Medium", 0b10: "Low", 0b11: "Very Low"}

def get_battery_level(status: int) -> str:
    battery_id = (status >> 6) & 0b11
    return BATTERY_LEVELS.get(battery_id, "Unknown")

def load_accessories(airtags, config_dir):
    # Loaded once at startup and reused for the life of the process -- both
    # for cloud polling and for BLE matching. Reusing the same objects (vs.
    # re-parsing the plist every cycle) matters for BLE: the findmy library
    # tracks rolling-key alignment state on the accessory object itself via
    # update_alignment(), which only helps if it's the *same* object across
    # scans.
    accessories = []
    for airtag in airtags:
        plist_path = config_dir / airtag["plist_path"]
        ha_mqtt_id = airtag["ha_mqtt_id"]
        name = airtag.get("name", ha_mqtt_id)
        try:
            with Path(plist_path).open("rb") as f:
                accessory = FindMyAccessory.from_plist(f)
            accessories.append({"ha_mqtt_id": ha_mqtt_id, "name": name, "accessory": accessory})
        except Exception as e:
            logging.error("Error loading accessory %s from %s: %s", ha_mqtt_id, plist_path, str(e))
    return accessories

def get_location_report(accessory, anisette_server: str):
    try:
        anisette = RemoteAnisetteProvider(anisette_server)
        acc = get_account_sync(anisette)

        try:
            report = acc.fetch_location(accessory)
        finally:
            acc._evt_loop.run_until_complete(acc.close())

        if report:
            return report
        else:
            logging.warning("No location report found for accessory")
            return None
    except Exception as e:
        logging.error("Error fetching location report: %s", str(e))
        return None

def publish_location(client, topic, report):
    location = {
        "latitude": report.latitude,
        "longitude": report.longitude,
        "gps_accuracy": report.confidence,
        "last_report_time": report.timestamp,
        "broadcast_time": datetime.now()
    }
    info = client.publish(topic, json.dumps(location, default=str))
    _wait_for_publish(info, topic)
    logging.info("Location report published to %s: %s", topic, location)

def publish_state(client, state_topic, state):
    info = client.publish(state_topic, state)
    _wait_for_publish(info, state_topic)
    logging.info("Published '%s' to %s", state, state_topic)

def _wait_for_publish(info, topic, timeout=10):
    # A bare wait_for_publish() with no timeout can hang forever if the
    # connection is in a bad state when this is called (e.g. mid-reconnect)
    # -- that would freeze the whole process, since every publish in this
    # script blocks on this. Bound it and log instead of hanging.
    try:
        info.wait_for_publish(timeout=timeout)
    except (RuntimeError, ValueError) as e:
        logging.warning("Publish to %s may not have completed: %s", topic, e)

def publish_discovery_config(client, ha_mqtt_id, name):
    discovery_topic = f"homeassistant/device_tracker/{ha_mqtt_id}/config"
    payload = {
        # "name": None + a "device" block is HA's documented way to say
        # "this entity IS the device -- don't append a name suffix".
        # (Earlier we set this to the same string as the device name,
        # which made HA concatenate them into "X X" -- that's the bug
        # this avoids.)
        "name": None,
        "unique_id": ha_mqtt_id,
        "object_id": ha_mqtt_id,
        "json_attributes_topic": f"{ha_mqtt_id}/attributes",
        "availability_topic": f"{ha_mqtt_id}_gps/availability",
        "source_type": "gps",
        "device": {
            "identifiers": [ha_mqtt_id],
            "name": name,
        },
    }
    client.publish(discovery_topic, json.dumps(payload), retain=True)
    logging.info("Published discovery config for %s to %s", ha_mqtt_id, discovery_topic)

def publish_battery_discovery_config(client, ha_mqtt_id, name):
    discovery_topic = f"homeassistant/sensor/{ha_mqtt_id}_battery/config"
    payload = {
        # A non-null suffix name here is intentional: HA concatenates it
        # with the device name ("<device name> Battery"), which is the
        # normal/expected behavior for a secondary entity on a device.
        "name": "Battery",
        "unique_id": f"{ha_mqtt_id}_battery",
        "object_id": f"{ha_mqtt_id}_battery",
        "state_topic": f"{ha_mqtt_id}/battery",
        "availability_topic": f"{ha_mqtt_id}_gps/availability",
        "icon": "mdi:battery",
        "device": {
            "identifiers": [ha_mqtt_id],
            "name": name,
        },
    }
    client.publish(discovery_topic, json.dumps(payload), retain=True)
    logging.info("Published battery discovery config for %s to %s", ha_mqtt_id, discovery_topic)

def load_last_update_time():
    if Path(LAST_UPDATE_FILE).exists():
        with open(LAST_UPDATE_FILE, "r") as f:
            return json.load(f).get("last_update_time", 0)
    return 0

def save_last_update_time(last_update_time):
    with open(LAST_UPDATE_FILE, "w") as f:
        json.dump({"last_update_time": last_update_time}, f)

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logging.info("Connected to MQTT Broker")
    else:
        logging.error("Failed to connect, return code %s", reason_code)

async def _ble_scan_loop_async(client, accessories, home_latitude, home_longitude,
                                ble_scan_interval, ble_scan_duration, unseen_threshold,
                                stop_event):
    # ha_mqtt_id -> last time we published a BLE-triggered "home" update.
    # Used to avoid re-publishing on every single scan cycle while the tag
    # just sits at home in range -- we only need to say "home" again once
    # unseen_threshold has passed since the last time we said it.
    last_published = {}

    while not stop_event.is_set():
        try:
            scanner = await OfflineFindingScanner.create()
            async for device in scanner.scan_for(timeout=ble_scan_duration):
                now = time.time()
                for entry in accessories:
                    ha_mqtt_id = entry["ha_mqtt_id"]
                    accessory = entry["accessory"]

                    try:
                        matched = device.is_from(accessory)
                    except Exception as e:
                        logging.warning("BLE match check failed for %s: %s", ha_mqtt_id, str(e))
                        continue

                    if not matched:
                        continue

                    if now - last_published.get(ha_mqtt_id, 0) < unseen_threshold:
                        # Already told HA this one is home recently -- skip
                        # the redundant publish.
                        break

                    logging.info(
                        "BLE detected %s nearby (rssi=%s, battery=%s) -- publishing as home",
                        ha_mqtt_id, device.rssi, device.battery_level,
                    )

                    location = {
                        "latitude": home_latitude,
                        "longitude": home_longitude,
                        "gps_accuracy": 10,
                        "last_report_time": device.detected_at,
                        "broadcast_time": datetime.now(),
                        "source": "ble",
                    }
                    client.publish(f"{ha_mqtt_id}/attributes", json.dumps(location, default=str))
                    client.publish(f"{ha_mqtt_id}_gps/availability", "online")
                    if device.battery_level and device.battery_level != "Unknown":
                        client.publish(f"{ha_mqtt_id}/battery", device.battery_level)

                    last_published[ha_mqtt_id] = now
                    break
        except Exception as e:
            logging.error("BLE scan cycle failed: %s", str(e))

        # Sleep between scan windows, but wake early (checked once a second)
        # if we're shutting down.
        slept = 0
        while slept < ble_scan_interval and not stop_event.is_set():
            await asyncio.sleep(1)
            slept += 1

def ble_scan_loop(client, accessories, home_latitude, home_longitude,
                   ble_scan_interval, ble_scan_duration, unseen_threshold, stop_event):
    # Runs in its own dedicated thread with its own asyncio event loop
    # (BLE scanning via bleak/BlueZ is async-only). client.publish() is
    # documented thread-safe to call from threads other than paho-mqtt's own
    # network thread, so we can publish straight from here without routing
    # through the main loop -- avoiding the earlier keepalive-starvation
    # mistake, since this thread is entirely separate from the one running
    # loop_start()'s network thread.
    #
    # Deliberately one-directional: seeing the AirTag over BLE means it's
    # definitely home right now, so we publish immediately. NOT seeing it
    # does NOT mean it's away (BLE range is short and detection is flaky) --
    # "away" stays the responsibility of the existing cloud poll cycle.
    try:
        asyncio.run(_ble_scan_loop_async(
            client, accessories, home_latitude, home_longitude,
            ble_scan_interval, ble_scan_duration, unseen_threshold, stop_event,
        ))
    except Exception as e:
        logging.error("BLE scan thread crashed: %s", str(e))

def main(config_path: str) -> int:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    anisette_server = config["anisette_server"]
    mqtt_broker = config["mqtt_broker"]
    mqtt_username = config["mqtt_username"]
    mqtt_password = config["mqtt_password"]
    mqtt_port = config["mqtt_port"]
    polling_interval = config["polling_interval"] * 60
    airtags = config["airtags"]

    # BLE-related settings. home_latitude/home_longitude are required for
    # BLE to do anything useful (it needs somewhere to report as "home");
    # if they're missing, BLE scanning is skipped entirely rather than
    # publishing bogus 0,0 coordinates.
    ble_scan_interval = config.get("ble_scan_interval", 40)
    ble_scan_duration = config.get("ble_scan_duration", 20)
    unseen_threshold = config.get("unseen_threshold", 60)
    home_latitude = config.get("home_latitude")
    home_longitude = config.get("home_longitude")

    config_dir = Path(config_path).parent
    state = {"last_update_time": load_last_update_time()}

    accessories = load_accessories(airtags, config_dir)

    def publish_all_discovery(client):
        for entry in accessories:
            publish_discovery_config(client, entry["ha_mqtt_id"], entry["name"])
            publish_battery_discovery_config(client, entry["ha_mqtt_id"], entry["name"])

    def poll_and_publish(client):
        for entry in accessories:
            ha_mqtt_id = entry["ha_mqtt_id"]
            accessory = entry["accessory"]
            mqtt_topic = f"{ha_mqtt_id}/attributes"
            mqtt_availability_topic = f"{ha_mqtt_id}_gps/availability"
            mqtt_battery_topic = f"{ha_mqtt_id}/battery"

            report = get_location_report(accessory, anisette_server)
            if report:
                publish_location(client, mqtt_topic, report)
                publish_state(client, mqtt_availability_topic, "online")
                try:
                    battery_level = get_battery_level(report.status)
                    publish_state(client, mqtt_battery_topic, battery_level)
                except Exception as e:
                    logging.warning("Could not determine battery level for %s: %s", ha_mqtt_id, str(e))
            else:
                publish_state(client, mqtt_availability_topic, "offline")

        state["last_update_time"] = time.time()
        save_last_update_time(state["last_update_time"])

    # Set (not called) by on_message, then acted on from the main thread.
    # IMPORTANT: on_message runs on paho-mqtt's own network thread -- the
    # same thread responsible for sending keepalive pings. Doing the
    # AirTag fetch (several seconds of real HTTP calls per tag, to Apple
    # and the anisette server) directly inside on_message blocks that
    # thread long enough to miss keepalives, which gets the connection
    # killed by the broker for "exceeded timeout". So on_message must
    # only ever do fast, non-blocking work.
    resync_requested = threading.Event()

    def on_message(client, userdata, msg):
        # Home Assistant publishes "online" to this topic every time it
        # finishes starting up (its MQTT "birth message"). Our attributes/
        # availability/battery topics aren't retained, so without this,
        # entities would sit "unavailable" after every HA restart until
        # our next scheduled poll (up to polling_interval away). Listening
        # for the birth message lets us push a *fresh* fetch immediately
        # instead of waiting -- and instead of just replaying stale
        # retained data, which would hide a genuinely dead bridge.
        if msg.topic == "homeassistant/status" and msg.payload.decode() == "online":
            logging.info("Home Assistant just came back online -- flagging for resync")
            resync_requested.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(mqtt_username, mqtt_password)

    # One persistent connection for the life of the process, rather than
    # reconnecting every cycle -- needed so we can stay subscribed to
    # homeassistant/status and react the moment HA restarts. paho-mqtt's
    # own background thread (started by loop_start) handles reconnecting
    # automatically if the connection ever drops.
    client.connect(mqtt_broker, mqtt_port, 60)
    client.loop_start()
    client.subscribe("homeassistant/status")

    # Publish retained MQTT discovery configs once at startup so Home
    # Assistant creates/updates the entities automatically -- no
    # configuration.yaml edits needed on the HA side.
    publish_all_discovery(client)

    ble_stop_event = threading.Event()
    ble_thread = None
    if home_latitude is not None and home_longitude is not None:
        ble_thread = threading.Thread(
            target=ble_scan_loop,
            args=(client, accessories, home_latitude, home_longitude,
                  ble_scan_interval, ble_scan_duration, unseen_threshold, ble_stop_event),
            daemon=True,
            name="ble-scan",
        )
        ble_thread.start()
        logging.info("BLE scan thread started (scan %ss every %ss)", ble_scan_duration, ble_scan_interval)
    else:
        logging.warning("home_latitude/home_longitude not set in config -- BLE scanning disabled")

    try:
        while True:
            current_time = time.time()
            sleep_time = max(0, polling_interval - (current_time - state["last_update_time"]))

            logging.info("Sleeping for up to %d seconds (or until HA restarts)", sleep_time)
            # An interruptible sleep: wakes up immediately if on_message
            # flags a resync, otherwise behaves like the normal timer.
            woke_for_resync = resync_requested.wait(timeout=sleep_time)
            resync_requested.clear()

            if woke_for_resync:
                logging.info("Resyncing after Home Assistant restart")
                publish_all_discovery(client)

            poll_and_publish(client)
    finally:
        ble_stop_event.set()
        client.loop_stop()
        client.disconnect()

    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Track AirTags and publish their locations to MQTT.')
    parser.add_argument('config', type=str, help='Path to the configuration file')

    args = parser.parse_args()

    sys.exit(main(args.config))
