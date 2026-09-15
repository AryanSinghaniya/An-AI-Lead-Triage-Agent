# AI Lead Triage Agent

**AI reads every inbound form submission, qualifies it, and saves a personalized draft reply — in under 3 seconds per lead.**

Tools used: Google Sheets, OpenAI `gpt-4o-mini`, Gmail, Python.  
Run from terminal. No UI. No visual workflow builder.

---

## What This Builds

You have a contact form on your website. Every time someone fills it out, it lands in a Google Sheet. Right now someone on your team opens that sheet, reads the message, decides if it is real, figures out what is missing, and writes a reply. That takes 20 to 40 minutes per lead and often happens hours after the submission.

This script runs against that sheet every time you want. It reads every row with a blank Status column, sends each one through OpenAI with a strict schema that forces the model to extract only what was actually written (never inventing budget numbers or deadlines), generates a plain-text draft reply, saves it to Gmail Drafts, and writes the status back to the sheet. The next time you run it, already-processed rows are skipped.

```
Google Sheet (form submissions)
        ↓
workflow.py  — run on demand or on a cron
        ↓
OpenAI gpt-4o-mini (Structured Outputs — strict Pydantic schema)
  → intent classification
  → explicit budget and timeline extraction
  → missing information checklist
  → personalized draft reply
        ↓
Gmail Drafts  (or  output/  folder in demo mode)
        ↓
Sheet row status → "Draft Ready" or "Review Required"
```

---

## Part 1. The AI Capability

**OpenAI Structured Outputs with strict Pydantic schema enforcement**

Released: August 2024 (strict mode via `response_format` with Pydantic `BaseModel`)  
Reference: https://platform.openai.com/docs/guides/structured-outputs

Before this, getting reliable JSON out of a language model meant prompt-engineering around schema drift, writing parsers to handle malformed outputs, and catching runtime errors when a required key was missing. With Structured Outputs in strict mode, the model is constrained at the token level to only produce values that conform to your schema. Every field is always present. Every enum value is always valid. The output is a Python object, not a string you parse.

For a lead triage workflow this matters because the downstream steps (saving to a CRM, creating a Gmail draft, updating a sheet) all depend on structured data. A malformed response mid-batch breaks everything. Strict outputs mean the batch completes or the API call fails — never silent corruption.

**The caveat:** The model can only extract what is in the message. If a prospect writes "need AI for my business ASAP" and the schema has a `budget_stated` field, the model cannot invent a number. But it can still make a wrong classification — for example, treating a very short but legitimate inquiry as spam. This is why the schema includes a `confidence` score and why anything under 80% is routed to human review before the draft is touched.

---

## Part 2. The Business Pain

A 50 to 300 person service or product business — a managed IT firm, a digital consultancy, a logistics company — receives inbound leads through its website. Some of those leads are worth $40,000. Some are spam. Most are somewhere in between, vague on scope and missing the information you need to actually scope the work.

The operational problem is not that the leads are bad. It is that the same person who should be on the phone closing deals is instead reading emails, categorizing them, and drafting polite "could you tell us more about your budget" replies.

At 50 to 300 people you do not have a dedicated SDR team. You have someone who does this in addition to everything else. Leads that arrive after hours sit until morning. A Harvard Business Review study found that leads responded to within five minutes are 21 times more likely to qualify than those responded to after 30 minutes. By the time your team replies, the prospect has already submitted a form to a competitor.

---

## Part 3. Why This Pairing

The reason Structured Outputs specifically solves this pain — rather than just asking the model to "reply to this email" — is that the business logic is deterministic even if the lead content is not.

- Every lead needs the same fields checked: intent, urgency, budget stated, timeline stated, what is missing.
- Every draft needs the same constraints applied: under 120 words, no pricing promises, address by first name.
- The Sheet status update is binary: processed or not.

Structured Outputs let you encode all of that as a schema rather than as prose instructions. The model fills the schema. The script acts on the schema. There is no ambiguity about what the model returned.

---

## How to Run It

### Demo mode (no credentials needed)

```bash
pip install -r requirements.txt
python workflow.py
```

Reads from `data/leads.csv`. Saves drafts to `output/`. Updates the CSV.

### Live mode (Google Sheets + Gmail)

```bash
cp .env.example .env
# fill in OPENAI_API_KEY, GOOGLE_CREDENTIALS_PATH, GOOGLE_SHEET_ID, GMAIL_SENDER_ADDRESS

python workflow.py --live
```

Reads unprocessed rows from your Google Sheet. Creates Gmail drafts. Updates the sheet.

