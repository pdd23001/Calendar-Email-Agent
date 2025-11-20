# Meeting Scheduler Agent

An intelligent meeting scheduling assistant powered by Google Gemini AI that:
- **Parses natural language** scheduling requests
- **Remembers contacts** - saves email addresses for future use
- **Checks your calendar** - integrates with Google Calendar to find free time slots
- **Drafts emails** - creates professional scheduling emails
- **Sends via SMTP** - delivers emails through Gmail or other SMTP servers

## Features

### 🧠 Contact Memory
The system remembers people you've scheduled with:
- First time: `python scheduler.py "Schedule meeting with Alice (alice@test.com)"`
- Next time: `python scheduler.py "Schedule meeting with Alice"` ← Email auto-filled!

Contacts are stored in `contacts.json` for persistence.

### 📅 Google Calendar Integration
- Checks your real calendar availability
- Only suggests free time slots  
- **Creates calendar events** when you provide exact times
- Gracefully falls back if calendar unavailable

### 🎯 Two Scheduling Modes

**Proposal Mode** (suggest time slots):
- Use when: "Schedule meeting next week" or "tomorrow afternoon"
- Behavior: Finds 2-3 free slots → Sends proposal email

**Direct Mode** (create calendar event):
- Use when: "Schedule meeting tomorrow at 2pm" or "Monday at 10am"
- Behavior: Creates calendar event → Sends confirmation email
- **Requires Google Calendar credentials**

### ✉️ Smart Email Drafting
- Generates professional emails (proposals or confirmations)
- Includes calendar event links in direct mode
- Asks for confirmation before sending (by default)

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Create and Configure .env File

The `.env` file stores all your API keys and configuration. Create it in the project directory:

```bash
cd "Your Project Directory"
touch .env (or you can create it manually)
```

Then open `.env` in your text editor and add the following:

#### Required Variables

```bash
# === Gemini AI API Key ===
# Get your API key from: https://aistudio.google.com/
# Click "Get API key" → Create API key → Copy it here
GEMINI_API_KEY=your_actual_gemini_api_key_here

# === Gmail SMTP Configuration ===
# Your Gmail address (the account sending emails)
SMTP_USERNAME=your.email@gmail.com

# Gmail App Password (NOT your regular Gmail password!)
# How to generate:
# 1. Go to https://myaccount.google.com/security
# 2. Enable 2-Step Verification (if not already enabled)
# 3. Go to App passwords: https://myaccount.google.com/apppasswords
# 4. Select app: Mail
# 5. Select device: Other (Custom name) → "Meeting Scheduler"
# 6. Click Generate → Copy the 16-character password
SMTP_PASSWORD=your_16_character_app_password_here
```

#### Optional Variables (Already Have Defaults)

```bash
# === SMTP Server Settings ===
# Only change if not using Gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587

# === Email Sender Identity ===
# What recipients will see as the sender
FROM_EMAIL=your.email@gmail.com
FROM_NAME=Your Name

# === Time Zone ===
# Default timezone for scheduling
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
DEFAULT_TIME_ZONE=America/New_York

# === Google Calendar Settings ===
# Which calendar to use (usually leave as "primary")
CALENDAR_ID=primary

# === Contact Storage ===
# Where to save contact email addresses
CONTACTS_FILE=./contacts.json
```

**Important Notes:**
- ⚠️ **Never commit `.env` to Git** - it's already in `.gitignore`
- 🔑 **Use App Password, not Gmail password** - regular password won't work
- ✅ **No quotes needed** - values are used as-is after the `=` sign

**Example Complete .env File:**
```bash
GEMINI_API_KEY=AIzaSyD-9tIGbxQzXcVmR1Ko8sN3jW4pLmH7kF0
SMTP_USERNAME=john.doe@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
FROM_EMAIL=john.doe@gmail.com
FROM_NAME=John Doe
DEFAULT_TIME_ZONE=America/New_York
```

### 3. Set Up Google Calendar (Optional but Recommended)

To enable calendar integration:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable the **Google Calendar API**
4. Create OAuth 2.0 credentials:
   - Application type: **Desktop app**
   - Download the credentials JSON file
5. Save as `credentials.json` in the project directory
6. First run will open a browser for authorization
7. `token.json` will be auto-created for future runs

**Without calendar setup**: The system will still work but won't check your real calendar availability.

### 4. Test the System

```bash
python scheduler.py "Schedule a 30 minute sync with Bob (bob@example.com) next Monday morning"
```

## Usage Examples

### Proposal Mode (suggest time slots)
```bash
# First time scheduling with someone
python scheduler.py "Schedule 1 hour meeting with Alice (alice@company.com) next week afternoons about Q4 planning"

# Second time - email remembered!
python scheduler.py "Schedule 30min followup with Alice tomorrow morning"

# Multiple attendees
python scheduler.py "Schedule team sync with Alice and Bob next Friday, 30 minutes"

# Specific time preferences
python scheduler.py "Schedule meeting with Carol (carol@test.com) next week, mornings only, 45 minutes"
```

