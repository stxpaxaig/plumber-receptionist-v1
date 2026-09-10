import hmac
import logging
import os
import threading

from flask import Flask, abort, jsonify, request
from openai import InvalidWebhookSignatureError, OpenAI

from receptionist import CallController, extract_phone_number
from storage import LeadStore


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY", ""),
        OPENAI_WEBHOOK_SECRET=os.getenv("OPENAI_WEBHOOK_SECRET", ""),
        LEADS_ADMIN_TOKEN=os.getenv("LEADS_ADMIN_TOKEN", ""),
        DATABASE_PATH=os.getenv("DATABASE_PATH", "data/leads.sqlite3"),
        COMPANY_NAME=os.getenv("COMPANY_NAME", "Mountain Plumbing"),
        LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
    )
    if config:
        app.config.update(config)

    logging.basicConfig(
        level=getattr(logging, app.config["LOG_LEVEL"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = LeadStore(app.config["DATABASE_PATH"])
    app.extensions["lead_store"] = store

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/webhooks/openai")
    def openai_webhook():
        api_key = app.config["OPENAI_API_KEY"]
        webhook_secret = app.config["OPENAI_WEBHOOK_SECRET"]
        if not api_key or not webhook_secret:
            app.logger.error("OpenAI credentials are not configured")
            return jsonify(error="server_not_configured"), 503

        raw_body = request.get_data(cache=True, as_text=False)
        try:
            event = OpenAI(
                api_key=api_key,
                webhook_secret=webhook_secret,
            ).webhooks.unwrap(raw_body, request.headers)
        except InvalidWebhookSignatureError:
            app.logger.warning("Rejected webhook with invalid signature")
            return jsonify(error="invalid_signature"), 400
        except Exception:
            app.logger.exception("Unable to parse OpenAI webhook")
            return jsonify(error="invalid_webhook"), 400

        if event.type != "realtime.call.incoming":
            return jsonify(received=True)

        event_id = event.id
        call_id = event.data.call_id
        if not store.claim_webhook(event_id, call_id):
            return jsonify(received=True, duplicate=True)

        sip_headers = [
            {"name": header.name, "value": header.value}
            for header in (event.data.sip_headers or [])
        ]
        caller_number = extract_phone_number(sip_headers, "From")
        called_number = extract_phone_number(sip_headers, "To")
        store.start_call(call_id, caller_number, called_number)

        controller = CallController(
            api_key=api_key,
            call_id=call_id,
            caller_number=caller_number,
            called_number=called_number,
            company_name=app.config["COMPANY_NAME"],
            store=store,
        )
        threading.Thread(
            target=controller.run,
            name=f"realtime-{call_id[-8:]}",
            daemon=True,
        ).start()
        return jsonify(received=True), 202

    @app.get("/api/leads")
    def list_leads():
        expected = app.config["LEADS_ADMIN_TOKEN"]
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(401)
        return jsonify(leads=store.list_leads())

    @app.get("/api/leads/<int:lead_id>")
    def get_lead(lead_id):
        expected = app.config["LEADS_ADMIN_TOKEN"]
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(401)
        lead = store.get_lead(lead_id)
        if lead is None:
            abort(404)
        return jsonify(lead)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
