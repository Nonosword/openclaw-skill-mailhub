from __future__ import annotations
import typer
from rich.console import Console
from .config import Settings

app = typer.Typer()
console = Console()

@app.command()
def wizard():
    s = Settings.load()
    s.ensure_dirs()

    console.print("[bold]MailHub setup wizard[/bold]")
    name = typer.prompt("Agent display name", default=s.toggles.agent_display_name)
    s.toggles.agent_display_name = name

    alerts = typer.prompt("Mail alerts mode (off|all|suggested)", default=s.toggles.mail_alerts_mode)
    s.toggles.mail_alerts_mode = alerts

    auto = typer.prompt("Auto reply (off|on)", default=s.toggles.auto_reply)
    s.toggles.auto_reply = auto

    cal = typer.prompt("Calendar management (off|on)", default=s.toggles.calendar_management)
    s.toggles.calendar_management = cal
    if cal == "on":
        s.toggles.calendar_days_window = int(typer.prompt("Calendar window days", default=str(s.toggles.calendar_days_window)))

    bill = typer.prompt("Bill analysis (off|on)", default=s.toggles.bill_analysis)
    s.toggles.bill_analysis = bill

    disclosure = typer.prompt("Disclosure line", default=s.toggles.disclosure_line)
    s.toggles.disclosure_line = disclosure

    s.save()
    console.print({"ok": True, "settings_path": str(s.settings_path)})