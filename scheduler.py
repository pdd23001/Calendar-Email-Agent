"""
scheduler_agent.py

Simple AI meeting-scheduler agent that:
- Uses Google Gemini API to parse a natural-language instruction
  and to draft a scheduling email.
- Uses SMTP to send the email.

Usage:
    python scheduler_agent.py "Schedule a 30 minute sync with Alice (alice@example.com) next week, mornings only, about the Q4 roadmap."

NOTE: This v1 does NOT integrate with a real calendar.
It just picks reasonable time slots inside the requested window.
"""

import os
import json
import sys
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from pathlib import Path
from difflib import get_close_matches

from dotenv import load_dotenv
from google import genai  # Gemini / Google GenAI SDK

# Google Calendar API imports
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False


# ---------------------------------------------------------------------------
# 1. Environment & client setup
# ---------------------------------------------------------------------------

load_dotenv()  # load .env if present

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable.")

# Create Gemini client (Developer API mode).
# Docs: https://ai.google.dev/gemini-api/docs/quickstart
# The key could also be picked up from GEMINI_API_KEY env var automatically,
# but we'll pass it explicitly here for clarity.
client = genai.Client(api_key=GEMINI_API_KEY)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)
FROM_NAME = os.getenv("FROM_NAME", "Meeting Assistant")
DEFAULT_TIME_ZONE = os.getenv("DEFAULT_TIME_ZONE", "America/New_York")

# Calendar and contacts configuration
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
CONTACTS_FILE = os.getenv("CONTACTS_FILE", "./contacts.json")
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]  # Read/write access

if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
    raise RuntimeError(
        "SMTP_HOST, SMTP_USERNAME, and SMTP_PASSWORD must be set in environment variables."
    )


# ---------------------------------------------------------------------------
# 2. Utility: simple JSON extraction from model text
# ---------------------------------------------------------------------------

def extract_json_from_text(text: str) -> dict:
    """
    Gemini sometimes wraps JSON in code fences like ```json ... ``` or includes
    extra commentary. This helper tries to pull out the JSON object.

    This is intentionally simple; for production you'd want something more robust.
    """
    text = text.strip()

    # Remove code fences if present
    if text.startswith("```"):
        # Strip first fence
        text = text.split("```", 1)[1]
        # Strip potential language tag (e.g. "json\n")
        text = text.lstrip("json").lstrip("python").lstrip().strip()
        # Strip closing fence if still present
        if "```" in text:
            text = text.split("```", 1)[0].strip()

    # Try direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: try to find the first {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            return json.loads(candidate)
        raise


# ---------------------------------------------------------------------------
# 2b. Contact Memory: remember emails for people
# ---------------------------------------------------------------------------

