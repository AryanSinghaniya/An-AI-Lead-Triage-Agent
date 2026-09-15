#!/usr/bin/env python3
"""
AI Lead Triage & Draft Reply Agent
====================================
Reads new form submissions from Google Sheets (or demo CSV),
qualifies each lead with OpenAI Structured Outputs,
saves a personalized draft reply to Gmail Drafts (or local file),
then marks the row processed.

Run:
  python workflow.py          # demo mode, no credentials needed
  python workflow.py --live   # live mode, needs Google credentials + OpenAI key

What this does in 4 steps:
  1. Pull unprocessed rows from the Leads sheet (Status column is blank)
  2. Send each lead to OpenAI — strict schema, no hallucinated fields
  3. Save the draft reply (Gmail Drafts in live mode, output/ folder in demo)
  4. Write status back to the sheet so the row is never processed twice
"""

import os
import sys
import csv
import json
import re
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rich.console import Console
from rich.rule import Rule
from rich.panel import Panel
from rich.table import Table

load_dotenv()

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

console = Console(force_terminal=True, legacy_windows=False)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).parent
LEADS_CSV     = ROOT / "data" / "leads.csv"
OUTPUT_DIR    = ROOT / "output"
PROMPT_FILE   = ROOT / "prompts" / "qualify.txt"
OUTPUT_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA  — strict Pydantic model for OpenAI Structured Outputs
# The AI must fill every field. budget_stated and timeline_stated are nullable
# because the schema explicitly forbids inventing numbers not in the message.
# ══════════════════════════════════════════════════════════════════════════════
class LeadResult(BaseModel):
    intent: str = Field(
        description="One of: qualified | needs_clarification | spam | prompt_injection"
    )
    urgency: str = Field(
        description="One of: high | medium | low | none"
    )
    summary: str = Field(
        description="One sentence: what this person actually wants"
    )
    budget_stated: Optional[str] = Field(
        default=None,
        description="Exact budget the prospect mentioned, or null. Do NOT invent a number."
    )
    timeline_stated: Optional[str] = Field(
        default=None,
        description="Exact deadline or timeline the prospect mentioned, or null."
    )
    missing_info: list[str] = Field(
        default_factory=list,
        description="Critical items absent from the message that are needed to scope the work"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Your confidence in the classification (0.0 to 1.0)"
    )
    draft_subject: str = Field(
        description="Email subject line for the draft reply"
    )
    draft_body: str = Field(
        description="The full plain-text draft reply, under 120 words, no markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Load leads
# In live mode: reads from Google Sheet via gspread
# In demo mode: reads from data/leads.csv
# ══════════════════════════════════════════════════════════════════════════════
def load_leads_demo():
    """Read unprocessed rows from the local CSV (Status column is blank)."""
    leads = []
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if row["status"].strip() == "":
                leads.append({"row_index": i + 1, **row})
    return leads


def load_leads_live():
    """Read unprocessed rows from Google Sheet (Status column is blank)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
        sheet_id   = os.getenv("GOOGLE_SHEET_ID")
        tab_name   = os.getenv("GOOGLE_SHEET_TAB", "Leads")

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds  = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc     = gspread.authorize(creds)
        sheet  = gc.open_by_key(sheet_id).worksheet(tab_name)

        records = sheet.get_all_records()
        leads   = []
        for i, row in enumerate(records):
            if str(row.get("status", "")).strip() == "":
                leads.append({"row_index": i + 1, **{k.lower(): v for k, v in row.items()}})
        return leads, sheet
    except Exception as e:
        console.print(f"[red]Google Sheets error: {e}[/red]")
        console.print("[yellow]Falling back to demo mode.[/yellow]")
        return load_leads_demo(), None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Qualify with AI (Structured Outputs)
# Uses OpenAI's strict schema enforcement via .parse() so the output
# is always a valid LeadResult — no broken JSON, no missing keys.
# Falls back to a deterministic local engine if no API key is set.
# ══════════════════════════════════════════════════════════════════════════════
def qualify_lead(lead: dict) -> LeadResult:
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    user_message  = (
        f"Name: {lead.get('name')}\n"
        f"Email: {lead.get('email')}\n"
        f"Company: {lead.get('company')}\n"
        f"Message:\n{lead.get('message')}"
    )

    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key and api_key != "your_openai_api_key_here":
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            completion = client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                response_format=LeadResult,
                temperature=0.1,
            )
            return completion.choices[0].message.parsed
        except Exception as e:
            console.print(f"  [yellow]API call failed ({e}) — using local engine.[/yellow]")

    # ── Local deterministic fallback (no API key required for demo) ───────────
    return _local_qualify(lead)


def _local_qualify(lead: dict) -> LeadResult:
    """Rule-based qualifier that runs without an API key."""
    body = (lead.get("message") or "").lower()
    name = (lead.get("name") or "there").split()[0]
    company = lead.get("company") or "your company"

    # Security: prompt injection check
    injection_patterns = [
        r"ignore (all )?previous instructions",
        r"you are now in",
        r"discount mode",
        r"coupon code",
    ]
    for pat in injection_patterns:
        if re.search(pat, body, re.IGNORECASE):
            return LeadResult(
                intent="prompt_injection",
                urgency="none",
                summary="Adversarial input detected — possible prompt injection attempt.",
                budget_stated=None,
                timeline_stated=None,
                missing_info=[],
                confidence=0.99,
                draft_subject=f"Re: {lead.get('subject', lead.get('message', '')[:40])}",
                draft_body=(
                    "Thank you for reaching out. We were unable to process your request. "
                    "Please contact us directly with a verified business inquiry."
                ),
            )

    # Spam check
    if any(k in body for k in ["seo backlinks", "guaranteed ranking", "backlink package"]):
        return LeadResult(
            intent="spam",
            urgency="none",
            summary="Unsolicited marketing / SEO spam.",
            budget_stated=None,
            timeline_stated=None,
            missing_info=[],
            confidence=0.98,
            draft_subject="Automated Notice",
            draft_body="Thank you for reaching out. We do not accept unsolicited marketing offers.",
        )

    # MENA / multilingual enterprise lead
    has_budget   = any(k in body for k in ["$", "usd", "aed", "sar", "budget", "capex"])
    has_timeline = any(k in body for k in ["june", "q2", "q3", "week", "month", "deadline", "asap"])
    has_scope    = any(k in body for k in ["zendesk", "netsuite", "sap", "erp", "api", "tier", "pilot", "customs", "invoice", "ticket"])

    if has_budget and has_scope:
        # Well-defined qualified lead
        budget   = None
        timeline = None
        for token in ["$40,000", "$55,000", "AED 180,000", "AED 250,000"]:
            if token.lower().replace(",", "") in body.replace(",", ""):
                budget = token
                break
        for token in ["6 weeks", "June 30", "Q2"]:
            if token.lower() in body:
                timeline = token
                break

        missing = []
        if not any(k in body for k in ["staff", "employees", "team", "person", "seat"]):
            missing.append("Team / user seat count")

        return LeadResult(
            intent="qualified",
            urgency="high",
            summary=f"{company} is looking for a technical automation solution with stated budget and scope.",
            budget_stated=budget,
            timeline_stated=timeline,
            missing_info=missing,
            confidence=0.93,
            draft_subject=f"Re: Your inquiry — let's find a time to talk",
            draft_body=(
                f"Hi {name},\n\n"
                f"Thank you for reaching out. I have read through your requirements for {company} "
                f"and this is exactly the kind of work we specialize in.\n\n"
                f"I would love to set up a 25-minute call to walk through your setup and share "
                f"a few examples from similar projects. Would later this week work for you?\n\n"
                f"Best,\nSolutions Team"
            ),
        )

    # Vague / incomplete inquiry
    missing = ["Estimated budget range", "Specific systems or data involved", "Target timeline"]
    if not has_scope:
        missing.append("What the AI should actually do — specific use case")

    return LeadResult(
        intent="needs_clarification",
        urgency="medium",
        summary=f"{company} has expressed interest in AI automation but has not provided enough detail to scope the work.",
        budget_stated=None,
        timeline_stated=None,
        missing_info=missing,
        confidence=0.75,
        draft_subject=f"Re: Your AI inquiry — a couple of quick questions",
        draft_body=(
            f"Hi {name},\n\n"
            f"Thanks for getting in touch. We would love to help {company} with AI automation.\n\n"
            f"To point you in the right direction, could you share a bit more:\n"
            f"What specific process or tool are you hoping to automate, and do you have a rough "
            f"budget in mind for this quarter?\n\n"
            f"Happy to jump on a quick call once I have a better picture.\n\nBest,\nSolutions Team"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Save draft reply
# In live mode: creates a Gmail draft via the Gmail API
# In demo mode: writes a plain-text file to output/
# ══════════════════════════════════════════════════════════════════════════════
def save_draft_demo(lead: dict, result: LeadResult) -> str:
    """Save draft as a plain-text file in output/."""
    safe_name = re.sub(r"[^\w]", "_", lead.get("name", "lead"))
    filename  = OUTPUT_DIR / f"draft_{safe_name}.txt"
    content   = (
        f"TO:      {lead.get('email')}\n"
        f"SUBJECT: {result.draft_subject}\n"
        f"STATUS:  DRAFT — not sent\n"
        f"SAVED:   {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 60}\n\n"
        f"{result.draft_body}\n"
    )
    filename.write_text(content, encoding="utf-8")
    return str(filename)


def save_draft_live(lead: dict, result: LeadResult) -> str:
    """Create a Gmail draft via the Gmail API."""
    try:
        import base64
        from email.mime.text import MIMEText
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
        sender     = os.getenv("GMAIL_SENDER_ADDRESS", "me")
        scopes     = ["https://mail.google.com/"]
        creds      = Credentials.from_service_account_file(creds_path, scopes=scopes)
        service    = build("gmail", "v1", credentials=creds)

        msg = MIMEText(result.draft_body)
        msg["to"]      = lead.get("email")
        msg["from"]    = sender
        msg["subject"] = result.draft_subject

        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft   = service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        return f"Gmail Draft ID: {draft['id']}"
    except Exception as e:
        console.print(f"  [yellow]Gmail API error: {e} — saving locally instead.[/yellow]")
        return save_draft_demo(lead, result)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Mark row processed
# Writes the status back so the same lead is never processed twice.
# ══════════════════════════════════════════════════════════════════════════════
def mark_processed_demo(row_index: int, status: str):
    """Rewrite the CSV with the updated status for this row."""
    rows = []
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for i, row in enumerate(reader):
            if i + 1 == row_index:
                row["status"] = status
            rows.append(row)

    with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def mark_processed_live(sheet, row_index: int, status: str):
    """Update the Status cell in Google Sheet (column F = index 6)."""
    try:
        sheet.update_cell(row_index + 1, 6, status)  # +1 for header row
    except Exception as e:
        console.print(f"  [yellow]Could not update sheet: {e}[/yellow]")
        mark_processed_demo(row_index, status)


def reset_demo_data():
    """Reset all status values in data/leads.csv to blank and clean output/ drafts."""
    if LEADS_CSV.exists():
        with open(LEADS_CSV, mode="r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            fieldnames = ["timestamp", "name", "email", "company", "message", "status"]
        for row in reader:
            row["status"] = ""
        with open(LEADS_CSV, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(reader)
    # Clean output folder
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("*.txt"):
            try:
                f.unlink()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Use Google Sheets + Gmail (requires credentials)")
    parser.add_argument("--reset", action="store_true", help="Reset leads.csv status column and clean output/ directory")
    args = parser.parse_args()

    if args.reset:
        reset_demo_data()
        console.print("[bold green]Reset complete.[/bold green] All leads in data/leads.csv have blank statuses and output/ drafts cleared.\n")
        return

    console.print()
    console.print(Rule("[bold cyan]AI Lead Triage Agent[/bold cyan]", style="cyan"))
    mode = "LIVE (Google Sheets + Gmail)" if args.live else "DEMO (local CSV + output folder)"
    console.print(f"  Mode: [bold]{mode}[/bold]")
    console.print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    console.print()

    # ── Load leads ────────────────────────────────────────────────────────────
    sheet = None
    if args.live:
        leads, sheet = load_leads_live()
    else:
        leads = load_leads_demo()

    if not leads:
        console.print("[green]No new leads to process.[/green]")
        return

    console.print(f"[bold]Found {len(leads)} unprocessed lead(s).[/bold]\n")

    summary_rows = []

    for lead in leads:
        name    = lead.get("name", "Unknown")
        company = lead.get("company", "")
        console.print(Rule(f"[white]{name}[/white]  ·  [dim]{company}[/dim]", style="dim"))

        # ── Qualify ───────────────────────────────────────────────────────────
        console.print(f"  [dim]Qualifying...[/dim]")
        result = qualify_lead(lead)

        # ── Display result ────────────────────────────────────────────────────
        intent_colors = {
            "qualified":          "green",
            "needs_clarification": "yellow",
            "spam":               "red",
            "prompt_injection":   "red",
        }
        color = intent_colors.get(result.intent, "white")

        console.print(f"  Intent:    [{color}]{result.intent.upper()}[/{color}]")
        console.print(f"  Urgency:   {result.urgency.upper()}")
        console.print(f"  Confidence:{result.confidence * 100:.0f}%")
        console.print(f"  Summary:   {result.summary}")

        if result.budget_stated:
            console.print(f"  Budget:    {result.budget_stated}")
        if result.timeline_stated:
            console.print(f"  Timeline:  {result.timeline_stated}")
        if result.missing_info:
            console.print(f"  [yellow]Missing:   {', '.join(result.missing_info)}[/yellow]")

        # Safeguard: route low-confidence or injection leads to human review
        human_review = result.confidence < 0.80 or result.intent in ("prompt_injection", "spam")
        if human_review:
            console.print(f"  [bold red]→ Flagged for human review[/bold red]")
        else:
            console.print(f"  [bold green]→ Draft ready for SDR one-click send[/bold green]")

        # Show draft
        console.print()
        console.print(Panel(
            f"[bold]Subject:[/bold] {result.draft_subject}\n\n{result.draft_body}",
            title="[cyan]Draft Reply[/cyan]",
            border_style="cyan",
            expand=False,
        ))

        # ── Save draft ────────────────────────────────────────────────────────
        if args.live and sheet:
            saved_to = save_draft_live(lead, result)
        else:
            saved_to = save_draft_demo(lead, result)
        console.print(f"  [dim]Saved: {saved_to}[/dim]")

        # ── Mark processed ────────────────────────────────────────────────────
        status = "Review Required" if human_review else "Draft Ready"
        if args.live and sheet:
            mark_processed_live(sheet, lead["row_index"], status)
        else:
            mark_processed_demo(lead["row_index"], status)

        summary_rows.append((name, company, result.intent, result.urgency,
                              f"{result.confidence*100:.0f}%", status))
        console.print()

    # ── Summary table ─────────────────────────────────────────────────────────
    console.print(Rule("[bold]Pipeline Summary[/bold]", style="cyan"))
    table = Table(box=None, header_style="bold cyan", show_edge=False, pad_edge=False)
    table.add_column("Name",       style="white",  min_width=18)
    table.add_column("Company",    style="dim",    min_width=22)
    table.add_column("Intent",     min_width=20)
    table.add_column("Urgency",    min_width=8)
    table.add_column("Confidence", justify="right")
    table.add_column("Status",     min_width=16)

    for row in summary_rows:
        color = "green" if "qualified" in row[2] else "yellow" if "clarification" in row[2] else "red"
        table.add_row(
            row[0], row[1],
            f"[{color}]{row[2]}[/{color}]",
            row[3], row[4], row[5]
        )
    console.print(table)
    console.print()
    console.print(f"[bold green]Done.[/bold green]  Drafts saved to [underline]output/[/underline]  ·  CSV updated at [underline]data/leads.csv[/underline]")
    console.print()


if __name__ == "__main__":
    main()
