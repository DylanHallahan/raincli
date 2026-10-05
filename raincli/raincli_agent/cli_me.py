"""``raincli me``, ``trust``, ``routing`` and ``app`` (protocol §16.3, §16.4, §16.5, §16.11,
§16.12 C5, §16.14 S3): the person session's headless commands and the machine's routing and
trust settings. Bodies are read from a file or stdin only, never from argv."""
import argparse
import os
import time
import uuid

from . import attachments as att
from . import person
from . import cli as _cli
from .cli import (EXIT_OK, EXIT_TIMEOUT, FILE_LABEL, _send_surfacing_id, client, format_message, message_id, out,
                  out_json, print_messages, read_body, report_send)
from .config import default_config_path
from .errors import ApiError, RainError, UsageError
from .text import escape_line

ROUTING = ("all", "inbox-only")


def _agent_config(args):
    return args.agent_config or default_config_path()


def _runtime_config(args):
    from .login import runtime_config_path
    return getattr(args, "runtime_config", None) or runtime_config_path(_agent_config(args))


def body_file_args(sp, attach=True):
    sp.add_argument("--body-file", required=True, metavar="PATH|-",
                    help="read the message text from a file or - for stdin (never from argv)")
    sp.add_argument("--id", help="client message id (uuid4) for an idempotent retry")
    if attach:
        sp.add_argument("--attach", action="append", default=[], metavar="PATH",
                        help="attach a Markdown (.md) file; repeatable")
    sp.add_argument("--json", action="store_true", help="print JSON")


# -- errors the user acts on ------------------------------------------------------------------------

def team_choice(exc):
    """``team_required`` (§16.12 C9) as a message naming the teams."""
    teams = [t for t in (getattr(exc, "payload", {}) or {}).get("teams") or [] if isinstance(t, dict)]
    names = ", ".join(escape_line(str(t.get("slug", "")))[:64] for t in teams)
    return RainError(f"you are a member of several teams; choose one with --team ({names})")


def not_deliverable(exc, endpoint):
    """§16.12 C10: an agent endpoint that can't receive is refused, never silently rerouted;
    the machine endpoint is offered instead."""
    reason = (getattr(exc, "payload", {}) or {}).get("reason")
    reason = escape_line(str(reason)).replace("_", " ") if reason else "not deliverable"
    if isinstance(endpoint, dict) and endpoint.get("agent"):
        handle = escape_line(str(endpoint.get("machine")))
        return RainError(f"{escape_line(person.endpoint_label(endpoint))} can't receive messages ({reason}); "
                         f"to leave it with the machine instead, send to {handle}")
    return RainError(f"{escape_line(person.endpoint_label(endpoint))} can't receive messages ({reason})")


def _sending(endpoint, call):
    try:
        return call()
    except ApiError as exc:
        if exc.code == "team_required":
            raise team_choice(exc) from None
        if exc.code == "not_deliverable":
            raise not_deliverable(exc, endpoint) from None
        raise


# -- raincli send <endpoint> (machine credential, §16.11) ----------------------------------------------

def cmd_send_endpoint(args):
    """``raincli send <endpoint>``: any endpoint kind; the body from a file or stdin only."""
    if args.body is not None:
        raise UsageError("raincli send <endpoint> reads the body from --body-file PATH or - (stdin) only")
    endpoint = person.parse_endpoint(args.endpoint)
    body = read_body(args)
    mid = message_id(args.id) if args.id else str(uuid.uuid4())
    files = att.load_for_send(args.attach)
    api = client(args)

    def action(state):
        state["sent"] = True
        return _sending(endpoint, lambda: api.send(endpoint, body, message_id=mid, attachments=files,
                                                   from_agent=args.from_agent))

    message, created = _send_surfacing_id(args, mid, action)
    report_send(message, created, args.json)
    return EXIT_OK


def reply_endpoint(parent, me):
    """The other participant of ``parent`` as an API endpoint, seen from ``me`` (an
    endpoint label: a handle or ``@email``). §16.4: a reply to a named agent goes to it."""
    sender = parent.get("from_endpoint") or parent.get("from")
    recipient = parent.get("to_endpoint") or parent.get("to")
    mine = parent.get("from") == me
    other = recipient if mine else sender
    if isinstance(other, dict) and "person" in other:
        return {"person": other["person"]}
    if isinstance(other, dict):
        return {k: other[k] for k in ("machine", "agent") if other.get(k)} if other.get("agent") \
            else other.get("machine")
    return other


