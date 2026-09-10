"""
Telnyx provisioning and routing helpers.
Idempotently ensures TeXML application exists and phone number is routed to it.
"""

import logging
import os
import requests
from typing import Optional

LOGGER = logging.getLogger(__name__)
TELNYX_API_BASE = "https://api.telnyx.com/v2"
TELNYX_APP_NAME = "Plumber Receptionist V1"


def get_telnyx_headers(api_key: str) -> dict:
    """Return Telnyx API headers with auth."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def ensure_texml_app(
    api_key: str,
    public_domain: str,
) -> Optional[str]:
    """
    Idempotently ensure TeXML application exists and is configured.
    Returns the application ID, or None if provisioning failed.
    """
    if not api_key or not public_domain:
        LOGGER.error("Missing TELNYX_API_KEY or RAILWAY_PUBLIC_DOMAIN for TeXML app provisioning")
        return None

    headers = get_telnyx_headers(api_key)
    voice_url = f"https://{public_domain}/webhooks/telnyx"

    try:
        # List existing TeXML applications
        list_response = requests.get(
            f"{TELNYX_API_BASE}/texml_applications",
            headers=headers,
            timeout=10,
        )
        list_response.raise_for_status()
        apps = list_response.json().get("data", [])

        # Find or create the app
        existing_app = next(
            (app for app in apps if app.get("friendly_name") == TELNYX_APP_NAME),
            None,
        )

        if existing_app:
            app_id = existing_app["id"]
            LOGGER.info(f"Found existing TeXML app: {app_id}")

            # Ensure the app is active and has correct voice routing
            if (
                existing_app.get("active") is True
                and existing_app.get("voice_url") == voice_url
                and existing_app.get("voice_method") == "post"
            ):
                LOGGER.info(f"TeXML app already configured correctly: {app_id}")
                return app_id

            # Update the app
            LOGGER.info(f"Updating TeXML app {app_id} with current voice URL")
            update_payload = {
                "friendly_name": TELNYX_APP_NAME,
                "active": True,
                "voice_url": voice_url,
                "voice_method": "post",
            }
            update_response = requests.patch(
                f"{TELNYX_API_BASE}/texml_applications/{app_id}",
                json=update_payload,
                headers=headers,
                timeout=10,
            )
            update_response.raise_for_status()
            LOGGER.info(f"Updated TeXML app {app_id}")
            return app_id

        else:
            # Create new app
            LOGGER.info(f"Creating new TeXML app: {TELNYX_APP_NAME}")
            create_payload = {
                "friendly_name": TELNYX_APP_NAME,
                "active": True,
                "voice_url": voice_url,
                "voice_method": "post",
            }
            create_response = requests.post(
                f"{TELNYX_API_BASE}/texml_applications",
                json=create_payload,
                headers=headers,
                timeout=10,
            )
            create_response.raise_for_status()
            new_app = create_response.json().get("data", {})
            app_id = new_app.get("id")
            LOGGER.info(f"Created new TeXML app: {app_id}")
            return app_id

    except Exception as e:
        LOGGER.error(f"Failed to ensure TeXML app: {e}")
        return None


def provision_phone_number(
    api_key: str,
    phone_number: str,
    app_id: str,
) -> bool:
    """
    Idempotently route the phone number to the TeXML app.
    Returns True if successful, False otherwise.
    """
    if not api_key or not phone_number or not app_id:
        LOGGER.error("Missing credentials or app ID for phone number provisioning")
        return False

    headers = get_telnyx_headers(api_key)

    try:
        # Find the phone number
        LOGGER.info(f"Looking up phone number {phone_number}")
        list_response = requests.get(
            f"{TELNYX_API_BASE}/phone_numbers",
            headers=headers,
            params={"filter[phone_number][eq]": phone_number},
            timeout=10,
        )
        list_response.raise_for_status()
        numbers = list_response.json().get("data", [])

        if not numbers:
            LOGGER.error(f"Phone number not found: {phone_number}")
            return False

        number_record = numbers[0]
        number_id = number_record.get("id")
        current_app_id = number_record.get("connection_id")

        if current_app_id == app_id:
            LOGGER.info(f"Phone number {phone_number} already routed to app {app_id}")
            return True

        # Update the phone number's connection (app) association
        LOGGER.info(f"Routing {phone_number} to TeXML app {app_id}")
        update_payload = {"connection_id": app_id}
        update_response = requests.patch(
            f"{TELNYX_API_BASE}/phone_numbers/{number_id}",
            json=update_payload,
            headers=headers,
            timeout=10,
        )
        update_response.raise_for_status()
        LOGGER.info(f"Successfully routed {phone_number} to TeXML app {app_id}")
        return True

    except Exception as e:
        LOGGER.error(f"Failed to provision phone number {phone_number}: {e}")
        return False


def provision_telnyx(
    api_key: Optional[str] = None,
    phone_number: Optional[str] = None,
    public_domain: Optional[str] = None,
) -> bool:
    """
    Orchestrate full provisioning: ensure TeXML app and route phone number.
    Returns True if successful, False otherwise.
    
    If any required env var is missing, returns False without raising.
    This allows tests to run without credentials.
    """
    api_key = api_key or os.getenv("TELNYX_API_KEY")
    phone_number = phone_number or os.getenv("TELNYX_PHONE_NUMBER")
    public_domain = public_domain or os.getenv("RAILWAY_PUBLIC_DOMAIN")

    if not api_key or not phone_number or not public_domain:
        LOGGER.warning(
            f"Telnyx provisioning skipped: missing credentials or domain. "
            f"(api_key={bool(api_key)}, phone={bool(phone_number)}, domain={bool(public_domain)})"
        )
        return False

    app_id = ensure_texml_app(api_key, public_domain)
    if not app_id:
        LOGGER.error("Failed to ensure TeXML application")
        return False

    if not provision_phone_number(api_key, phone_number, app_id):
        LOGGER.error(f"Failed to provision phone number {phone_number}")
        return False

    LOGGER.info("Telnyx provisioning completed successfully")
    return True