class ContactMemory:
    """
    Simple contact storage: remembers name -> email mappings.
    Saves to a JSON file for persistence across runs.
    """
    def __init__(self, filepath: str = CONTACTS_FILE):
        self.filepath = Path(filepath)
        self.contacts = self._load()

    def _load(self) -> dict:
        """Load contacts from JSON file."""
        if not self.filepath.exists():
            return {}
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def save(self) -> None:
        """Save contacts to JSON file."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.contacts, f, indent=2, ensure_ascii=False)

    def add_contact(self, name: str, email: str) -> None:
        """Add or update a contact."""
        if not name or not email:
            return
        # Normalize name to lowercase for case-insensitive matching
        key = name.strip().lower()
        self.contacts[key] = email.strip()
        self.save()

    def get_email(self, name: str) -> str | None:
        """Get email for a name. Returns None if not found."""
        key = name.strip().lower()
        return self.contacts.get(key)

    def fuzzy_match(self, name: str, threshold: float = 0.6) -> str | None:
        """
        Try to find a contact by fuzzy matching the name.
        Returns the email if a close match is found, else None.
        """
        key = name.strip().lower()
        matches = get_close_matches(key, self.contacts.keys(), n=1, cutoff=threshold)
        if matches:
            return self.contacts[matches[0]]
        return None

    def get_all_contacts_text(self) -> str:
        """
        Return a formatted string of all contacts for injection into prompts.
        """
        if not self.contacts:
            return "(No saved contacts yet.)"
        lines = [f"- {name.title()}: {email}" for name, email in self.contacts.items()]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2c. Google Calendar: check availability
# ---------------------------------------------------------------------------

def get_calendar_service():
    """
    Authenticate and return a Google Calendar API service object.
    Uses OAuth 2.0 flow with local credentials.
    """
    if not CALENDAR_AVAILABLE:
        return None

    creds = None
    token_path = Path("token.json")
    creds_path = Path("credentials.json")

    # Load existing token if available
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), CALENDAR_SCOPES)

    # If no valid credentials, authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif creds_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            # No credentials file found
            return None

        # Save credentials for next run
        with open(token_path, "w") as token_file:
            token_file.write(creds.to_json())

    try:
        service = build("calendar", "v3", credentials=creds)
        return service
    except Exception:
        return None


def get_busy_times(service, time_min: dt.datetime, time_max: dt.datetime) -> list[dict]:
    """
    Query Google Calendar for busy times between time_min and time_max.
    Returns a list of dicts: [{"start": datetime, "end": datetime}, ...]
    """
    if not service:
        return []

    try:
        body = {
            "timeMin": time_min.isoformat() + "Z",
            "timeMax": time_max.isoformat() + "Z",
            "items": [{"id": CALENDAR_ID}],
        }
        events_result = service.freebusy().query(body=body).execute()
        calendars = events_result.get("calendars", {})
        busy_periods = calendars.get(CALENDAR_ID, {}).get("busy", [])

        busy_times = []
        for period in busy_periods:
            start = dt.datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
            end = dt.datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
            # Remove timezone info for simplicity (or handle properly in production)
            busy_times.append({
                "start": start.replace(tzinfo=None),
                "end": end.replace(tzinfo=None)
            })
        return busy_times
    except HttpError:
        return []


def is_slot_free(slot_start: dt.datetime, slot_end: dt.datetime, busy_times: list[dict]) -> bool:
    """
    Check if a time slot overlaps with any busy periods.
    Returns True if the slot is free.
    """
    for busy in busy_times:
        # Check for overlap: slot and busy period overlap if:
        # slot_start < busy_end AND slot_end > busy_start
        if slot_start < busy["end"] and slot_end > busy["start"]:
            return False
    return True


def create_calendar_event(service, meeting: dict, start_time: dt.datetime, end_time: dt.datetime) -> dict | None:
    """
    Create a calendar event for the meeting.
    
    Args:
        service: Google Calendar API service object
        meeting: Meeting dict with subject, topic, attendees, etc.
        start_time: Event start datetime
        end_time: Event end datetime
    
    Returns:
        Dict with event details (id, link) or None on failure
    """
    if not service:
        return None
    
    try:
        # Build attendee list
        attendees = []
        for att in meeting.get("attendees", []):
            if att.get("email"):
                attendees.append({"email": att["email"]})
        
        # Create event body
        topic = meeting.get("topic") or ""
        extra_context = meeting.get("extra_context") or ""
        description = (topic + ("\n\n" + extra_context if extra_context else "")).strip()
        
        event = {
            "summary": meeting.get("subject", "Meeting"),
            "description": description,
            "start": {
                "dateTime": start_time.isoformat(),
                "timeZone": meeting.get("time_zone", DEFAULT_TIME_ZONE),
            },
            "end": {
                "dateTime": end_time.isoformat(),
                "timeZone": meeting.get("time_zone", DEFAULT_TIME_ZONE),
            },
            "attendees": attendees,
            "reminders": {
                "useDefault": True,
            },
        }
        
        # Create the event
        created_event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        
        return {
            "id": created_event.get("id"),
            "link": created_event.get("htmlLink"),
            "summary": created_event.get("summary"),
        }
    except HttpError as e:
        print(f"   ⚠ Failed to create calendar event: {e}")
        return None
    except Exception as e:
        print(f"   ⚠ Unexpected error creating event: {e}")
        return None


def search_events(service, criteria: dict, time_range_start: dt.datetime, time_range_end: dt.datetime) -> list[dict]:
    """
    Search for calendar events matching the given criteria.
    
    Args:
        service: Google Calendar API service object
        criteria: Dict with search parameters (attendee_name, subject_keywords, etc.)
        time_range_start: Start of time range to search
        time_range_end: End of time range to search
    
    Returns:
        List of matching events with details
    """
    if not service:
        return []
    
    try:
        # Query calendar events in the time range
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_range_start.isoformat() + 'Z',
            timeMax=time_range_end.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # Filter by criteria
        matching_events = []
        attendee_name = (criteria.get("attendee_name") or "").lower()
        subject_keywords = criteria.get("subject_keywords") or []
        
        for event in events:
            # Check attendee name match (only if attendee_name is specified)
            attendee_match = False
            if attendee_name:
                # Check actual attendees list
                for attendee in event.get('attendees', []):
                    attendee_email = attendee.get('email', '').lower()
                    attendee_display = attendee.get('displayName', '').lower()
                    if attendee_name in attendee_email or attendee_name in attendee_display:
                        attendee_match = True
                        break
                
                # Also check event summary/title for the name
                if not attendee_match:
                    summary = event.get('summary', '').lower()
                    if attendee_name in summary:
                        attendee_match = True
                
                # Also check organizer
                if not attendee_match:
                    organizer = event.get('organizer', {})
                    organizer_email = organizer.get('email', '').lower()
                    organizer_display = organizer.get('displayName', '').lower()
                    if attendee_name in organizer_email or attendee_name in organizer_display:
                        attendee_match = True
            else:
                # No attendee filter - match all events
                attendee_match = True
            
            # Check subject keywords match
            subject_match = True
            if subject_keywords:
                summary = event.get('summary', '').lower()
                subject_match = any(keyword.lower() in summary for keyword in subject_keywords)
            
            if attendee_match and subject_match:
                matching_events.append({
                    'id': event['id'],
                    'summary': event.get('summary', 'No title'),
                    'start': event['start'].get('dateTime', event['start'].get('date')),
                    'end': event['end'].get('dateTime', event['end'].get('date')),
                    'attendees': event.get('attendees', []),
                    'raw_event': event
                })
        
        return matching_events
        
    except HttpError as e:
        print(f"   ⚠ Failed to search events: {e}")
        return []
    except Exception as e:
        print(f"   ⚠ Unexpected error searching events: {e}")
        return []


def delete_calendar_event(service, event_id: str) -> bool:
    """
    Delete a calendar event by ID.
    
    Args:
        service: Google Calendar API service object
        event_id: ID of the event to delete
    
    Returns:
        True if deleted successfully, False otherwise
    """
    if not service:
        return False
    
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except HttpError as e:
        print(f"   ⚠ Failed to delete event: {e}")
        return False
    except Exception as e:
        print(f"   ⚠ Unexpected error deleting event: {e}")
        return False





# ---------------------------------------------------------------------------
# 3. Agent: parse user instruction into structured meeting data
# ---------------------------------------------------------------------------

PARSE_PROMPT_TEMPLATE = """
You are an AI scheduling assistant.