### Direct Mode (create calendar event + send confirmation)
```bash
# Exact time - creates event immediately
python scheduler.py "Schedule meeting with Alice tomorrow at 2pm for 1 hour"

# Specific date and time
python scheduler.py "Schedule with Bob on Monday November 25 at 10am, 30 minutes"

# With topic
python scheduler.py "Schedule Q4 planning with Alice next Friday at 3pm for 2 hours"
```

**Note**: Direct mode requires Google Calendar credentials. The system automatically detects which mode to use based on whether you provide an exact time.

## How It Works

### Proposal Mode
1. **Contact Memory**: Loads saved contacts from `contacts.json`
2. **Calendar Connection**: Connects to Google Calendar (if configured)
3. **Parse Request**: Gemini AI extracts meeting details and detects proposal mode
4. **Save Contacts**: Any new emails are automatically saved
5. **Find Slots**: Checks calendar and finds 2-3 free time slots
6. **Draft Proposal**: Gemini AI writes a proposal email with time options
7. **Confirmation**: Shows email and asks for approval
8. **Send**: Delivers email via SMTP

### Direct Mode
1-4. Same as proposal mode
5. **Create Event**: Creates calendar event at the exact time you specified
6. **Draft Confirmation**: Gemini AI writes a confirmation email with event link
7-8. Same as proposal mode

**Auto-detection**: The system uses AI to detect if you've given an exact time (direct) or a range (proposal).

## Feature Walkthroughs

### 📊 Complete System Flow

```mermaid
graph TD
    START[User Command] --> INIT[Initialize System]
    INIT --> CONTACTS[Load Contact Memory]
    CONTACTS --> CAL[Connect to Calendar]
    CAL --> PARSE[Parse with Gemini AI]
    
    PARSE --> SAVE{New Contact?}
    SAVE -->|Yes| SAVEACT[Save to contacts.json]
    SAVE -->|No| MODE
    SAVEACT --> MODE
    
    MODE{Scheduling Mode?}
    
    MODE -->|Direct| DIRECT[Direct Mode Path]
    MODE -->|Proposal| PROPOSAL[Proposal Mode Path]
    
    DIRECT --> DCAL{Calendar OK?}
    DCAL -->|No| ERROR[Error: Need Calendar]
    DCAL -->|Yes| CREATE[Create Event]
    CREATE --> DCONF[Draft Confirmation]
    DCONF --> DSEND[Send Email]
    DSEND --> DONE1[Done]
    
    PROPOSAL --> SLOTS[Find Free Slots]
    SLOTS --> PPROP[Draft Proposal]
    PPROP --> PSEND[Send Email]
    PSEND --> DONE2[Done]
    
    style DIRECT fill:#90EE90,color:#000
    style CREATE fill:#90EE90,color:#000
    style PROPOSAL fill:#87CEEB,color:#000
    style SLOTS fill:#87CEEB,color:#000
    style ERROR fill:#FFB6C6,color:#000
```

---

### 🧠 Contact Memory Feature

**How It Works:**

1. **First Mention**: When you schedule a meeting and include an email:
   ```bash
   python3 scheduler.py "Schedule with Alice (alice@company.com) tomorrow"
   ```
   - The system extracts name="Alice" and email="alice@company.com"
   - Saves to `contacts.json`: `{"alice": "alice@company.com"}`
   - Prints: `→ Saving new contact: Alice <alice@company.com>`

2. **Subsequent Uses**: Next time you mention just the name:
   ```bash
   python3 scheduler.py "Schedule followup with Alice next week"
   ```
   - AI looks up "Alice" in contact memory
   - Auto-fills email from `contacts.json`
   - No need to type the email again!

3. **Fuzzy Matching**: Works with name variations:
   - Saved as: "alice smith"
   - Works with: "Alice", "alice", "Alice Smith"

**Storage**: All contacts are stored in `contacts.json` as name→email mappings, persisted across runs.

**Flow Diagram:**
```mermaid
graph LR
    A[User: Schedule with Alice] --> B[Parse Request]
    B --> C{Contact Exists?}
    C -->|No| D[Save to contacts.json]
    C -->|Yes| E[Use Existing Email]
    D --> F[Schedule Meeting]
    E --> F
    
    G[Next Time: Schedule with Alice] --> H[Parse Request]
    H --> I[Look up in contacts.json]
    I --> J[Auto-fill email]
    J --> F
```

---

### ✅ Direct Scheduling Feature

**System Flow - Mode Detection:**
```mermaid
graph TD
    A[User Input] --> B[Parse with Gemini AI]
    B --> C{Exact Time Detected?}
    C -->|Yes| D[Direct Mode]
    C -->|No| E[Proposal Mode]
    
    D --> F[Create Calendar Event]
    F --> G[Draft Confirmation Email]
    
    E --> I[Find Free Slots]
    I --> J[Draft Proposal Email]
    J --> K[Send with Options]
    
    style D fill:#90EE90,color:#000
    style E fill:#87CEEB,color:#000
```