### To run every 30 minutes (cron)

```
*/30 * * * * cd /path/to/mlproject && python workflow.py --live >> logs/run.log 2>&1
```

---

## Step-by-Step Walkthrough

### Step 1. The Sheet

Your contact form (Tally, Typeform, or a plain HTML form) posts submissions to a Google Sheet with these columns:

| timestamp | name | email | company | message | status |
|---|---|---|---|---|---|
| 2026-09-14 08:12 | Sarah Jenkins | sjenkins@... | Apex Managed Services | Hi, we are an 85-person... | _(blank)_ |

The `status` column is blank on arrival. That is the trigger.

### Step 2. The Qualification

For each blank-status row, the script sends the message to `gpt-4o-mini` with `response_format=LeadResult` — a Pydantic model that the API is forced to match exactly.

The model returns:

```json
{
  "intent": "qualified",
  "urgency": "high",
  "summary": "Apex Managed Services wants Zendesk ticket triage...",
  "budget_stated": "$40,000 to $55,000",
  "timeline_stated": "6 weeks",
  "missing_info": ["Monthly ticket volume"],
  "confidence": 0.96,
  "draft_subject": "Re: Your inquiry — let's find a time to talk",
  "draft_body": "Hi Sarah, ..."
}
```

If `budget_stated` is not in the message, the field is `null`. The model cannot guess.

### Step 3. The Safeguard

After the AI returns, a deterministic rule runs before anything is saved:

```python
human_review = result.confidence < 0.80 or result.intent in ("prompt_injection", "spam")
```

- Confidence below 80%: status set to `Review Required`, no draft goes anywhere.
- Prompt injection or spam: quarantined immediately.
- Qualified at high confidence: status set to `Draft Ready`.

The draft is never auto-sent. It lands in Gmail Drafts. The SDR reviews it and hits send.

### Step 4. The Output

- Gmail Draft created (live mode) or `.txt` file saved to `output/` (demo mode)
- Sheet row status updated to `Draft Ready` or `Review Required`
- Running the script again skips every row with a non-blank status

---

## The Four Test Cases

| Lead | Scenario | Result | Safeguard |
|---|---|---|---|
| Sarah Jenkins | Full budget, scope, timeline stated | `qualified` · 96% | Draft Ready — auto |
| Dave Miller | "Need AI ASAP" — no detail | `needs_clarification` · 75% | Review Required |
| Tariq Al-Mansoor | Bilingual MENA enterprise, AED budget | `qualified` · 93% | Draft Ready — auto |
| BlackHat Bot | Prompt injection attempt | `prompt_injection` · 99% | Review Required + quarantined |

---

## Safety Design

| Risk | How it is handled |
|---|---|
| Model invents a budget | `budget_stated` is `Optional[str]` — null if not in message |
| Low-confidence classification | Anything below 0.80 goes to human review |
| Prompt injection in the message | Regex pre-filter + intent classification catches it |
| Email auto-sent before review | Drafts only. Nothing is ever sent automatically. |
| Same lead processed twice | Status column is written after processing — blank-status check is the gate |

---

## Limitations

This version does not:
- Watch the sheet in real time — it runs on demand or cron, not on a webhook trigger
- Enrich the lead with company data (headcount, funding) — that would require a search API call
- Handle attachments or linked documents in the form submission
- Localize the draft reply to Arabic for MENA leads — the draft is English only

---

## What I Would Add Next

1. **Webhook trigger** — instead of cron, a small Cloud Function that fires the moment Tally posts a new row
2. **Clearbit / Apollo enrichment** — pull company size and funding before qualifying so the model has more signal
3. **Arabic draft support** — detect language from the message and switch the prompt accordingly

---

## Other Capability → Pain Pairings I Considered

| Capability | Pain | Why I did not build it |
|---|---|---|
| Claude 3.7 extended thinking | Contract redline review | Too niche for a 1-day build; needs a real legal document corpus |
| Whisper + GPT-4o | WhatsApp voice note triage (MENA) | Strong idea, but audio files are harder to screenshot for a written submission |
| Gemini 2.0 native audio | Meeting notes without a note-taker | Already covered by the reference articles; less differentiated |
| Search-grounded GPT-4o | Vendor due diligence before a discovery call | Live web search results are non-deterministic — harder to show reproducible output |

The lead triage workflow won because it is the most universal pain (every service business has a contact form), the output is immediately visible (a Gmail draft the SDR can read), and Structured Outputs add real engineering value that a plain prompt-and-parse approach does not.