# -- raincli me ... (person session, §16.4) ------------------------------------------------------------

def _person(args):
    return person.PersonClient.for_config(_agent_config(args))


def cmd_me_inbox(args):
    api = _person(args)
    if not args.watch:
        after, messages = 0, []
        while True:
            page, cursor = api.inbox(after=after, include_acked=args.all)
            messages.extend(page)
            if len(page) < 100 or int(cursor) <= after:
                break
            after = int(cursor)
        print_messages(messages, args.json)
        return EXIT_OK
    # --watch: print each new message once (never acks; reading one with `me read` does).
    deadline = time.monotonic() + args.timeout if args.timeout else None
    after, seen, received = 0, set(), 0
    try:
        while True:
            wait = 25
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return EXIT_OK if received else EXIT_TIMEOUT
                wait = max(0, min(25, int(remaining + 0.999)))
            page, cursor = api.inbox(after=after, wait=wait, include_acked=args.all)
            fresh = [m for m in page if m["id"] not in seen]
            seen.update(m["id"] for m in fresh)
            if fresh:
                print_messages(fresh, args.json)
                received += len(fresh)
                if args.once:
                    return EXIT_OK
            after = max(after, int(cursor))
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_me_read(args):
    """Show one message; reading a message to you acks it (§16.4)."""
    api = _person(args)
    message = api.message(args.message_id)
    me = api.me()["user"]["email"]
    if (message.get("to_endpoint") or {}).get("person", "").lower() == me.lower() and not message.get("acked_at"):
        message = api.ack(message["id"])[0]
    if args.json:
        out_json({"message": message})
    else:
        out(format_message(message))
    return EXIT_OK


def cmd_me_send(args):
    endpoint = person.parse_endpoint(args.endpoint)
    body = read_body(args)
    mid = message_id(args.id) if args.id else str(uuid.uuid4())
    files = att.load_for_send(args.attach)
    api = _person(args)

    def action(state):
        state["sent"] = True
        return _sending(endpoint, lambda: api.send(endpoint, body, message_id=mid, attachments=files,
                                                   team=args.team))

    message, created = _send_surfacing_id(args, mid, action)
    report_send(message, created, args.json)
    return EXIT_OK


def cmd_me_reply(args):
    body = read_body(args)
    mid = message_id(args.id) if args.id else str(uuid.uuid4())
    files = att.load_for_send(args.attach)
    api = _person(args)

    def action(state):
        parent = api.message(args.message_id)
        me = api.me()
        to = reply_endpoint(parent, "@" + me["user"]["email"])
        teams = [args.team] if args.team else [None]
        state["sent"] = True
        for index, team in enumerate(teams):
            try:
                return _send_reply(api, to, body, mid, parent, files, team)
            except ApiError as exc:
                if exc.code == "team_required" and team is None and not args.team:
                    # The message JSON has no team: try the person's teams in turn. A wrong team is refused
                    # (400/404) before anything is stored, and every attempt carries the same id.
                    teams.extend(t["slug"] for t in me.get("teams") or [] if isinstance(t, dict))
                    continue
                if team is not None and index < len(teams) - 1 and exc.status in (400, 404):
                    continue
                raise
        raise RainError("none of your teams holds this conversation")

    message, created = _send_surfacing_id(args, mid, action)
    report_send(message, created, args.json)
    return EXIT_OK


def _send_reply(api, to, body, mid, parent, files, team):
    try:
        return api.send(to, body, message_id=mid, in_reply_to=parent["id"], attachments=files, team=team)
    except ApiError as exc:
        if exc.code == "not_deliverable":
            raise not_deliverable(exc, to) from None
        raise


