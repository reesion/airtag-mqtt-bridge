"""
Handles Apple Account login + session persistence for airtag_tracker.py.

The key idea: Apple Account sessions are precious (each fresh login creates a
new "device" entry on the account and forces a new 2FA challenge). So we log
in interactively ONE TIME, save the resulting session to disk, and every
subsequent run just reloads that saved session instead of logging in again.

First run (no saved session yet): prompts for email/password, then walks you
through the 2FA challenge interactively.

Every run after that: silently reuses the saved session file. No 2FA needed
again unless Apple invalidates the session server-side.
"""

import os
import json
import logging
from pathlib import Path

from findmy import AppleAccount, LoginState
from findmy.reports import RemoteAnisetteProvider

# Relative path -- the container's CMD `cd`s into the persistent state
# directory before running, so this lands in the volume that survives
# restarts/updates instead of being lost with the container.
SESSION_FILE = Path("account_session.json")


def get_account_sync(anisette: RemoteAnisetteProvider) -> AppleAccount:
    if SESSION_FILE.exists():
        try:
            state_info = json.loads(SESSION_FILE.read_text())
            acc = AppleAccount(anisette, state_info=state_info)
            logging.info("Restored existing Apple account session from %s", SESSION_FILE)
            return acc
        except Exception as e:
            logging.warning(
                "Saved session at %s could not be loaded (%s) -- falling back to fresh login",
                SESSION_FILE, e,
            )

    # No usable saved session -- do a fresh interactive login.
    # Run this once manually (e.g. `docker compose run --rm airtag_tracker python _login.py`)
    # BEFORE the background service starts, so you're actually there to enter the 2FA code.
    email = os.environ.get("APPLE_ID_EMAIL") or input("Apple ID email: ")
    password = os.environ.get("APPLE_ID_PASSWORD") or input("Apple ID password: ")

    acc = AppleAccount(anisette)
    state = acc.login(email, password)

    if state == LoginState.REQUIRE_2FA:
        methods = acc.get_2fa_methods()
        for i, method in enumerate(methods):
            print(f"{i} - {method}")
        idx = int(input("Choose 2FA method (number): "))
        method = methods[idx]
        method.request()
        code = input("Enter the code you received: ")
        method.submit(code)

    acc.to_json(SESSION_FILE)
    logging.info("Logged in successfully and saved new session to %s", SESSION_FILE)
    return acc


if __name__ == "__main__":
    # Lets you bootstrap the session file standalone, e.g.:
    #   docker compose run --rm airtag_tracker python _login.py
    import yaml

    logging.basicConfig(level=logging.INFO)
    config = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
    anisette = RemoteAnisetteProvider(config["anisette_server"])
    get_account_sync(anisette)
    print(f"Session saved to {SESSION_FILE}. You can now start the background service.")