**How It Works:**

**Example 1: Direct Mode (Exact Time)**
```bash
python3 scheduler.py "Schedule with Bob tomorrow at 2pm for 1 hour"
```

What happens:
1. AI detects exact time → **Direct Mode**
2. Prints: `>> Scheduling mode: DIRECT`
3. Creates Google Calendar event at 2pm tomorrow
4. Prints event link: `✓ Event link: https://calendar.google.com/...`
5. Drafts **confirmation email** with event details and link
6. Sends to Bob

Result: ✅ Event created + Confirmation sent

**Detailed Flow:**
```mermaid
graph TD
    A[Input: Schedule with Bob tomorrow at 2pm] --> B[Initialize Contact Memory]
    B --> C[Connect to Calendar]
    C --> D[Parse with Gemini AI]
    D --> E[Detect: direct mode]
    E --> F[Extract: exact_time]
    F --> G{Calendar Available?}
    G -->|No| H[ERROR: Need credentials]
    G -->|Yes| I[Create Calendar Event]
    I --> J[Event Created]
    J --> K[Draft Confirmation Email]
    K --> L[Include Event Link]
    L --> M{User Approval?}
    M -->|No| N[Aborted]
    M -->|Yes| O[Send Email via SMTP]
    O --> P[Done]
    
    style I fill:#90EE90,color:#000
    style J fill:#90EE90,color:#000
    style P fill:#90EE90,color:#000
    style H fill:#FFB6C6,color:#000
```

---

**Example 2: Proposal Mode (Time Range)**
```bash
python3 scheduler.py "Schedule with Bob tomorrow afternoon"
```

What happens:
1. AI detects vague time → **Proposal Mode**
2. Prints: `>> Scheduling mode: PROPOSAL`
3. Checks your calendar for free slots tomorrow afternoon
4. Finds 2-3 available slots (e.g., 1pm, 2pm, 3pm)
5. Drafts **proposal email** with time options
6. Sends to Bob for selection

Result: 📋 Options sent, no event created yet

**Detailed Flow:**
```mermaid
graph TD
    A[Input: Schedule with Bob tomorrow afternoon] --> B[Initialize Contact Memory]
    B --> C[Connect to Calendar]
    C --> D[Parse with Gemini AI]
    D --> E[Detect: proposal mode]
    E --> F[Extract: earliest_start, latest_end]
    F --> G{Calendar Available?}
    G -->|No| H[Use time-based slots]
    G -->|Yes| I[Query Free/Busy API]
    I --> J[Get busy periods]
    H --> K[Generate candidate slots]
    J --> K
    K --> L[Filter out conflicts]
    L --> M[Select 2-3 best slots]
    M --> N[Draft Proposal Email]
    N --> O[List time options]
    O --> P{User Approval?}
    P -->|No| Q[Aborted]
    P -->|Yes| R[Send Email via SMTP]
    R --> S[Done]
    
    style M fill:#87CEEB,color:#000
    style N fill:#87CEEB,color:#000
    style S fill:#87CEEB,color:#000
    style Q fill:#FFB6C6,color:#000
```


---

**Key Differences:**

| Input | Mode | Creates Event? | Email Type |
|-------|------|----------------|------------|
| "tomorrow at 2pm" | Direct | ✅ Yes | Confirmation |
| "tomorrow afternoon" | Proposal | ❌ No | Proposal with options |
| "Monday at 10am" | Direct | ✅ Yes | Confirmation |
| "next week mornings" | Proposal | ❌ No | Proposal with options |

**Requirements**: Direct mode requires Google Calendar credentials (`credentials.json`). Without it, the system will show an error and cannot create events.

## File Structure

```
.
├── scheduler.py          # Main application
├── requirements.txt      # Python dependencies
├── .env                  # Configuration (you fill this in)
├── .gitignore           # Protects sensitive files
├── contacts.json        # Auto-created contact storage
├── credentials.json     # Google OAuth credentials (you provide)
└── token.json           # Auto-created after first auth
```

## Security Notes

- ⚠️ **Never commit** `.env`, `credentials.json`, `token.json`, or `contacts.json`
- ✅ These are already in `.gitignore`
- 🔑 Use Gmail **App Passwords**, not your regular password
- 🔐 SMTP and API credentials are sensitive - keep them private

## Troubleshooting

**"Missing GEMINI_API_KEY"**
- Get your key from https://aistudio.google.com/
- Add to `.env` file

**"SMTP authentication failed"**
- Use Gmail App Password, not regular password
- Enable 2FA on Google Account first
- Generate App Password from Google Account > Security

**"Calendar not available"**
- Check `credentials.json` exists
- Run `pip install -r requirements.txt` again
- Delete `token.json` and re-authenticate

**"No attendee emails parsed"**
- First time: include email like `Alice (alice@test.com)`
- Check `contacts.json` to see saved contacts

## Future Enhancements

- Multi-timezone support
- Integration with other calendar providers
- Web interface
- Conflict resolution when all slots are busy

---
