import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import websocket


LOGGER = logging.getLogger(__name__)
OPENAI_API_BASE = "https://api.openai.com/v1"
OPENAI_REALTIME_WS = "wss://api.openai.com/v1/realtime"


def extract_phone_number(headers, header_name):
    value = next(
        (item.get("value", "") for item in headers if item.get("name", "").lower() == header_name.lower()),
        "",
    )
    match = re.search(r"sip:(\+?\d+)", value)
    return match.group(1) if match else None


def receptionist_instructions(company_name, caller_number):
    known_number = caller_number or "unknown"
    return f"""
You are the virtual receptionist for {company_name}, a plumbing company. You are answering a real telephone call. Sound calm, warm, natural, and concise. Allow the caller to interrupt you and respond to what they actually say.

Begin by clearly saying you are {company_name}'s virtual receptionist and ask how you can help. Determine the reason for the call, then conversationally collect:
- caller's full name
- best callback number (the incoming caller ID is {known_number}; ask whether that is the best callback number instead of assuming)
- complete service address
- specific plumbing problem
- urgency: emergency, urgent, routine, or unsure

Safety: If there is active flooding, tell the caller to shut off the nearest safe water valve or the home's main water supply only if they can do so safely. If they report a gas smell, suspected gas leak, fire, electrical danger, or immediate danger to people, tell them to move to safety and contact 911 or the appropriate utility emergency line. Do not attempt a diagnosis.

Hard rules:
- Never invent or promise pricing, estimates, appointment times, technician availability, arrival times, warranties, or diagnoses.
- Say a plumber will review the details and contact them; do not claim a booking is confirmed.
- Ask only one or two short questions at a time.
- If something is unclear, politely verify it.
- Before ending, read back the caller's name, callback number, service address, problem, and urgency and ask them to confirm or correct it.
- Only after the caller confirms, call save_plumbing_lead exactly once with the final corrected information and a concise factual summary.
- After the tool succeeds, thank the caller, remind them that a plumber will review the request and call them back, and end naturally.
""".strip()


LEAD_TOOL = {
    "type": "function",
    "name": "save_plumbing_lead",
    "description": "Save the confirmed plumbing service lead. Call only after reading back all important information and receiving caller confirmation.",
    "parameters": {
        "type": "object",
        "properties": {
            "caller_name": {"type": "string", "description": "Caller's confirmed full name"},
            "callback_number": {"type": "string", "description": "Confirmed callback telephone number"},
            "service_address": {"type": "string", "description": "Confirmed complete service address"},
            "plumbing_problem": {"type": "string", "description": "Factual description of the plumbing issue without diagnosis"},
            "urgency": {"type": "string", "enum": ["emergency", "urgent", "routine", "unsure"]},
            "summary": {"type": "string", "description": "Concise factual summary for the plumber"},
        },
        "required": [
            "caller_name",
            "callback_number",
            "service_address",
            "plumbing_problem",
            "urgency",
            "summary",
        ],
        "additionalProperties": False,
    },
}


class CallController:
    def __init__(self, api_key, call_id, caller_number, called_number, company_name, store):
        self.api_key = api_key
        self.call_id = call_id
        self.caller_number = caller_number
        self.called_number = called_number
        self.company_name = company_name
        self.store = store
        self.transcript = []
        self.saved_tool_calls = set()
        self.lead_saved = False

    @property
    def auth_headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def run(self):
        try:
            self._accept_call()
            self._monitor_call()
        except Exception as exc:
            LOGGER.exception("Call controller failed for %s", self.call_id)
            self.store.finish_call(self.call_id, "failed", self.transcript, str(exc))
        else:
            self.store.finish_call(
                self.call_id,
                "completed" if self.lead_saved else "ended_without_confirmed_lead",
                self.transcript,
                None,
            )

    def _accept_call(self):
        payload = {
            "type": "realtime",
            "model": "gpt-realtime-2.1",
            "instructions": receptionist_instructions(self.company_name, self.caller_number),
            "audio": {
                "input": {
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "auto",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {"voice": "marin"},
            },
            "tools": [LEAD_TOOL],
            "tool_choice": "auto",
        }
        response = requests.post(
            f"{OPENAI_API_BASE}/realtime/calls/{quote(self.call_id, safe='')}/accept",
            headers={**self.auth_headers, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        self.store.mark_answered(self.call_id)

    def _monitor_call(self):
        ws = websocket.create_connection(
            f"{OPENAI_REALTIME_WS}?call_id={quote(self.call_id, safe='')}",
            header=[f"Authorization: Bearer {self.api_key}"],
            timeout=15,
        )
        ws.settimeout(None)
        try:
            ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "instructions": f"Greet the caller now as {self.company_name}'s virtual receptionist and ask how you can help."
                },
            }))
            while True:
                raw_message = ws.recv()
                if raw_message in (None, ""):
                    break
                event = json.loads(raw_message)
                self._handle_event(ws, event)
        except websocket.WebSocketConnectionClosedException:
            pass
        finally:
            ws.close()

    def _handle_event(self, ws, event):
        event_type = event.get("type", "")
        if event_type == "conversation.item.input_audio_transcription.completed":
            self._append_transcript("caller", event.get("transcript", ""), event.get("item_id"))
        elif event_type in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            self._append_transcript("receptionist", event.get("transcript", ""), event.get("item_id"))
        elif event_type == "response.function_call_arguments.done":
            self._handle_tool_call(
                ws,
                event.get("name"),
                event.get("call_id"),
                event.get("arguments", "{}"),
            )
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self._handle_tool_call(
                    ws,
                    item.get("name"),
                    item.get("call_id"),
                    item.get("arguments", "{}"),
                )
        elif event_type == "error":
            LOGGER.error("Realtime error for %s: %s", self.call_id, event.get("error"))

    def _append_transcript(self, role, text, item_id=None):
        clean = (text or "").strip()
        if not clean:
            return
        entry = {
            "role": role,
            "text": clean,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if item_id:
            entry["item_id"] = item_id
        if not any(existing.get("item_id") == item_id and existing["role"] == role for existing in self.transcript if item_id):
            self.transcript.append(entry)
            self.store.update_transcript(self.call_id, self.transcript)

    def _handle_tool_call(self, ws, name, tool_call_id, arguments):
        if name != "save_plumbing_lead" or not tool_call_id or tool_call_id in self.saved_tool_calls:
            return
        self.saved_tool_calls.add(tool_call_id)
        try:
            lead = json.loads(arguments)
            lead_id = self.store.save_lead(
                call_id=self.call_id,
                caller_number=self.caller_number,
                called_number=self.called_number,
                lead=lead,
                transcript=self.transcript,
            )
            self.lead_saved = True
            result = {"success": True, "lead_id": lead_id}
        except Exception as exc:
            LOGGER.exception("Unable to save lead for %s", self.call_id)
            result = {"success": False, "error": "lead_storage_failed"}

        ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": json.dumps(result),
            },
        }))
        ws.send(json.dumps({"type": "response.create"}))
