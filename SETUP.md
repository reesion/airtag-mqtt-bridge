# AirTag -> MQTT -> Home Assistant bridge (Portainer add-on, no filesystem/SSH access needed anywhere)

Since your Synology is a completely separate box with nothing to do with
HA/Portainer, and your Portainer add-on doesn't do git-based stack builds,
this version has GitHub build the image for you automatically, and gets
all its config through Portainer's own browser Console -- no File Station,
no SSH, on either machine.

## 1. Push the code to GitHub (web upload, no git command line)

Create a new public repo on github.com. Use "Add file -> Upload files" in
your browser to upload everything in this package EXCEPT `config.yaml`
(that one's just a reference copy for you to edit locally and paste in
later -- don't upload it, it's fine as public since it has no real values
yet, but no reason to). Make sure `.github/workflows/docker-build.yml`
goes in at that exact path (`.github/workflows/` folder) -- GitHub's
upload UI preserves folder structure if you drag the whole `.github`
folder in, or you can create the file manually via "Add file -> Create
new file" and type that path in the filename box.

## 2. Let GitHub Actions build it

After the upload, go to the "Actions" tab in your repo -- you should see
a workflow run start automatically (triggered by the push). Wait for it
to go green (a minute or two).

Then go to your repo's main page -> right sidebar -> "Packages" -> click
the package it just published. On that package's page, go to "Package
settings" (bottom right) -> Change visibility -> make it **Public**. This
lets Portainer pull the image without needing registry credentials.

Note the exact image path shown there -- it'll be something like
`ghcr.io/yourusername/your-repo-name:latest`.

## 3. Deploy the stack in Portainer

Open `docker-compose.yml` from this package, replace
`ghcr.io/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME:latest` with your actual
image path from step 2.

In Portainer: Stacks -> Add stack -> name it (e.g. "airtag-tracker") ->
Web editor -> paste the compose content -> Deploy the stack.

The container will start immediately and just sit there waiting -- that's
expected, it's designed to stay up and console-able until you configure it
(see entrypoint.sh if you're curious why).

## 4. Configure it through Portainer's Console

Portainer -> Containers -> click `airtag_tracker` -> **Console** button ->
Connect (shell: `/bin/sh`).

Create the records folder and config.yaml:
```
mkdir -p /app/data/records
cat > /app/data/config.yaml << 'EOF'
anisette_server: "http://192.168.1.142:6969"
mqtt_broker: "YOUR_MQTT_BROKER"
mqtt_username: "YOUR_MQTT_USERNAME"
mqtt_password: "YOUR_MQTT_PASSWORD"
mqtt_port: 1883
polling_interval: 15
ble_scan_interval: 40
ble_scan_duration: 20
unseen_threshold: 60
airtags:
  - plist_path: "records/airtag_1.plist"
    ha_mqtt_id: "airtag_1"
  - plist_path: "records/airtag_2.plist"
    ha_mqtt_id: "airtag_2"
EOF
```
(fill in your real MQTT broker/username/password before pasting)

## 5. Get your two plist files in (base64 paste, since they're binary)

Binary files can't be pasted directly into a text console, so encode them
first. On the Mac, in Terminal:
```
base64 "OwnedBeacons/1B7EA54D-B9AF-4906-88E1-C6C70B431590.plist" > airtag_1.b64.txt
base64 "OwnedBeacons/E447F9A7-EBB9-44C8-A5AB-F2061BC73BD6.plist" > airtag_2.b64.txt
```
Open each `.txt` file, copy its full contents, and in the Portainer
console run:
```
cat > /app/data/records/airtag_1.b64 << 'EOF'
[paste the base64 content from airtag_1.b64.txt here]
EOF
base64 -d /app/data/records/airtag_1.b64 > /app/data/records/airtag_1.plist
```
Repeat for airtag_2. Verify both landed correctly:
```
ls -la /app/data/records/
```
You should see `airtag_1.plist` and `airtag_2.plist` with reasonable file
sizes (a few KB, not 0 bytes).

## 6. One-time interactive Apple login

Still in the same Console session:
```
python /app/_login.py
```
Enter your Apple ID email and password, then complete 2FA (pick trusted
device or SMS, type the code). When it says "Session saved", this part is
done for good -- the session persists in the named volume, so container
restarts/updates won't force you through this again.

Within about 10 seconds of config.yaml existing, the container's main
loop (entrypoint.sh) should notice and start the tracker automatically --
check Portainer -> Containers -> airtag_tracker -> Logs to confirm you
see "Found /app/data/config.yaml -- starting tracker."

## 7. Add the Home Assistant side

Copy the contents of `ha_configuration_snippet.yaml` into your HA
`configuration.yaml`, then restart Home Assistant.

## 8. Verify

After ~15 minutes, check Developer Tools -> States in HA, search
"airtag_1" -- you should see latitude/longitude attributes populated.

## Notes

- BLE scanning is intentionally left out (needs direct Bluetooth hardware
  access, too fiddly through this setup). GPS-only via Apple's Find My
  network -- same data source hass-FindMy used, different architecture
  that should actually hold its session.
- If the container's logs show it stuck waiting even after step 6, double
  check `/app/data/config.yaml` actually exists and is valid YAML --
  `cat /app/data/config.yaml` in the console to check.
