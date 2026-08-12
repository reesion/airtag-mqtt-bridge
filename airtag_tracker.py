import logging
import sys
import yaml
import json
import argparse
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
    info.wait_for_publish()
    logging.info("Location report published to %s: %s", topic, location)

def publish_state(client, state_topic, state):
    info = client.publish(state_topic, state)
    info.wait_for_publish()
    logging.info("Published '%s' to %s", state, state_topic)

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

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.username_pw_set(mqtt_username, mqtt_password)

    config_dir = Path(config_path).parent
    last_update_time = load_last_update_time()

    # Publish retained MQTT discovery configs once at startup so Home
    # Assistant creates/updates the device_tracker entities automatically --
    # no configuration.yaml edits needed on the HA side.
    client.connect(mqtt_broker, mqtt_port, 60)
    client.loop_start()
    for airtag in airtags:
        ha_mqtt_id = airtag["ha_mqtt_id"]
        name = airtag.get("name", ha_mqtt_id)
        publish_discovery_config(client, ha_mqtt_id, name)
        publish_battery_discovery_config(client, ha_mqtt_id, name)
    client.loop_stop()
    client.disconnect()

    while True:
        current_time = time.time()
        sleep_time = max(0, polling_interval - (current_time - last_update_time))

        logging.info("Sleeping for %d seconds", sleep_time)
        time.sleep(sleep_time)

        current_time = time.time()

        for airtag in airtags:
            plist_path = config_dir / airtag["plist_path"]
            ha_mqtt_id = airtag["ha_mqtt_id"]
            mqtt_topic = f"{ha_mqtt_id}/attributes"
            mqtt_availability_topic = f"{ha_mqtt_id}_gps/availability"
            mqtt_battery_topic = f"{ha_mqtt_id}/battery"

            report = get_location_report(plist_path, anisette_server)
            client.connect(mqtt_broker, mqtt_port, 60)
            client.loop_start()
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
            client.loop_stop()
            client.disconnect()

        last_update_time = current_time
        save_last_update_time(last_update_time)

    client.disconnect()

    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Track AirTags and publish their locations to MQTT.')
    parser.add_argument('config', type=str, help='Path to the configuration file')

    args = parser.parse_args()

    sys.exit(main(args.config))