The user will describe a meeting they want to schedule in natural language.
Extract the details and respond ONLY with valid JSON, no commentary.

JSON schema (all keys required):

{{
  "subject": "Short email subject line for this meeting.",
  "topic": "A brief description of the meeting topic.",
  "attendees": [
    {{
      "name": "Name if given, else null",
      "email": "email@example.com if given, else null"
    }}
  ],
  "duration_minutes": 30,
  "time_zone": "IANA time zone, e.g. 'America/New_York'",
  "scheduling_mode": "direct" or "proposal" or "cancel",
  "exact_time": "ISO 8601 datetime if mode=direct, e.g. '2025-11-24T14:00', else null",
  "earliest_start": "ISO 8601 datetime for the earliest acceptable start, e.g. '2025-11-24T09:00'",
  "latest_end": "ISO 8601 datetime for the latest acceptable end, e.g. '2025-11-28T17:00'",
  "preferred_times_of_day": ["morning", "afternoon", "evening"],
  "extra_context": "Any additional information that should be included in the email body.",
  "cancel_criteria": {{
    "attendee_name": "Name of person if canceling meeting with them, else null",
    "date_range_start": "ISO 8601 datetime for start of search range if canceling, else null",
    "date_range_end": "ISO 8601 datetime for end of search range if canceling, else null",
    "subject_keywords": ["List of keywords to match in subject if canceling, else empty list"]
  }}
}}

Rules:
- Use the time zone '{default_tz}' if the user doesn't specify one.
- If the user mentions a relative window (e.g. "next week", "tomorrow afternoon"),
  choose a reasonable concrete range starting from today's date: {today}.
- Ensure earliest_start < latest_end.
- If you are unsure about exact times, pick typical working hours:
  - morning: 09:00–12:00
  - afternoon: 13:00–17:00
  - evening: 17:00–20:00
- If no attendees are mentioned, attendees list can be empty (but still present).

SCHEDULING MODE DETECTION (CRITICAL):
- Set scheduling_mode to "cancel" if the user wants to DELETE/CANCEL/REMOVE an existing meeting
  (e.g., "Cancel my meeting with Alice", "Delete tomorrow's 2pm meeting", "Remove the sync with Bob").
  In this case, fill cancel_criteria with search parameters.
- Set scheduling_mode to "direct" if the user specifies an EXACT date and time for SCHEDULING
  (e.g., "tomorrow at 2pm", "Monday November 25 at 3:00pm", "next Friday 10am").
  In this case, set exact_time to that specific datetime.