def cmd_me_fetch(args):
    """Download one attachment (``--attachment N``, 1-based), verified and never overwriting."""
    api = _person(args)
    message = api.message(args.message_id)
    items = message.get("attachments") or []
    if not 1 <= args.attachment <= len(items):
        raise RainError(f"message has {len(items)} attachment(s); --attachment is 1 to {len(items)}")
    meta = items[args.attachment - 1]
    att.check_metadata(meta)
    if args.to:
        directory = att.prepare_dir(args.to)
    else:
        directory = att.prepare_dir(os.getcwd(), "raincli-attachments", message["id"])
    target = os.path.join(directory, meta["filename"])
    if att.existing_matches(target, meta["sha256"]):
        out(f"already present: {escape_line(target)}")
        return EXIT_OK
    data, header_sha = api.download_attachment(message["id"], args.attachment)
    att.verify_download(meta, data, header_sha)
    result = att.write_exclusive(directory, meta["filename"], data, meta["sha256"])
    label = "saved" if result == "saved" else "already present"
    out(f"{label}: {escape_line(target)} ({meta['size']} bytes, sha256 {meta['sha256']}) [{FILE_LABEL}]")
    return EXIT_OK


def cmd_me_approve(args):
    from . import trust
    record = trust.approve(_runtime_config(args), args.message_id, always=args.always)
    out(f"approved: {record['id']} is now {record['state']}")
    if args.always:
        out(f"trusted from now on: {escape_line(person.endpoint_label(record['sender']))}")
    return EXIT_OK


def cmd_me_sign_out(args):
    result = person.sign_out(_agent_config(args), local_only=args.local_only)
    if not result["removed"]:
        out("this machine had no person session")
    elif result["server"] == "skipped":
        out("deleted the person session here (--local-only); revoke it on the website's Signed-in apps list")
    elif result["server"] == "already_revoked":
        out("the server had already ended this person session; deleted it here")
    else:
        out("signed out: this person session is revoked and deleted here")
    return EXIT_OK


# -- raincli login --person ---------------------------------------------------------------------------

def login_person(args):
    """``raincli login --person`` (§16.3): a person session for this signed-in machine,
    proven by its credential. It rotates nothing."""
    import getpass
    from .config import Secret
    _cli._need_tty("raincli login --person")
    email = args.email or _cli._ask("Email: ")
    if not email:
        raise UsageError("an email address is required")
    password = Secret(getpass.getpass("Password: "))
    result = person.add_session(_agent_config(args), email, password)
    protection = "DPAPI-protected, private Windows ACL" if os.name == "nt" else "mode 0600"
    out(f"added a person session for {escape_line(email)} to this machine")
    out(f"wrote {result['config']} ({protection})")
    return EXIT_OK


# -- raincli trust / routing / app ----------------------------------------------------------------------

def cmd_trust(args):
    from . import trust
    path = _runtime_config(args)
    if args.mode:
        result = trust.set_mode(path, args.mode)
    elif args.action == "add":
        result = trust.add(path, args.sender)
    elif args.action == "remove":
        result = trust.remove(path, args.sender)
    else:
        result = trust.describe(path)
    if args.json:
        out_json(result)
        return EXIT_OK
    mode = result["trust_mode"]
    out(f"trust mode: {mode} ("
        + ("any member of your team reaches your named agents" if mode == "team"
           else "only trusted senders; others wait for raincli me approve") + ")")
    out("trusted senders: " + (escape_line(", ".join(result["trusted_senders"])) or "none"))
    if result["blocked_senders"]:
        out("blocked senders: " + escape_line(", ".join(result["blocked_senders"])))
    if result.get("owner_email"):
        out(f"always trusted: you ({escape_line(result['owner_email'])})")
    return EXIT_OK


def cmd_routing(args):
    api = client(args)
    if args.all or args.inbox_only:
        data = api.request("PUT", "/routing", body={"routing": "all" if args.all else "inbox-only"})[1]
    else:
        data = api.request("GET", "/routing")[1]
    if args.json:
        out_json(data)
    else:
        routing = data.get("routing")
        out(f"routing: {escape_line(str(routing))} ("
            + ("teammates may message this machine's named agents" if routing == "all"
               else "only this machine's inbox receives messages") + ")")
    return EXIT_OK


def cmd_app_rotate(args):
    """For the app installer (§16.14 S3): a fresh ``app_install_token``. Prints no token."""
    person.rotate_app_install_token(_agent_config(args))
    out("rotated this install's app token")
    return EXIT_OK


# -- parsers -----------------------------------------------------------------------------------------

