import logging
import sys
import yaml
import json
import argparse
import threading
from pathlib import Path
import time
import paho.mqtt.client as mqtt
from _login import get_account_sync
from findmy import FindMyAccessory
from findmy.reports import RemoteAnisetteProvider
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

def get_location_report(plist_path: str, anisette_server: str):
    try:
        with Path(plist_path).open("rb") as f:
            airtag = FindMyAccessory.from_plist(f)

        anisette = RemoteAnisetteProvider(anisette_server)
        acc = get_account_sync(anisette)

        try:
            report = acc.fetch_location(airtag)
        finally:
            acc._evt_loop.run_until_complete(acc.close())

        if report:
            return report
        else:
            logging.warning("No location report found for %s", plist_path)
            return None
    except Exception as e:
        logging.error("Error fetching location report for %s: %s", plist_path, str(e))
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

    config_dir = Path(config_path).parent
    state = {"last_update_time": load_last_update_time()}

    def publish_all_discovery(client):
        for airtag in airtags:
            ha_mqtt_id = airtag["ha_mqtt_id"]
            name = airtag.get("name", ha_mqtt_id)
            publish_discovery_config(client, ha_mqtt_id, name)
            publish_battery_discovery_config(client, ha_mqtt_id, name)

    def poll_and_publish(client):
        for airtag in airtags:
            plist_path = config_dir / airtag["plist_path"]
            ha_mqtt_id = airtag["ha_mqtt_id"]
            mqtt_topic = f"{ha_mqtt_id}/attributes"
            mqtt_availability_topic = f"{ha_mqtt_id}_gps/availability"
            mqtt_battery_topic = f"{ha_mqtt_id}/battery"

            report = get_location_report(plist_path, anisette_server)
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
        client.loop_stop()
        client.disconnect()

    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Track AirTags and publish their locations to MQTT.')
    parser.add_argument('config', type=str, help='Path to the configuration file')

    args = parser.parse_args()

    sys.exit(main(args.config))