- Set scheduling_mode to "proposal" if the user gives a TIME RANGE or vague window for SCHEDULING
  (e.g., "next week", "tomorrow afternoon", "sometime next Monday", "between 2-4pm").
  In this case, set exact_time to null.

IMPORTANT - Known Contacts:
If the user mentions a person by name only (without an email), check if they match any of these saved contacts.
If there's a match, use the saved email address. If not, set email to null.

Known contacts:
{known_contacts}

User request:
\"\"\"{user_instruction}\"\"\"
"""


def parse_meeting_request(user_instruction: str, contacts: ContactMemory) -> dict:
    """
    Ask Gemini to turn the natural-language user instruction into structured JSON.
    Uses contact memory to auto-fill known email addresses.
    """
    today = dt.date.today().isoformat()
    known_contacts = contacts.get_all_contacts_text()

    prompt = PARSE_PROMPT_TEMPLATE.format(
        user_instruction=user_instruction,
        today=today,
        default_tz=DEFAULT_TIME_ZONE,
        known_contacts=known_contacts,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    text = response.text
    data = extract_json_from_text(text)
    return data


# ---------------------------------------------------------------------------
# 4. Agent: pick candidate time slots
# ---------------------------------------------------------------------------

def pick_candidate_slots(meeting: dict, calendar_service=None, max_slots: int = 3) -> list[dict]:
    """
    Given structured meeting data, pick 2–3 candidate time slots.

    If calendar_service is provided, this will check Google Calendar for availability
    and only suggest free time slots. Otherwise, it picks reasonable slots within
    [earliest_start, latest_end] that match preferred_times_of_day and duration.

    Each slot dict:
    {
      "start": "ISO datetime string",
      "end": "ISO datetime string"
    }
    """
    duration_minutes = int(meeting.get("duration_minutes", 30))
    duration = dt.timedelta(minutes=duration_minutes)

    earliest_start = dt.datetime.fromisoformat(meeting["earliest_start"])
    latest_end = dt.datetime.fromisoformat(meeting["latest_end"])
    preferred = meeting.get("preferred_times_of_day") or ["morning", "afternoon"]

    # Get busy times from calendar if available
    busy_times = []
    if calendar_service:
        print("  → Checking Google Calendar for availability...")
        busy_times = get_busy_times(calendar_service, earliest_start, latest_end)
        if busy_times:
            print(f"  → Found {len(busy_times)} busy period(s)")
        else:
            print("  → No conflicts found in calendar")

    slots: list[dict] = []

    # Define simple hour windows
    time_blocks = {
        "morning": (9, 12),      # 09:00–12:00
        "afternoon": (13, 17),   # 13:00–17:00
        "evening": (17, 20),     # 17:00–20:00
    }

    # Iterate day by day between earliest_start.date and latest_end.date
    current_day = earliest_start.date()
    last_day = latest_end.date()

    while current_day <= last_day and len(slots) < max_slots:
        for tod in preferred:
            if tod not in time_blocks:
                continue
            start_hour, end_hour = time_blocks[tod]

            # Candidate start at the block's start, within earliest/latest
            candidate_start = dt.datetime.combine(current_day, dt.time(start_hour, 0))
            candidate_end = candidate_start + duration

            # Ensure candidate is within the global window
            if candidate_start < earliest_start:
                continue
            if candidate_end > latest_end:
                continue

            # Check if slot is free (if calendar available)
            if busy_times and not is_slot_free(candidate_start, candidate_end, busy_times):
                continue

            slots.append(
                {
                    "start": candidate_start.isoformat(timespec="minutes"),
                    "end": candidate_end.isoformat(timespec="minutes"),
                }
            )
            if len(slots) >= max_slots:
                break

        current_day += dt.timedelta(days=1)

    return slots


# ---------------------------------------------------------------------------
# 5. Agent: draft the email text with Gemini
# ---------------------------------------------------------------------------

EMAIL_PROMPT_TEMPLATE = """
You are an AI meeting scheduling assistant.

Write a polite, concise email to schedule a meeting using the details below.

Meeting:
- Subject: {subject}
- Topic / purpose: {topic}
- Duration: {duration_minutes} minutes
- Time zone: {time_zone}
- Candidate time slots:
{slot_lines}

Attendees (may be empty or missing names):
{attendee_lines}

Extra context from the user:
\"\"\"{extra_context}\"\"\"


