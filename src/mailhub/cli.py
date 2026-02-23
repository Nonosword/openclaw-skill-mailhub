import typer
from rich.console import Console

from .config import Settings
from .pipelines.ingest import inbox_poll, inbox_ingest_day
from .pipelines.triage import triage_day, triage_suggest
from .pipelines.reply import reply_prepare, reply_send, reply_auto
from .pipelines.calendar import agenda
from .pipelines.billing import billing_detect, billing_analyze, billing_month
from .providers.google_gmail import auth_google
from .providers.ms_graph import auth_microsoft
from .providers.imap_smtp import auth_imap
from .providers.caldav import auth_caldav
from .providers.carddav import auth_carddav
from .cli_wizard import app as wizard_app

app = typer.Typer(no_args_is_help=True)
console = Console()

app.add_typer(wizard_app, name="wizard")

@app.command()
def doctor():
    """Basic diagnostics for state dir, db, keychain access."""
    s = Settings.load()
    s.ensure_dirs()
    from .store import DB
    DB(s.db_path).init()
    console.print({"state_dir": str(s.state_dir), "db": str(s.db_path)})

auth_app = typer.Typer()
app.add_typer(auth_app, name="auth")

@auth_app.command("google")
def _auth_google(scopes: str = "gmail,calendar,contacts"):
    Settings.load().ensure_dirs()
    auth_google(scopes=scopes)

@auth_app.command("microsoft")
def _auth_ms(scopes: str = "mail,calendar,contacts"):
    Settings.load().ensure_dirs()
    auth_microsoft(scopes=scopes)

@auth_app.command("imap")
def _auth_imap(email: str, imap_host: str, smtp_host: str):
    Settings.load().ensure_dirs()
    auth_imap(email=email, imap_host=imap_host, smtp_host=smtp_host)

@auth_app.command("caldav")
def _auth_caldav(username: str, host: str):
    Settings.load().ensure_dirs()
    auth_caldav(username=username, host=host)

@auth_app.command("carddav")
def _auth_carddav(username: str, host: str):
    Settings.load().ensure_dirs()
    auth_carddav(username=username, host=host)

inbox_app = typer.Typer()
app.add_typer(inbox_app, name="inbox")

@inbox_app.command("poll")
def _poll(since: str = "15m", mode: str = "alerts"):
    console.print(inbox_poll(since=since, mode=mode))

@inbox_app.command("ingest")
def _ingest(date: str = "today"):
    console.print(inbox_ingest_day(date=date))

triage_app = typer.Typer()
app.add_typer(triage_app, name="triage")

@triage_app.command("day")
def _triage_day(date: str = "today"):
    console.print(triage_day(date=date))

@triage_app.command("suggest")
def _triage_suggest(since: str = "15m"):
    console.print(triage_suggest(since=since))

reply_app = typer.Typer()
app.add_typer(reply_app, name="reply")

@reply_app.command("prepare")
def _reply_prepare(index: int):
    console.print(reply_prepare(index=index))

@reply_app.command("send")
def _reply_send(index: int, confirm_text: str):
    console.print(reply_send(index=index, confirm_text=confirm_text))

@reply_app.command("auto")
def _reply_auto(since: str = "15m", dry_run: bool = True):
    console.print(reply_auto(since=since, dry_run=dry_run))

cal_app = typer.Typer()
app.add_typer(cal_app, name="cal")

@cal_app.command("agenda")
def _agenda(days: int = 3):
    console.print(agenda(days=days))

billing_app = typer.Typer()
app.add_typer(billing_app, name="billing")

@billing_app.command("detect")
def _detect(since: str = "30d"):
    console.print(billing_detect(since=since))

@billing_app.command("analyze")
def _analyze(statement_id: str):
    console.print(billing_analyze(statement_id=statement_id))

@billing_app.command("month")
def _month(month: str):
    console.print(billing_month(month=month))

@app.command()
def settings_show():
    """Print current settings."""
    s = Settings.load()
    console.print(s.as_dict())

@app.command()
def settings_set(key: str, value: str):
    """Set a toggle key in settings."""
    s = Settings.load()
    if not hasattr(s.toggles, key):
        raise typer.BadParameter(f"Unknown key: {key}")
    # basic typing
    cur = getattr(s.toggles, key)
    if isinstance(cur, int):
        setattr(s.toggles, key, int(value))
    else:
        setattr(s.toggles, key, value)
    s.save()
    console.print({"ok": True, "set": {key: value}})