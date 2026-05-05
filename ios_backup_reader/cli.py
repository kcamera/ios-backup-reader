"""ios-backup-reader CLI entry point."""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


# ---------------------------------------------------------------------------
# Path prompt with tab-completion
# ---------------------------------------------------------------------------

def _prompt_for_path() -> Path:
    try:
        from prompt_toolkit import prompt
        from prompt_toolkit.completion import PathCompleter

        raw = prompt(
            "Backup path: ",
            completer=PathCompleter(expanduser=True),
        ).strip()
    except (ImportError, KeyboardInterrupt, EOFError, OSError):
        raw = input("Backup path: ").strip()

    return Path(raw).expanduser()


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--path", "-p",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Path to iOS backup directory. Prompted interactively if omitted.",
)
@click.pass_context
def cli(ctx: click.Context, path: Optional[Path]) -> None:
    """ios-backup-reader — browse and export iOS backup data."""
    ctx.ensure_object(dict)
    # Defer path prompting until a command actually needs it (skip during --help parsing).
    ctx.obj["path"] = path
    ctx.obj["path_resolved"] = False


def _resolve_path(ctx: click.Context) -> Path:
    if not ctx.obj["path_resolved"]:
        path = ctx.obj["path"]
        if path is None:
            path = _prompt_for_path()
            if not path.is_dir():
                console.print(f"[red]Not a directory: {path}[/red]")
                sys.exit(1)
        ctx.obj["path"] = path
        ctx.obj["path_resolved"] = True
    return ctx.obj["path"]


def _get_backup_raw(ctx: click.Context):
    """Return a Backup without decryption — for metadata-only commands like `info`."""
    from .backup import Backup, BackupError
    path = _resolve_path(ctx)
    try:
        return Backup(path)
    except BackupError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