Requirements for the email:
- Write as if from a human (me), not from an AI.
- Start with a friendly greeting.
- Briefly mention the purpose of the meeting.
- Present the candidate time slots as a bulleted list.
- Clearly state that times are in {time_zone}.
- Ask them to choose one option or propose an alternative.
- Keep it professional but warm.
- Sign off with: "Best regards, {sender_name}"
- Do NOT include any JSON or technical formatting, just plain email text.
"""


def format_slots_for_prompt(slots: list[dict], time_zone: str) -> str:
    lines = []
    for s in slots:
        start = dt.datetime.fromisoformat(s["start"])
        end = dt.datetime.fromisoformat(s["end"])
        # For simplicity, we don't convert time zones here; we just label.
        date_str = start.strftime("%a, %b %d")
        time_str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        lines.append(f"- {date_str}, {time_str} ({time_zone})")
    return "\n".join(lines) if lines else "- (No slots found, but suggest that in the email.)"


def format_attendees_for_prompt(attendees: list[dict]) -> str:
    if not attendees:
        return "- (No attendees specified.)"
    lines = []
    for a in attendees:
        name = a.get("name") or "Unknown"
        email = a.get("email") or "unknown"
        lines.append(f"- {name} <{email}>")
    return "\n".join(lines)


def draft_email(meeting: dict, slots: list[dict]) -> str:
    """
    Ask Gemini to draft the actual email body.
    """
    slot_lines = format_slots_for_prompt(slots, meeting["time_zone"])
    attendee_lines = format_attendees_for_prompt(meeting.get("attendees", []))
    
    # Get first attendee name for greeting
    recipient_name = "there"
    attendees = meeting.get("attendees", [])
    if attendees and attendees[0].get("name"):
        # Use first name only
        full_name = attendees[0]["name"]
        recipient_name = full_name.split()[0] if full_name else "there"

    prompt = EMAIL_PROMPT_TEMPLATE.format(
        subject=meeting["subject"],
        topic=meeting["topic"],
        duration_minutes=meeting["duration_minutes"],
        time_zone=meeting["time_zone"],
        slot_lines=slot_lines,
        attendee_lines=attendee_lines,
        extra_context=meeting.get("extra_context", ""),
        recipient_name=recipient_name,
        sender_name=FROM_NAME,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


# ---------------------------------------------------------------------------
# 5b. Confirmation email for direct scheduling
# ---------------------------------------------------------------------------

CONFIRMATION_EMAIL_TEMPLATE = """
You are an AI meeting scheduling assistant.

Write a polite, concise CONFIRMATION email for a meeting that has already been scheduled.

Meeting details:
- Subject: {subject}
- Topic / purpose: {topic}
- Date and time: {date_time_str}
- Duration: {duration_minutes} minutes
- Time zone: {time_zone}
- Calendar event link: {event_link}

Attendees:
{attendee_lines}

Extra context from the user:
\"\"\"{extra_context}\"\"\"