def register(sub, parser_class):
    me = sub.add_parser("me", help="your messages as a person (person session; raincli login --person)",
                        description="Messages to and from you as a person (§16.4), with this machine's person "
                                    "session. Bodies are read from a file or stdin, never from argv.")
    me_sub = me.add_subparsers(dest="me_command", required=True, parser_class=parser_class)
    inbox = me_sub.add_parser("inbox", help="messages to you (unread by default)")
    inbox.add_argument("--watch", action="store_true", help="wait for new messages and print them (never marks read)")
    inbox.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    inbox.add_argument("--timeout", type=float, metavar="S", help="with --watch: exit 4 if nothing arrives in S s")
    inbox.add_argument("--all", action="store_true", help="include messages already read")
    inbox.add_argument("--json", action="store_true", help="print JSON")
    inbox.set_defaults(func=cmd_me_inbox)
    read = me_sub.add_parser("read", help="show one message (marks a message to you read)")
    read.add_argument("message_id", metavar="MSG_ID")
    read.add_argument("--json", action="store_true", help="print JSON")
    read.set_defaults(func=cmd_me_read)
    send = me_sub.add_parser("send", help="send as yourself to a machine, handle/agent or @email")
    send.add_argument("endpoint", metavar="ENDPOINT", help="handle, handle/agent or @email")
    send.add_argument("--team", metavar="SLUG", help="the team, when you belong to several")
    body_file_args(send)
    send.set_defaults(func=cmd_me_send, body=None)
    reply = me_sub.add_parser("reply", help="reply to a message as yourself")
    reply.add_argument("message_id", metavar="MSG_ID")
    reply.add_argument("--team", metavar="SLUG", help=argparse.SUPPRESS)
    body_file_args(reply)
    reply.set_defaults(func=cmd_me_reply, body=None)
    fetch = me_sub.add_parser("fetch", help="download one attachment of a message (never overwrites)")
    fetch.add_argument("message_id", metavar="MSG_ID")
    fetch.add_argument("--attachment", type=int, required=True, metavar="N", help="which attachment, from 1")
    fetch.add_argument("--to", metavar="DIR", help="target directory (default ./raincli-attachments/<msg-id>/)")
    fetch.set_defaults(func=cmd_me_fetch)
    approve = me_sub.add_parser("approve", help="deliver a message held for approval to one of your agents")
    approve.add_argument("message_id", metavar="MSG_ID")
    approve.add_argument("--always", action="store_true", help="also trust its sender from now on")
    approve.add_argument("--runtime-config", metavar="PATH", help="machine runtime config (default: beside --config)")
    approve.set_defaults(func=cmd_me_approve)
    sign_out = me_sub.add_parser("sign-out", help="end this machine's person session (the machine stays signed in)")
    sign_out.add_argument("--local-only", action="store_true", help="delete it here without revoking it")
    sign_out.set_defaults(func=cmd_me_sign_out)

    trust = sub.add_parser("trust", help="who may reach this machine's named agents (team or list)",
                           description="Trust for messages to this machine's named agents (§16.12 C5). In team "
                                       "mode (the default) any member of your team reaches them; in list mode only "
                                       "trusted senders do and others wait for raincli me approve. You are always "
                                       "trusted.")
    trust.add_argument("action", nargs="?", choices=("add", "remove"))
    trust.add_argument("sender", nargs="?", metavar="MACHINE|@EMAIL")
    trust.add_argument("--mode", choices=("list", "team"))
    trust.add_argument("--runtime-config", metavar="PATH", help="machine runtime config (default: beside --config)")
    trust.add_argument("--json", action="store_true", help="print JSON")
    trust.set_defaults(func=_checked_trust)

    routing = sub.add_parser("routing", help="whether teammates may message this machine's named agents")
    which = routing.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="named agents and the inbox (the default)")
    which.add_argument("--inbox-only", action="store_true", help="only this machine's inbox")
    routing.add_argument("--json", action="store_true", help="print JSON")
    routing.set_defaults(func=cmd_routing)

    app = sub.add_parser("app", help="the RainCLI app's local settings")
    app_sub = app.add_subparsers(dest="app_command", required=True, parser_class=parser_class)
    rotate = app_sub.add_parser("rotate-install-token",
                                help="give this install a new app token (the installer runs it; prints no token)")
    rotate.set_defaults(func=cmd_app_rotate)


def _checked_trust(args):
    if args.mode and args.action:
        raise UsageError("use either --mode or add/remove")
    if args.action and not args.sender:
        raise UsageError(f"raincli trust {args.action} needs a machine handle or @email")
    if args.sender and not args.action:
        raise UsageError("raincli trust takes add or remove before the sender")
    return cmd_trust(args)