def _get_backup(ctx: click.Context):
    """Return a ready-to-use Backup, prompting for a passphrase if encrypted."""
    from .backup import Backup, BackupError
    path = _resolve_path(ctx)
    try:
        backup = Backup(path)
    except BackupError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not backup.is_encrypted():
        return backup

    # Done with the lightweight Backup — close it before creating DecryptedBackup
    backup.close()

    # Encrypted — need iphone-backup-decrypt
    try:
        import iphone_backup_decrypt  # noqa: F401
    except ImportError:
        console.print(
            "[red]Error:[/red] This backup is encrypted.\n"
            '[yellow]Install decryption support:[/yellow] '
            'pip install "ios-backup-reader[encrypted]"'
        )
        sys.exit(1)

    from .backup import DecryptedBackup
    passphrase = click.prompt("Backup passphrase", hide_input=True)
    try:
        return DecryptedBackup(path, passphrase)
    except BackupError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show backup metadata (device, iOS version, encryption status)."""
    backup = _get_backup_raw(ctx)

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")

    encrypted = backup.is_encrypted()
    enc_str = "[red]Yes — decrypt required[/red]" if encrypted else "[green]No[/green]"

    table.add_row("Device", backup.device_name())
    table.add_row("iOS Version", backup.ios_version())
    table.add_row("Phone Number", backup.phone_number())
    table.add_row("Last Backup", backup.last_backup_date())
    table.add_row("Encrypted", enc_str)
    table.add_row("Path", str(ctx.obj["path"]))

    console.print(table)

    if encrypted:
        try:
            import iphone_backup_decrypt  # noqa: F401
            decrypt_available = True
        except ImportError:
            decrypt_available = False

        if decrypt_available:
            console.print(
                "[yellow]Encrypted backup — run any data command and you will be prompted "
                "for the passphrase.[/yellow]"
            )
        else:
            console.print(
                "[yellow]Encrypted backups require decryption support. "
                r'Install with: pip install "ios-backup-reader[encrypted]"[/yellow]'
            )


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("pattern", required=False, default=None)
@click.option(
    "--search", "-s",
    "search_query",
    default=None,
    help="Full-text search across all messages (mutually exclusive with PATTERN).",
)
@click.pass_context
def messages(ctx: click.Context, pattern: Optional[str], search_query: Optional[str]) -> None:
    """
    List conversations, browse by contact, or full-text search.

    \b
    Examples:
      messages                       list all conversations
      messages "*john*"              browse messages with matching contacts (glob)
      messages --search "hello"      search message text across all chats
    """
    if pattern and search_query:
        console.print("[red]Error:[/red] use either PATTERN or --search, not both.")
        sys.exit(1)

    backup = _get_backup(ctx)
    with console.status("Loading messages…"):
        from .parsers import messages as msg_parser
        chats = msg_parser.load(backup)

    if not chats:
        console.print("[yellow]No messages found.[/yellow]")
        return

    if search_query:
        _full_text_search(chats, search_query)
        return

    if pattern is None:
        # List all conversations
        table = Table(title="Conversations", box=box.SIMPLE_HEAD)
        table.add_column("ID", style="dim", justify="right")
        table.add_column("Contact / Identifier", style="cyan")
        table.add_column("Display Name")
        table.add_column("Service", style="dim")
        table.add_column("Messages", justify="right")

        for c in chats:
            table.add_row(
                str(c.id),
                c.chat_identifier,
                c.display_name or "—",
                c.service,
                str(c.message_count),
            )
        console.print(table)
    else:
        # Filter by glob pattern against chat_identifier and display_name
        pat = pattern.lower()
        matched = [
            c for c in chats
            if fnmatch.fnmatch(c.chat_identifier.lower(), pat)
            or fnmatch.fnmatch(c.display_name.lower(), pat)
        ]
        if not matched:
            console.print(f"[yellow]No conversations matching '[bold]{pattern}[/bold]'.[/yellow]")
            return

        for chat in matched:
            _print_chat(chat)


def _print_chat(chat) -> None:
    title = chat.display_name or chat.chat_identifier
    console.rule(f"[bold cyan]{title}[/bold cyan] ({chat.service})")

    is_group = len(chat.handles) > 1

    for msg in chat.messages:
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
        if msg.is_from_me:
            sender = "[bold green]Me[/bold green]"
        elif is_group and msg.handle_id and msg.handle_id in chat.handles:
            who = chat.handles[msg.handle_id]
            sender = f"[bold blue]{who}[/bold blue]"
        else:
            sender = "[bold blue]Them[/bold blue]"
        text = msg.text or ""

        # Markers
        recovered_str = " [dim yellow](deleted)[/dim yellow]" if msg.is_recovered else ""

        # Attachments
        att_str = ""
        if msg.attachments:
            names = [a.transfer_name or Path(a.filename).name for a in msg.attachments]
            att_str = f" [dim][{', '.join(names)}][/dim]"

        console.print(f"[dim]{date_str}[/dim] {sender}: {text}{att_str}{recovered_str}")


def _full_text_search(chats, query: str) -> None:
    q = query.lower()
    results: list[tuple] = []  # (chat, message)
    for chat in chats:
        for msg in chat.messages:
            if msg.text and q in msg.text.lower():
                results.append((chat, msg))

    if not results:
        console.print(f"[yellow]No messages containing '[bold]{query}[/bold]'.[/yellow]")
        return

    table = Table(title=f"Search: {query}", box=box.SIMPLE_HEAD)
    table.add_column("Date", style="dim")
    table.add_column("Contact")
    table.add_column("Dir", style="dim", justify="center")
    table.add_column("Message")

    for chat, msg in results:
        name = chat.display_name or chat.chat_identifier
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
        direction = "→" if msg.is_from_me else "←"
        text = (msg.text or "")[:120]
        table.add_row(date_str, name, direction, text)

    console.print(table)
    console.print(f"[dim]{len(results)} result(s)[/dim]")


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

@cli.group(invoke_without_command=True)
@click.pass_context
def contacts(ctx: click.Context) -> None:
    """List all contacts."""
    if ctx.invoked_subcommand is not None:
        return

    backup = _get_backup(ctx)
    with console.status("Loading contacts…"):
        from .parsers import contacts as contact_parser
        all_contacts = contact_parser.load(backup)

    if not all_contacts:
        console.print("[yellow]No contacts found.[/yellow]")
        return

    _print_contacts(all_contacts)


def _print_contacts(all_contacts) -> None:
    table = Table(title=f"Contacts ({len(all_contacts)})", box=box.SIMPLE_HEAD)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Organization")
    table.add_column("Phones")
    table.add_column("Emails")

    for c in all_contacts:
        phones = ", ".join(v.value for v in c.phones[:2])
        emails = ", ".join(v.value for v in c.emails[:2])
        table.add_row(
            str(c.id),
            c.display_name,
            c.organization or "—",
            phones or "—",
            emails or "—",
        )

    console.print(table)


@contacts.command(name="search")
@click.argument("query")
@click.pass_context
def contacts_search(ctx: click.Context, query: str) -> None:
    """Search contacts by name or phone number."""
    backup = _get_backup(ctx)
    with console.status("Loading contacts…"):
        from .parsers import contacts as contact_parser
        all_contacts = contact_parser.load(backup)

    q = query.lower()
    matched = [
        c for c in all_contacts
        if q in c.display_name.lower()
        or any(q in v.value.lower() for v in c.phones)
        or any(q in v.value.lower() for v in c.emails)
        or (c.organization and q in c.organization.lower())
    ]

    if not matched:
        console.print(f"[yellow]No contacts matching '[bold]{query}[/bold]'.[/yellow]")
        return

    _print_contacts(matched)


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

@cli.group(invoke_without_command=True)
@click.pass_context
def notes(ctx: click.Context) -> None:
    """List all notes."""
    if ctx.invoked_subcommand is not None:
        return

    backup = _get_backup(ctx)
    with console.status("Loading notes…"):
        from .parsers import notes as notes_parser
        all_notes = notes_parser.load(backup)

    if not all_notes:
        console.print("[yellow]No notes found.[/yellow]")
        return

    table = Table(title=f"Notes ({len(all_notes)})", box=box.SIMPLE_HEAD)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Title", style="cyan")
    table.add_column("Folder")
    table.add_column("Modified", style="dim")
    table.add_column("Rich", justify="center", style="dim")

    for n in all_notes:
        mod = n.modified.strftime("%Y-%m-%d") if n.modified else "?"
        rich = "✓" if n.has_rich_content else ""
        table.add_row(str(n.id), n.title or "Untitled", n.folder, mod, rich)

    console.print(table)


@notes.command(name="show")
@click.argument("note_id", type=int)
@click.pass_context
def notes_show(ctx: click.Context, note_id: int) -> None:
    """Display the body of a note by ID."""
    backup = _get_backup(ctx)
    with console.status("Loading notes…"):
        from .parsers import notes as notes_parser
        all_notes = notes_parser.load(backup)

    for n in all_notes:
        if n.id == note_id:
            console.rule(f"[bold cyan]{n.title or 'Untitled'}[/bold cyan]")
            if n.has_rich_content:
                console.print("[dim](Note contains rich content — showing best-effort plain text)[/dim]\n")
            console.print(n.body_text or "[dim](empty)[/dim]")
            return

    console.print(f"[yellow]No note with ID {note_id}.[/yellow]")


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def calls(ctx: click.Context) -> None:
    """List call history."""
    backup = _get_backup(ctx)
    with console.status("Loading call history…"):
        from .parsers import calls as calls_parser
        records = calls_parser.load(backup)

    if not records:
        console.print("[yellow]No call records found.[/yellow]")
        return

    table = Table(title=f"Call History ({len(records)})", box=box.SIMPLE_HEAD)
    table.add_column("Date", style="dim")
    table.add_column("Number", style="cyan")
    table.add_column("Direction", justify="center")
    table.add_column("Answered", justify="center")
    table.add_column("Duration")
    table.add_column("Service", style="dim")

    for r in records:
        date_str = r.date.strftime("%Y-%m-%d %H:%M") if r.date else "?"
        direction = "Outgoing" if r.originated else "Incoming"
        answered = "✓" if r.answered else "✗"
        mins, secs = divmod(int(r.duration_seconds), 60)
        dur = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        table.add_row(date_str, r.address, direction, answered, dur, r.service)

    console.print(table)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--format", "fmt",
    type=click.Choice(["json", "csv"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--output", "-o",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, path_type=Path),
    default=Path("./ios-backup-export"),
    show_default=True,
    help="Output directory.",
)
@click.option(
    "--include",
    default="messages,contacts,notes,calls",
    show_default=True,
    help="Comma-separated list of data types to export.",
)
@click.pass_context
def export(ctx: click.Context, fmt: str, output: Path, include: str) -> None:
    """Export backup data to JSON or CSV files."""
    backup = _get_backup(ctx)
    types = {t.strip().lower() for t in include.split(",")}
    output.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        from rich.panel import Panel
        console.print(Panel(
            "[yellow]CSV format note:[/yellow] Messages and Contacts use multiple related files "
            "(chats.csv + messages.csv + message_attachments.csv; contacts.csv + contact_values.csv). "
            "Use the [bold]json[/bold] format for a single lossless file per data type.",
            title="CSV Export",
            border_style="yellow",
        ))

    if fmt == "json":
        from .exporters.json_export import export_all
    else:
        from .exporters.csv_export import export_all  # type: ignore[no-redef]

    export_all(backup, output, types, console)