Requirements for the email:
- Address the recipient by their first name in the greeting (e.g., "Hi {recipient_name},").
- CONFIRM that the meeting has been scheduled (don't ask for availability).
- Clearly state the date, time, and duration.
- Include the calendar event link so they can add it to their calendar.
- Mention the topic/purpose briefly.
- Let them know they can reach out if they need to reschedule.
- Keep it professional but warm.
- Sign off with: "Best regards, {sender_name}"
- Do NOT include any JSON or technical formatting, just plain email text.
"""


def draft_confirmation_email(meeting: dict, event_details: dict, start_time: dt.datetime, end_time: dt.datetime) -> str:
    """
    Ask Gemini to draft a confirmation email for a scheduled meeting.
    """
    attendee_lines = format_attendees_for_prompt(meeting.get("attendees", []))
    
    # Format the date/time nicely
    date_str = start_time.strftime("%A, %B %d, %Y")
    time_str = f"{start_time.strftime('%I:%M %p')}–{end_time.strftime('%I:%M %p')}"
    date_time_str = f"{date_str} at {time_str}"
    
    event_link = event_details.get("link", "N/A") if event_details else "N/A"
    
    # Get first attendee name for greeting
    recipient_name = "there"
    attendees = meeting.get("attendees", [])
    if attendees and attendees[0].get("name"):
        full_name = attendees[0]["name"]
        recipient_name = full_name.split()[0] if full_name else "there"
    
    prompt = CONFIRMATION_EMAIL_TEMPLATE.format(
        subject=meeting["subject"],
        topic=meeting["topic"],
        date_time_str=date_time_str,
        duration_minutes=meeting["duration_minutes"],
        time_zone=meeting["time_zone"],
        event_link=event_link,
        attendee_lines=attendee_lines,
        extra_context=meeting.get("extra_context", ""),
        recipient_name=recipient_name,
        sender_name=FROM_NAME,
    )
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()



# ---------------------------------------------------------------------------
# 5c. Cancellation email for cancel mode
# ---------------------------------------------------------------------------

CANCELLATION_EMAIL_TEMPLATE = """
You are an AI meeting scheduling assistant.

Write a polite, concise CANCELLATION email for a meeting that needs to be cancelled.

Meeting details being cancelled:
- Subject: {subject}
- Date and time: {date_time_str}
- Duration: {duration_minutes} minutes

Attendees:
{attendee_lines}

Requirements for the email:
- Address the recipient by their first name in the greeting (e.g., "Hi {recipient_name},").
- CLEARLY state that the meeting is CANCELLED.
- Include the meeting details (date, time, subject) so they know which one.
- Apologize for any inconvenience.
- Offer to reschedule if appropriate.
- Keep it professional but warm.
- Sign off with: "Best regards, {sender_name}"
- Do NOT include any JSON or technical formatting, just plain email text.
"""


def draft_cancellation_email(event: dict) -> str:
    """
    Ask Gemini to draft a cancellation email for a deleted meeting.
    """
    # Extract event details
    summary = event.get('summary', 'Meeting')
    start_str = event.get('start', '')
    end_str = event.get('end', '')
    
    # Parse start/end times
    try:
        if 'T' in start_str:
            start_time = dt.datetime.fromisoformat(start_str.replace('Z', '+00:00'))
            end_time = dt.datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            start_time = start_time.replace(tzinfo=None)
            end_time = end_time.replace(tzinfo=None)
            
            date_str = start_time.strftime("%A, %B %d, %Y")
            time_str = f"{start_time.strftime('%I:%M %p')}–{end_time.strftime('%I:%M %p')}"
            date_time_str = f"{date_str} at {time_str}"
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
        else:
            # All-day event
            date_time_str = start_str
            duration_minutes = "all-day"
    except:
        date_time_str = f"{start_str}"
        duration_minutes = "unknown"
    
    # Format attendees
    attendees = event.get('attendees', [])
    if attendees:
        attendee_lines = "\n".join([
            f"- {att.get('displayName', att.get('email', 'Unknown'))}"
            for att in attendees
        ])
    else:
        attendee_lines = "- (No other attendees)"
    
    # Get first attendee name for greeting
    recipient_name = "there"
    if attendees and attendees[0]:
        display_name = attendees[0].get('displayName', '')
        if display_name:
            recipient_name = display_name.split()[0]
        else:
            email = attendees[0].get('email', '')
            recipient_name = email.split('@')[0] if '@' in email else "there"
    
    prompt = CANCELLATION_EMAIL_TEMPLATE.format(
        subject=summary,
        date_time_str=date_time_str,
        duration_minutes=duration_minutes,
        attendee_lines=attendee_lines,
        recipient_name=recipient_name,
        sender_name=FROM_NAME,
    )
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()



# ---------------------------------------------------------------------------
# 6. SMTP: send the email
# ---------------------------------------------------------------------------

def send_email_smtp(to_emails: list[str], subject: str, body: str) -> None:
    """
    Send an email via SMTP using the environment variables.
    """
    if not to_emails:
        raise ValueError("No recipient emails provided.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = ", ".join(to_emails)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, to_emails, msg.as_string())


# ---------------------------------------------------------------------------
# 7. Orchestration: the “agent” flow
# ---------------------------------------------------------------------------

def run_scheduler_agent(user_instruction: str, auto_send: bool = True) -> None:
    # Initialize contact memory
    print(">> Initializing contact memory...")
    contacts = ContactMemory(CONTACTS_FILE)
    
    # Try to connect to Google Calendar
    calendar_service = None
    if CALENDAR_AVAILABLE:
        print(">> Connecting to Google Calendar...")
        calendar_service = get_calendar_service()
        if calendar_service:
            print("   ✓ Calendar connected")
        else:
            print("   ⚠ Calendar not available (missing credentials.json or auth failed)")
    else:
        print("   ⚠ Calendar API not installed (run: pip install -r requirements.txt)")
    
    print("\n>> Parsing meeting request with Gemini...")
    meeting = parse_meeting_request(user_instruction, contacts)
    print("Parsed meeting object:")
    print(json.dumps(meeting, indent=2))

    # Save any new contacts from the parsed data
    for attendee in meeting.get("attendees", []):
        name = attendee.get("name")
        email = attendee.get("email")
        if name and email:
            # Check if this is a new contact
            existing = contacts.get_email(name)
            if not existing:
                print(f"   → Saving new contact: {name} <{email}>")
                contacts.add_contact(name, email)
            elif existing != email:
                print(f"   → Updating contact: {name} <{email}>")
                contacts.add_contact(name, email)

    # Determine scheduling mode
    scheduling_mode = meeting.get("scheduling_mode", "proposal")
    print(f"\n>> Scheduling mode: {scheduling_mode.upper()}")
    
    # === CANCEL MODE ===
    if scheduling_mode == "cancel":
        cancel_criteria = meeting.get("cancel_criteria", {})
        
        if not calendar_service:
            print("\n[ERROR] Cannot cancel event: Calendar service unavailable.")
            print("To use event cancellation, you must set up Google Calendar credentials.")
            return
        
        # Determine search time range
        date_range_start_str = cancel_criteria.get("date_range_start")
        date_range_end_str = cancel_criteria.get("date_range_end")
        
        if date_range_start_str and date_range_end_str:
            time_range_start = dt.datetime.fromisoformat(date_range_start_str)
            time_range_end = dt.datetime.fromisoformat(date_range_end_str)
        else:
            # Default: search next 30 days
            time_range_start = dt.datetime.now()
            time_range_end = dt.datetime.now() + dt.timedelta(days=30)
        
        print(f"\n>> Searching for events to cancel...")
        print(f"   Search range: {time_range_start.strftime('%Y-%m-%d')} to {time_range_end.strftime('%Y-%m-%d')}")
        
        matching_events = search_events(calendar_service, cancel_criteria, time_range_start, time_range_end)
        
        if not matching_events:
            print("\n[INFO] No matching events found.")
            print("Try being more specific, e.g., mention attendee name or exact date/time.")
            return
        
        print(f"\n✓ Found {len(matching_events)} matching event(s):\n")
        
        # Display matching events
        for idx, event in enumerate(matching_events, 1):
            start_str = event['start']
            try:
                if 'T' in start_str:
                    start_time = dt.datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    date_time_display = start_time.strftime('%A, %B %d at %I:%M %p')
                else:
                    date_time_display = start_str
            except:
                date_time_display = start_str
            
            attendee_names = ", ".join([a.get('displayName', a.get('email', 'Unknown')) for a in event['attendees'][:3]])
            if len(event['attendees']) > 3:
                attendee_names += f" +{len(event['attendees']) - 3} more"
            
            print(f"  [{idx}] {event['summary']}")
            print(f"      {date_time_display}")
            print(f"      Attendees: {attendee_names}\n")
        
        # If multiple matches, ask user to select
        selected_event = None
        if len(matching_events) == 1:
            selected_event = matching_events[0]
            print(">> This is the only matching event.\n")
        else:
            while True:
                try:
                    selection = input(f"Which event would you like to cancel? [1-{len(matching_events)}] or 'q' to quit: ").strip()
                    if selection.lower() == 'q':
                        print("Cancelled. No events were deleted.")
                        return
                    idx = int(selection)
                    if 1 <= idx <= len(matching_events):
                        selected_event = matching_events[idx - 1]
                        break
                    else:
                        print(f"Please enter a number between 1 and {len(matching_events)}.")
                except ValueError:
                    print("Invalid input. Please enter a number.")
        
        # Confirm deletion
        print("\n" + "=" * 60)
        print("EVENT TO BE CANCELLED:")
        print("=" * 60)
        print(f"Subject: {selected_event['summary']}")
        try:
            start_time = dt.datetime.fromisoformat(selected_event['start'].replace('Z', '+00:00')).replace(tzinfo=None)
            print(f"When: {start_time.strftime('%A, %B %d, %Y at %I:%M %p')}")
        except:
            print(f"When: {selected_event['start']}")
        print(f"Attendees: {len(selected_event['attendees'])} people")
        print("=" * 60)
        
        if not auto_send:
            confirm = input("\n⚠️  Are you sure you want to DELETE this event? [y/N]: ").strip().lower()
            if confirm != 'y':
                print("Cancelled. Event was not deleted.")
                return
        
        # Delete the event
        print("\n>> Deleting calendar event...")
        success = delete_calendar_event(calendar_service, selected_event['id'])
        
        if not success:
            print("\n[ERROR] Failed to delete event. See error above.")
            return
        
        print("   ✓ Event deleted from calendar")
        
        # Draft cancellation email
        print("\n>> Drafting cancellation email with Gemini...")
        email_body = draft_cancellation_email(selected_event)
        print("\nGenerated cancellation email:\n")
        print("=" * 60)
        print(email_body)
        print("=" * 60)
        
        # Get recipient emails from the event
        recipients = [att['email'] for att in selected_event['attendees'] if att.get('email')]
        
        if not recipients:
            print("\n[INFO] No attendees with email addresses. No notification sent.")
            print("Event has been deleted from your calendar.")
            return
        
        if not auto_send:
            answer = input("\nSend this cancellation email to attendees? [y/N]: ").strip().lower()
            if answer != 'y':
                print("Email not sent. Event has been deleted from your calendar.")
                return
        
        print("\n>> Sending cancellation email via SMTP...")
        send_email_smtp(recipients, f"Cancelled: {selected_event['summary']}", email_body)
        print("Done. Cancellation email sent!")
        return
    
    # Collect recipient emails from attendees (for direct/proposal modes)
    recipients = [a["email"] for a in meeting.get("attendees", []) if a.get("email")]
    if not recipients and scheduling_mode != "cancel":
        print("\n[WARNING] No attendee emails parsed. "
              "You must hardcode or manually provide recipients.")
        return
    
    # === DIRECT SCHEDULING MODE ===
    if scheduling_mode == "direct":
        exact_time_str = meeting.get("exact_time")
        if not exact_time_str:
            print("\n[ERROR] Direct mode selected but no exact_time provided. Falling back to proposal mode.")
            scheduling_mode = "proposal"
        else:
            # Prepare event details
            start_time = dt.datetime.fromisoformat(exact_time_str)
            duration_minutes = int(meeting.get("duration_minutes", 30))
            end_time = start_time + dt.timedelta(minutes=duration_minutes)
            
            if not calendar_service:
                print("\n[ERROR] Cannot create calendar event: Calendar service unavailable.")
                print("To use direct scheduling, you must set up Google Calendar credentials.")
                return
            
            # Show what will be created
            print(f"\n>> Will create calendar event:")
            print(f"   Subject: {meeting['subject']}")
            print(f"   When: {start_time.strftime('%A, %B %d at %I:%M %p')}")
            print(f"   Duration: {duration_minutes} minutes")
            print(f"   Attendees: {', '.join([a.get('name', a.get('email', 'Unknown')) for a in meeting.get('attendees', [])])}")
            
            # Ask for confirmation BEFORE creating event
            if not auto_send:
                confirm = input("\n>> Proceed with creating this event and sending confirmation? [y/N]: ").strip().lower()
                if confirm != 'y':
                    print("Aborted. No event was created.")
                    return
            
            # NOW create the calendar event
            print(f"\n>> Creating calendar event...")
            event_details = create_calendar_event(calendar_service, meeting, start_time, end_time)
            
            if not event_details:
                print("\n[ERROR] Failed to create calendar event. See error above.")
                return
            
            print(f"   ✓ Event created: {event_details.get('summary')}")
            print(f"   ✓ Event link: {event_details.get('link')}")
            
            # Draft confirmation email
            print("\n>> Drafting confirmation email with Gemini...")
            email_body = draft_confirmation_email(meeting, event_details, start_time, end_time)
            print("\nGenerated confirmation email:\n")
            print("=" * 60)
            print(email_body)
            print("=" * 60)
            
            # Send email (event already created, so just send)
            print("\n>> Sending confirmation email via SMTP...")
            send_email_smtp(recipients, meeting["subject"], email_body)
            print("Done. Confirmation email sent!")
            return
    
    # === PROPOSAL MODE (default/fallback) ===
    print("\n>> Picking candidate time slots...")
    slots = pick_candidate_slots(meeting, calendar_service)
    print("Candidate slots:")
    print(json.dumps(slots, indent=2))

    print("\n>> Drafting proposal email with Gemini...")
    email_body = draft_email(meeting, slots)
    print("\nGenerated proposal email:\n")
    print("=" * 60)
    print(email_body)
    print("=" * 60)

    if not auto_send:
        answer = input("\nSend this proposal email? [y/N]: ").strip().lower()
        if answer != "y":
            print("Aborted. Email was not sent.")
            return

    print("\n>> Sending proposal email via SMTP...")
    send_email_smtp(recipients, meeting["subject"], email_body)
    print("Done. Proposal email sent!")


# ---------------------------------------------------------------------------
# 8. CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scheduler_agent.py \"Your scheduling instruction...\"")
        sys.exit(1)

    instruction = sys.argv[1]
    run_scheduler_agent(instruction, auto_send=False)
