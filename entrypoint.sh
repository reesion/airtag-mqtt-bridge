#!/bin/sh
# Keeps the container alive and console-able even before it's configured,
# instead of crash-looping on a missing config.yaml. Once config.yaml
# shows up in the persistent volume (you'll create it via Portainer's
# Console), it starts the actual tracker loop.

CONFIG_PATH="/app/data/config.yaml"

while [ ! -f "$CONFIG_PATH" ]; do
  echo "Waiting for $CONFIG_PATH to be created (use Portainer's Console to create it)..."
  sleep 10
done

echo "Found $CONFIG_PATH -- starting tracker."
exec python /app/airtag_tracker.py "$CONFIG_PATH"
