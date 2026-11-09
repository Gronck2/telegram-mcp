"""Sampled whole-call refusals recorded for structured errors (task 0007, round 1).

Round 1 added ``record_tool_error(<kind>)`` next to ~120 bare ``return "Error: ..."``
refusals. The risk is the opposite of a missed error: a SUCCESS result flagged as an
error (a record in a loop, a try/except or a multi-step tool that goes on to succeed).

Every pair below drives one real registered tool through the real low-level
CallToolRequest handler against a stub client:

* the refusal path: text identical with the switch OFF, plain result OFF, ``isError``
  and the documented ``kind`` ON, and the pair's ``bad_text`` proves the intended
  refusal (not some other error) was reached;
* a nearby successful path of the same tool: ``isError`` false and no ``_meta`` ON,
  byte-identical to OFF, and ``ok_text`` proves it really is the success text.
"""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import (  # noqa: F401  (registers the tools)
    chats,
    contacts,
    custom_emoji,
    folders,
    groups,
    media,
    messages,
    profile,
)
from mcp.types import CallToolRequest, CallToolRequestParams

META = "telegram-mcp/error"
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


# --- stub client -------------------------------------------------------------------


class Fake:
    """Telethon stand-in. ``routes`` maps a request class name to a value, an exception
    (raised) or a callable(request); ``attrs`` maps a client method name likewise."""

    def __init__(self, routes=None, entities=None, **attrs):
        self.routes = routes or {}
        self.entities = entities or {}
        self.attrs = attrs
        self.flood_sleep_threshold = 60

    def is_connected(self):
        return True

    async def get_dialogs(self, *args, **kwargs):
        return []

    async def _lookup(self, key):
        found = self.entities.get(key, ValueError(f"Cannot find any entity for {key!r}"))
        if isinstance(found, BaseException):
            raise found
        return found

    async def get_entity(self, key):
        return await self._lookup(key)

    async def get_input_entity(self, key):
        return await self._lookup(key)

    async def __call__(self, request, **kwargs):
        if isinstance(request, functions.help.GetNearestDcRequest):
            return None
        name = type(request).__name__
        if name not in self.routes:
            raise RuntimeError(f"stub: no route for {name}")
        return await _produce(self.routes[name], request)

    def __getattr__(self, name):
        if name.startswith("__") or name not in self.attrs:
            raise AttributeError(name)
        value = self.attrs[name]

        async def method(*args, **kwargs):
            return await _produce(value, *args, **kwargs)

        return method


async def _produce(value, *args, **kwargs):
    if isinstance(value, BaseException):
        raise value
    if callable(value):
        out = value(*args, **kwargs)
        if asyncio.iscoroutine(out):
            out = await out
        if isinstance(out, BaseException):
            raise out
        return out
    return value


def tg(name, **kw):
    return getattr(errors, name)(request=None, **kw)


def user(uid=5, **kw):
    return types.User(id=uid, access_hash=1, first_name=kw.pop("first_name", "Ann"), **kw)


def supergroup(cid=100):
    return types.Channel(
        id=cid,
        title="Chan",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        access_hash=2,
        megagroup=True,
    )


def broadcast(cid=101):
    return types.Channel(
        id=cid,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        access_hash=2,
        broadcast=True,
    )


def basic_group(cid=42):
    return types.Chat(
        id=cid,
        title="Small",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=NOW,
        version=1,
    )


def world(**extra):
    """Entities every tool below may need, keyed the ways resolve_entity asks."""
    sg, bc, bg, ann = supergroup(), broadcast(), basic_group(), user(5)
    ents = {
        100: sg,
        -1000000000100: sg,
        101: bc,
        -1000000000101: bc,
        42: bg,
        -42: bg,
        5: ann,
        1: user(1, is_self=True),
        "somebot": user(77, bot=True),
        **extra,
    }
    return ents


def fake(routes=None, **attrs):
    return Fake(routes=routes, entities=world(), **attrs)


# --- harness -----------------------------------------------------------------------


_TMP: dict = {}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    _TMP["path"] = tmp_path
    for var in ("TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_READ_CHATS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    monkeypatch.delenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(runtime, "_last_conn_verified", {})
    monkeypatch.setattr(groups, "_REMOVE_USER_UNBAN_DELAY", 0)
    monkeypatch.setattr(contacts._resolve_phone_limiter, "reserve", lambda *a, **k: None)


async def call(name, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    handler = runtime.mcp._mcp_server.request_handlers[CallToolRequest]
    return (await handler(request)).root


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


def out_path_patch(module):
    """Stand in for the file-path security check: write under the test's tmp dir."""

    async def resolve(**kwargs):
        return _TMP["path"] / "out.bin", None

    return lambda mp: mp.setattr(module, "_resolve_writable_file_path", resolve)


def nothing(monkeypatch):
    pass


class Pair:
    def __init__(
        self,
        tool,
        kind,
        bad,
        bad_text,
        ok,
        ok_text,
        setup=nothing,
        ok_setup=None,
        extra=None,
        id=None,
    ):
        self.tool, self.kind = tool, kind
        self.bad, self.bad_text = bad, bad_text  # (args, make_client)
        self.ok, self.ok_text = ok, ok_text
        self.setup, self.ok_setup = setup, ok_setup or setup
        self.extra = extra or {}  # extra expected _meta fields of the refusal
        self.id = id or f"{tool}-{kind}-{bad_text[:24]}"

    def __repr__(self):
        return self.id


async def run(monkeypatch, tool, spec, setup, switch):
    args, make = spec
    setup(monkeypatch)
    monkeypatch.setattr(runtime, "clients", {"one": make()})
    if switch:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    else:
        monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    return await call(tool, **args)


async def check_pair(monkeypatch, pair):
    bad_off = await run(monkeypatch, pair.tool, pair.bad, pair.setup, False)
    bad_on = await run(monkeypatch, pair.tool, pair.bad, pair.setup, True)
    assert pair.bad_text in text(bad_off), text(bad_off)
    assert text(bad_on) == text(bad_off), "text never changes with the switch"
    assert bad_off.meta is None and bad_off.isError is False
    assert bad_on.isError is True
    assert bad_on.meta == {META: {"kind": pair.kind, **pair.extra}}

    ok_off = await run(monkeypatch, pair.tool, pair.ok, pair.ok_setup, False)
    ok_on = await run(monkeypatch, pair.tool, pair.ok, pair.ok_setup, True)
    assert pair.ok_text in text(ok_off), text(ok_off)
    assert (ok_on.isError, ok_on.meta) == (False, None), text(ok_on)
    assert ok_on.model_dump(by_alias=True) == ok_off.model_dump(by_alias=True)


# --- fixtures for the stubs --------------------------------------------------------


def filters(*items):
    return types.messages.DialogFilters(filters=list(items), tags_enabled=False)


def folder(fid=5, title="Work", **kw):
    kw.setdefault("contacts", True)
    for field in ("pinned_peers", "include_peers", "exclude_peers"):
        kw.setdefault(field, [])
    return types.DialogFilter(id=fid, title=types.TextWithEntities(text=title, entities=[]), **kw)


def shared_folder(fid=6):
    return types.DialogFilterChatlist(
        id=fid,
        title=types.TextWithEntities(text="Shared", entities=[]),
        pinned_peers=[],
        include_peers=[],
    )


def folder_routes(*items, **more):
    return {
        "GetDialogFiltersRequest": filters(*items),
        "UpdateDialogFilterRequest": True,
        "GetAppConfigRequest": RuntimeError("no config"),
        **more,
    }


def resolved(peer, *users):
    return types.contacts.ResolvedPeer(peer=peer, chats=[], users=list(users))


def poll_message(closed=False, multiple=False, answers=2):
    poll = types.Poll(
        id=1,
        question=types.TextWithEntities(text="Q", entities=[]),
        answers=[
            types.PollAnswer(
                text=types.TextWithEntities(text=f"a{i}", entities=[]), option=bytes([i])
            )
            for i in range(answers)
        ],
        hash=0,
        closed=closed,
        multiple_choice=multiple,
    )
    return SimpleNamespace(
        media=types.MessageMediaPoll(poll=poll, results=types.PollResults()), id=3
    )


def _msgs(value):
    return lambda entity, ids=None, **kw: value


def _file_message(name="a.txt", mime="text/plain"):
    return SimpleNamespace(
        id=1,
        media=types.MessageMediaUnsupported(),
        file=SimpleNamespace(name=name, mime_type=mime),
    )


def _plain_message():
    return SimpleNamespace(id=1, media=None, file=None, rich_message=None)


def _album_messages(entity, ids=None, **kw):
    if isinstance(ids, list):
        return [SimpleNamespace(id=i, grouped_id=7 if i in (10, 11) else None) for i in ids]
    return SimpleNamespace(id=ids, grouped_id=7)


async def _download_to(target, file=None, **kw):
    import pathlib

    pathlib.Path(file).write_bytes(b"data")
    return file


# --- the pairs ---------------------------------------------------------------------

READ_RECEIPT = [types.ReadParticipantDate(user_id=5, date=NOW)]
INVITED = SimpleNamespace(updates=None, missing_invitees=[])


def _paired(*pairs):
    return [pytest.param(p, id=p.id) for p in pairs]


def chats_pairs():
    link = SimpleNamespace(link="https://t.me/c/100/1", html=None)
    return [
        Pair(
            "subscribe_public_channel",
            "private",
            ({"channel": 100}, lambda: fake({"JoinChannelRequest": tg("ChannelPrivateError")})),
            "Cannot subscribe",
            ({"channel": 100}, lambda: fake({"JoinChannelRequest": None})),
            "Subscribed to",
        ),
        Pair(
            "get_message_read_by",
            "private",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": tg("ChatAdminRequiredError")}),
            ),
            "admin rights are required",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": READ_RECEIPT}),
            ),
            '"count": 1',
            id="get_message_read_by-admin",
        ),
        Pair(
            "get_message_read_by",
            "private",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": tg("UserNotParticipantError")}),
            ),
            "not a participant",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": READ_RECEIPT}),
            ),
            '"count": 1',
            id="get_message_read_by-not-participant",
        ),
        Pair(
            "get_message_read_by",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": tg("PeerIdInvalidError")}),
            ),
            "Invalid chat",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"GetMessageReadParticipantsRequest": READ_RECEIPT}),
            ),
            '"count": 1',
            id="get_message_read_by-peer-invalid",
        ),
        Pair(
            "get_message_link",
            "internal",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake({"ExportMessageLinkRequest": SimpleNamespace(link=None, html=None)}),
            ),
            "Could not export link",
            ({"chat_id": 100, "message_id": 1}, lambda: fake({"ExportMessageLinkRequest": link})),
            "Link: https://t.me",
        ),
        Pair(
            "resolve_username",
            "invalid_argument",
            ({"username": "@"}, lambda: fake()),
            "Invalid username",
            (
                {"username": "ann"},
                lambda: fake({"ResolveUsernameRequest": resolved(types.PeerUser(5), user(5))}),
            ),
            '"id": 5',
        ),
    ]


def contacts_pairs():
    imported = types.contacts.ImportedContacts(
        imported=[types.ImportedContact(user_id=5, client_id=0)],
        popular_invites=[],
        retry_contacts=[],
        users=[],
    )
    two = types.contacts.ImportedContacts(
        imported=[
            types.ImportedContact(user_id=5, client_id=0),
            types.ImportedContact(user_id=6, client_id=1),
        ],
        popular_invites=[],
        retry_contacts=[],
        users=[],
    )
    added = {"AddContactRequest": SimpleNamespace(updates=[1])}
    return [
        Pair(
            "add_contact",
            "not_found",
            (
                {"username": "nobody", "first_name": "N"},
                lambda: fake({"ResolveUsernameRequest": resolved(types.PeerUser(5))}),
            ),
            "not found",
            (
                {"username": "ann", "first_name": "N"},
                lambda: fake(
                    {"ResolveUsernameRequest": resolved(types.PeerUser(5), user(5)), **added}
                ),
            ),
            "added successfully",
            id="add_contact-username-not-found",
        ),
        Pair(
            "add_contact",
            "invalid_argument",
            (
                {"username": "chan", "first_name": "N"},
                lambda: fake(
                    {"ResolveUsernameRequest": resolved(types.PeerChannel(100), user(5))}
                ),
            ),
            "not a user",
            (
                {"username": "ann", "first_name": "N"},
                lambda: fake(
                    {"ResolveUsernameRequest": resolved(types.PeerUser(5), user(5)), **added}
                ),
            ),
            "added successfully",
            id="add_contact-username-not-a-user",
        ),
        Pair(
            "add_contact",
            "invalid_argument",
            ({}, lambda: fake()),
            "Either phone or username",
            (
                {"phone": "+15550001", "first_name": "N"},
                lambda: fake({"ImportContactsRequest": imported}),
            ),
            "added successfully",
            id="add_contact-nothing-given",
        ),
        Pair(
            "import_contacts",
            "invalid_argument",
            (
                {
                    "contacts": [
                        {"phone": "+15550001", "first_name": "A"},
                        {"phone": "+15550002"},
                    ]
                },
                lambda: fake(),
            ),
            "nothing was imported",
            (
                {
                    "contacts": [
                        {"phone": "+15550001", "first_name": "A"},
                        {"phone": "+15550002", "first_name": "B"},
                    ]
                },
                lambda: fake({"ImportContactsRequest": two}),
            ),
            "Imported 2 contacts",
        ),
        Pair(
            "resolve_phone",
            "invalid_argument",
            ({"phone": "8 999 000-11-22"}, lambda: fake()),
            "international number",
            (
                {"phone": "+44 20 7946 0958"},
                lambda: fake({"ResolvePhoneRequest": resolved(types.PeerUser(5), user(5))}),
            ),
            "ResolvedPeer",
            id="resolve_phone-local-number",
        ),
        Pair(
            "resolve_phone",
            "invalid_argument",
            (
                {"phone": "+44 20 7946 0958"},
                lambda: fake({"ResolvePhoneRequest": tg("PhoneNumberInvalidError")}),
            ),
            "rejected this phone number",
            (
                {"phone": "+44 20 7946 0958"},
                lambda: fake({"ResolvePhoneRequest": resolved(types.PeerUser(5), user(5))}),
            ),
            "ResolvedPeer",
            id="resolve_phone-telegram-rejects",
        ),
    ]


def folders_pairs():
    live = folder(5)
    two_chats = {"chat_ids": [100, 999]}

    def update_ok(patch):
        return (
            {"folder_id": 5, "patch": patch},
            lambda: fake(folder_routes(folder(5))),
        )

    return [
        Pair(
            "update_folder",
            "not_found",
            ({"folder_id": 9, "patch": {"title": "N"}}, lambda: fake(folder_routes(live))),
            "folder 9 not found",
            update_ok({"title": "New"}),
            '"changed": true',
            id="update_folder-missing-folder",
        ),
        Pair(
            "update_folder",
            "invalid_argument",
            (
                {"folder_id": 6, "patch": {"title": "N"}},
                lambda: fake(folder_routes(shared_folder(6))),
            ),
            "shared folders cannot be edited",
            update_ok({"title": "New"}),
            '"changed": true',
            id="update_folder-shared",
        ),
        Pair(
            "update_folder",
            "invalid_argument",
            (
                {"folder_id": 5, "patch": {"title": "N"}, "expected_revision": "stale"},
                lambda: fake(folder_routes(live)),
            ),
            "folder changed since the snapshot",
            update_ok({"title": "New"}),
            '"changed": true',
            id="update_folder-stale-revision",
        ),
        Pair(
            "update_folder",
            "invalid_argument",
            ({"folder_id": 5, "patch": {"contacts": False}}, lambda: fake(folder_routes(live))),
            "at least one included peer",
            update_ok({"contacts": False, "groups": True}),
            '"changed": true',
            id="update_folder-nothing-included",
        ),
        Pair(
            "create_folder",
            "not_found",
            ({"title": "F", **two_chats}, lambda: fake(folder_routes())),
            "Failed to resolve",
            ({"title": "F", "chat_ids": [100]}, lambda: fake(folder_routes())),
            '"success": true',
            id="create_folder-second-chat-unresolved",
        ),
        Pair(
            "create_folder",
            "internal",
            (
                {"title": "F"},
                lambda: fake(
                    folder_routes(
                        UpdateDialogFilterRequest=tg(
                            "BadRequestError", message="DIALOG_FILTERS_TOO_MUCH"
                        )
                    )
                ),
            ),
            "folder limit",
            ({"title": "F"}, lambda: fake(folder_routes())),
            '"success": true',
            id="create_folder-server-limit",
        ),
        Pair(
            "add_chat_to_folder",
            "not_found",
            ({"folder_id": 5, "chat_id": 999}, lambda: fake(folder_routes(live))),
            "Failed to resolve",
            ({"folder_id": 5, "chat_id": 100}, lambda: fake(folder_routes(live))),
            "added to folder 5",
        ),
        Pair(
            "remove_chat_from_folder",
            "not_found",
            ({"folder_id": 5, "chat_id": 999}, lambda: fake(folder_routes(live))),
            "Failed to resolve",
            (
                {"folder_id": 5, "chat_id": 100},
                lambda: fake(
                    folder_routes(
                        folder(5, include_peers=[types.InputPeerChannel(100, 2)]),
                    )
                ),
            ),
            "removed from folder 5",
        ),
        Pair(
            "delete_folder",
            "invalid_argument",
            ({"folder_id": 1}, lambda: fake(folder_routes(live))),
            "Cannot delete system folder",
            ({"folder_id": 5}, lambda: fake(folder_routes(live))),
            "5",
        ),
    ]


def groups_pairs():
    g100 = {"chat_id": 100, "user_id": 5}
    participant = types.channels.ChannelParticipant(
        participant=types.ChannelParticipant(user_id=5, date=NOW), chats=[], users=[]
    )
    left = types.channels.ChannelParticipant(
        participant=types.ChannelParticipantLeft(peer=types.PeerUser(5)), chats=[], users=[]
    )
    banned = types.channels.ChannelParticipant(
        participant=types.ChannelParticipantBanned(
            peer=types.PeerUser(5),
            kicked_by=1,
            date=NOW,
            banned_rights=types.ChatBannedRights(until_date=None),
            left=True,
        ),
        chats=[],
        users=[],
    )
    removed_ok = (
        {**g100},
        lambda: fake({"GetParticipantRequest": participant, "EditBannedRequest": None}),
    )
    edit_ok = ({**g100, "change_info": True}, lambda: fake({"EditAdminRequest": None}))
    return [
        Pair(
            "create_group",
            "not_found",
            ({"title": "G", "user_ids": [5, 999]}, lambda: fake()),
            "Could not find a requested user",
            (
                {"title": "G", "user_ids": [5]},
                lambda: fake({"CreateChatRequest": SimpleNamespace(chats=[basic_group()])}),
            ),
            "Group created with ID: -42",
        ),
        Pair(
            "invite_to_group",
            "not_found",
            ({"group_id": 100, "user_ids": [5, 999]}, lambda: fake()),
            "could not be found",
            (
                {"group_id": 100, "user_ids": [5]},
                lambda: fake({"InviteToChannelRequest": INVITED}),
            ),
            "Successfully invited",
            id="invite_to_group-unknown-user",
        ),
        Pair(
            "invite_to_group",
            "private",
            (
                {"group_id": 100, "user_ids": [5]},
                lambda: fake({"InviteToChannelRequest": tg("UserPrivacyRestrictedError")}),
            ),
            "privacy settings",
            (
                {"group_id": 100, "user_ids": [5]},
                lambda: fake({"InviteToChannelRequest": INVITED}),
            ),
            "Successfully invited",
            id="invite_to_group-whole-call-privacy",
        ),
        Pair(
            "remove_user",
            "invalid_argument",
            ({"chat_id": 100, "user_id": 1}, lambda: fake()),
            "cannot target the current account",
            removed_ok,
            "removed from chat",
            id="remove_user-self",
        ),
        Pair(
            "remove_user",
            "not_found",
            (g100, lambda: fake({"GetParticipantRequest": left})),
            "not a member",
            removed_ok,
            "removed from chat",
            id="remove_user-left",
        ),
        Pair(
            "remove_user",
            "invalid_argument",
            (g100, lambda: fake({"GetParticipantRequest": banned})),
            "already banned",
            removed_ok,
            "removed from chat",
            id="remove_user-already-banned",
        ),
        Pair(
            "remove_user",
            "private",
            (g100, lambda: fake({"GetParticipantRequest": tg("ChatAdminRequiredError")})),
            "admin rights required",
            removed_ok,
            "removed from chat",
            id="remove_user-admin-required",
        ),
        Pair(
            "remove_user",
            "invalid_argument",
            ({"chat_id": 5, "user_id": 5}, lambda: fake()),
            "not a user",
            ({"chat_id": 42, "user_id": 5}, lambda: fake({"DeleteChatUserRequest": None})),
            "removed from chat",
            id="remove_user-chat-is-a-user",
        ),
        Pair(
            "toggle_slow_mode",
            "invalid_argument",
            ({"chat_id": 42, "seconds": 10}, lambda: fake()),
            "only supported for supergroups",
            ({"chat_id": 100, "seconds": 10}, lambda: fake({"ToggleSlowModeRequest": None})),
            "Slow mode enabled",
            id="toggle_slow_mode-basic-group",
        ),
        Pair(
            "toggle_slow_mode",
            "private",
            (
                {"chat_id": 100, "seconds": 10},
                lambda: fake({"ToggleSlowModeRequest": tg("ChatAdminRequiredError")}),
            ),
            "admin rights required",
            ({"chat_id": 100, "seconds": 0}, lambda: fake({"ToggleSlowModeRequest": None})),
            "Slow mode disabled",
            id="toggle_slow_mode-admin-required",
        ),
        Pair(
            "edit_admin_rights",
            "private",
            (
                {**g100, "change_info": True},
                lambda: fake({"EditAdminRequest": tg("ChatAdminRequiredError")}),
            ),
            "need admin rights",
            edit_ok,
            "Admin rights updated",
            id="edit_admin_rights-admin-required",
        ),
        Pair(
            "edit_admin_rights",
            "invalid_argument",
            (
                {**g100, "change_info": True},
                lambda: fake({"EditAdminRequest": tg("RightForbiddenError")}),
            ),
            "not allowed",
            edit_ok,
            "Admin rights updated",
            id="edit_admin_rights-right-forbidden",
        ),
        Pair(
            "edit_admin_rights",
            "invalid_argument",
            ({**g100, "mode": "patch"}, lambda: fake()),
            "none were",
            edit_ok,
            "Admin rights updated",
            id="edit_admin_rights-empty-patch",
        ),
        Pair(
            "promote_admin",
            "private",
            (
                {"group_id": 100, "user_id": 5},
                lambda: fake({"EditAdminRequest": tg("UserNotMutualContactError")}),
            ),
            "not mutual contacts",
            ({"group_id": 100, "user_id": 5}, lambda: fake({"EditAdminRequest": None})),
            "Successfully promoted",
        ),
        Pair(
            "ban_user",
            "private",
            (g100, lambda: fake({"EditBannedRequest": tg("UserNotMutualContactError")})),
            "not mutual contacts",
            (g100, lambda: fake({"EditBannedRequest": None})),
            "banned from chat",
        ),
        Pair(
            "unban_user",
            "private",
            (g100, lambda: fake({"EditBannedRequest": tg("UserNotMutualContactError")})),
            "not mutual contacts",
            (g100, lambda: fake({"EditBannedRequest": None})),
            "unbanned from chat",
        ),
        Pair(
            "get_member_admin_status",
            "private",
            ({"chat_id": 42, "user_id": 5}, lambda: fake()),
            "supports only supergroups",
            (g100, lambda: fake({"GetParticipantRequest": participant})),
            '"role"',
            id="get_member_admin_status-basic-group",
        ),
        Pair(
            "get_member_admin_status",
            "private",
            (g100, lambda: fake({"GetParticipantRequest": tg("ChatAdminRequiredError")})),
            "need admin rights",
            (g100, lambda: fake({"GetParticipantRequest": tg("UserNotParticipantError")})),
            "not-participant",
            id="get_member_admin_status-admin-required",
        ),
        Pair(
            "upgrade_group_to_supergroup",
            "invalid_argument",
            ({"chat_id": 101}, lambda: fake()),
            "broadcast channel",
            (
                {"chat_id": 42},
                lambda: fake({"MigrateChatRequest": SimpleNamespace(chats=[supergroup()])}),
            ),
            "upgraded to supergroup",
            id="upgrade_group-broadcast",
        ),
        Pair(
            "upgrade_group_to_supergroup",
            "private",
            ({"chat_id": 42}, lambda: fake({"MigrateChatRequest": tg("ChatAdminRequiredError")})),
            "only the group creator",
            ({"chat_id": 100}, lambda: fake()),
            "already a supergroup",
            id="upgrade_group-not-creator",
        ),
        Pair(
            "edit_chat_about",
            "invalid_argument",
            (
                {"chat_id": 100, "about": "x"},
                lambda: fake({"EditChatAboutRequest": tg("ChatAboutTooLongError")}),
            ),
            "255 character",
            ({"chat_id": 100, "about": "x"}, lambda: fake({"EditChatAboutRequest": True})),
            "description updated",
            id="edit_chat_about-too-long",
        ),
        Pair(
            "edit_chat_about",
            "private",
            (
                {"chat_id": 100, "about": "x"},
                lambda: fake({"EditChatAboutRequest": tg("ChatAdminRequiredError")}),
            ),
            "admin rights required",
            (
                {"chat_id": 100, "about": "x"},
                lambda: fake({"EditChatAboutRequest": tg("ChatAboutNotModifiedError")}),
            ),
            "already set",
            id="edit_chat_about-admin-required",
        ),
        Pair(
            "edit_chat_title",
            "invalid_argument",
            ({"chat_id": 5, "title": "T"}, lambda: fake()),
            "Cannot edit title",
            ({"chat_id": 100, "title": "T"}, lambda: fake({"EditTitleRequest": None})),
            "title updated",
        ),
        Pair(
            "get_invite_link",
            "internal",
            (
                {"chat_id": 100},
                lambda: fake(
                    {
                        "ExportChatInviteRequest": RuntimeError("x"),
                        "GetFullChatRequest": RuntimeError("x"),
                    },
                    export_chat_invite_link=RuntimeError("x"),
                ),
            ),
            "Could not retrieve invite link",
            (
                {"chat_id": 100},
                lambda: fake(
                    {"ExportChatInviteRequest": RuntimeError("first method fails")},
                    export_chat_invite_link="https://t.me/+fallback",
                ),
            ),
            "https://t.me/+fallback",
        ),
        Pair(
            "import_chat_invite",
            "private",
            (
                {"hash": "abc"},
                lambda: fake({"ImportChatInviteRequest": RuntimeError("needs admin")}),
            ),
            "requires admin approval",
            (
                {"hash": "abc"},
                lambda: fake({"ImportChatInviteRequest": SimpleNamespace(chats=[supergroup()])}),
            ),
            "Successfully joined chat",
            id="import_chat_invite-admin-approval",
        ),
        Pair(
            "import_chat_invite",
            "internal",
            ({"hash": "abc"}, lambda: fake({"ImportChatInviteRequest": RuntimeError("too many")})),
            "maximum number of participants",
            (
                {"hash": "abc"},
                lambda: fake({"ImportChatInviteRequest": SimpleNamespace(chats=[supergroup()])}),
            ),
            "Successfully joined chat",
            id="import_chat_invite-full",
        ),
    ]


def media_pairs(tmp_path_holder=None):
    media_message = SimpleNamespace(id=1, media=types.MessageMediaUnsupported(), rich_message=None)
    no_media = _plain_message()

    return [
        Pair(
            "download_media",
            "not_found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(None))),
            "No media found",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(media_message), download_media=_download_to),
            ),
            "Media downloaded to",
            setup=out_path_patch(media),
            id="download_media-no-message",
        ),
        Pair(
            "download_media",
            "not_found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(no_media))),
            "No media found",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(media_message), download_media=_download_to),
            ),
            "Media downloaded to",
            setup=out_path_patch(media),
            id="download_media-message-without-media",
        ),
        Pair(
            "get_media_info",
            "not_found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(None))),
            "No media found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(media_message))),
            "MessageMediaUnsupported",
            id="get_media_info-no-message",
        ),
        Pair(
            "get_media_info",
            "not_found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(no_media))),
            "No media found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(media_message))),
            "MessageMediaUnsupported",
            id="get_media_info-message-without-media",
        ),
        Pair(
            "list_photos",
            "invalid_argument",
            ({"chat_id": 100, "source": "bogus"}, lambda: fake()),
            "Unknown photo source",
            ({"chat_id": 100, "source": "messages"}, lambda: fake()),
            '"count": 0',
            setup=lambda mp: mp.setattr(media, "list_photo_references", _refs([])),
        ),
        Pair(
            "get_photo_sheet",
            "invalid_argument",
            ({"chat_id": 100, "source": "bogus"}, lambda: fake()),
            "Unknown photo source",
            ({"chat_id": 100, "source": "messages"}, lambda: fake()),
            "photo(s) for",
            setup=lambda mp: _sheet_patch(mp, [None, b"thumb"]),
            id="get_photo_sheet-unknown-source",
        ),
        Pair(
            "get_photo_sheet",
            "internal",
            ({"chat_id": 100, "source": "messages"}, lambda: fake()),
            "could be downloaded",
            ({"chat_id": 100, "source": "messages"}, lambda: fake()),
            "photo(s) for",
            setup=lambda mp: _sheet_patch(mp, [None, None]),
            ok_setup=lambda mp: _sheet_patch(mp, [None, b"thumb"]),
            id="get_photo_sheet-all-thumbnails-fail",
        ),
        Pair(
            "inspect_document",
            "internal",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(_file_message()), download_media=b""),
            ),
            "Failed to read the contents",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(_file_message()), download_media=b"hello"),
            ),
            "hello",
        ),
        Pair(
            "preview_custom_emoji",
            "invalid_argument",
            ({"document_id": "abc"}, lambda: fake()),
            "Invalid document_id",
            (
                {"document_id": 7},
                lambda: fake(
                    {"GetCustomEmojiDocumentsRequest": [SimpleNamespace(mime_type="x/y")]},
                    download_media=b"\x1f\x8b....",
                ),
            ),
            "has format",
            id="preview_custom_emoji-bad-id",
        ),
        Pair(
            "preview_custom_emoji",
            "internal",
            (
                {"document_id": 7},
                lambda: fake(
                    {"GetCustomEmojiDocumentsRequest": [SimpleNamespace(mime_type="x/y")]},
                    download_media=None,
                ),
            ),
            "Failed to download media",
            (
                {"document_id": 7},
                lambda: fake(
                    {"GetCustomEmojiDocumentsRequest": [SimpleNamespace(mime_type="x/y")]},
                    download_media=b"\x1f\x8b....",
                ),
            ),
            "has format",
            id="preview_custom_emoji-download-empty",
        ),
        Pair(
            "get_custom_emoji_pack",
            "invalid_argument",
            ({"pack": ""}, lambda: fake()),
            "Invalid pack name",
            (
                {"pack": "AnimatedDogs"},
                lambda: fake({"GetStickerSetRequest": tg("StickersetInvalidError")}),
            ),
            "not found or is invalid",
        ),
    ]


def _refs(items):
    async def fake_refs(*args, **kwargs):
        return items

    return fake_refs


def _sheet_patch(mp, thumbs):
    refs = [SimpleNamespace(identifier=i) for i in range(len(thumbs))]
    queue = list(thumbs)

    async def thumb(*args, **kwargs):
        return queue.pop(0)

    mp.setattr(media, "list_photo_references", _refs(refs))
    mp.setattr(media, "download_photo_bytes", thumb)
    mp.setattr(media, "build_contact_sheet", lambda tiles, columns: b"jpeg")


def messages_pairs():
    sg = {"chat_id": 100}
    fwd = {"from_chat_id": 100, "to_chat_id": 5, "message_id": 10}
    fwd_ok = (fwd, lambda: fake(forward_messages=None, get_messages=_album_messages))
    poll_args = {"chat_id": 100, "message_id": 3, "option_indices": [0]}
    poll_ok = (
        poll_args,
        lambda: fake({"SendVoteRequest": None}, get_messages=_msgs(poll_message())),
    )
    create_ok = (
        {"chat_id": 100, "question": "Q?", "options": ["a", "b"]},
        lambda: fake({"SendMediaRequest": None}),
    )
    return [
        Pair(
            "list_messages",
            "invalid_argument",
            ({**sg, "from_date": "yesterday"}, lambda: fake()),
            "Invalid from_date",
            (sg, lambda: fake(get_messages=_msgs([]))),
            "No messages found",
            id="list_messages-from-date",
        ),
        Pair(
            "list_messages",
            "invalid_argument",
            ({**sg, "from_date": "2026-01-01", "to_date": "soon"}, lambda: fake()),
            "Invalid to_date",
            (sg, lambda: fake(get_messages=_msgs([]))),
            "No messages found",
            id="list_messages-to-date-after-valid-from",
        ),
        Pair(
            "forward_message",
            "invalid_argument",
            ({**fwd, "topic_id": 0}, lambda: fake()),
            "topic_id must be a positive integer",
            fwd_ok,
            "Album of 2 messages forwarded",
            id="forward_message-bad-topic",
        ),
        Pair(
            "forward_messages",
            "invalid_argument",
            ({"from_chat_id": 100, "to_chat_id": 5, "message_ids": []}, lambda: fake()),
            "at least one id",
            (
                {"from_chat_id": 100, "to_chat_id": 5, "message_ids": [1, 2]},
                lambda: fake(forward_messages=None),
            ),
            "2 messages forwarded",
        ),
        Pair(
            "delete_chat_history",
            "private",
            (sg, lambda: fake({"DeleteHistoryRequest": tg("ChatAdminRequiredError")})),
            "admin privileges are required",
            (sg, lambda: fake({"DeleteHistoryRequest": SimpleNamespace(pts_count=3, offset=0)})),
            "3 messages deleted",
        ),
        Pair(
            "delete_messages_bulk",
            "invalid_argument",
            (
                {**sg, "message_ids": [1, 2]},
                lambda: fake({"DeleteMessagesRequest": tg("MessageIdInvalidError")}),
            ),
            "IDs are invalid",
            (
                {**sg, "message_ids": [1, 2]},
                lambda: fake({"DeleteMessagesRequest": SimpleNamespace(pts_count=2)}),
            ),
            "Deleted 2 of 2",
            id="delete_messages_bulk-invalid-id",
        ),
        Pair(
            "delete_messages_bulk",
            "private",
            (
                {**sg, "message_ids": [1, 2]},
                lambda: fake({"DeleteMessagesRequest": tg("ChatAdminRequiredError")}),
            ),
            "admin privileges are required",
            (
                {**sg, "message_ids": [1, 2]},
                lambda: fake({"DeleteMessagesRequest": SimpleNamespace(pts_count=2)}),
            ),
            "Deleted 2 of 2",
            id="delete_messages_bulk-admin-required",
        ),
        Pair(
            "unpin_all_messages",
            "private",
            (sg, lambda: fake({"UnpinAllMessagesRequest": tg("ChatAdminRequiredError")})),
            "admin privileges are required",
            (sg, lambda: fake({"UnpinAllMessagesRequest": None})),
            "All messages unpinned",
        ),
        Pair(
            "send_typing",
            "invalid_argument",
            ({**sg, "seconds": 0.5, "topic_id": 0}, lambda: fake()),
            "topic_id must be a positive integer",
            ({**sg, "seconds": 0.5}, lambda: fake({"SetTypingRequest": True})),
            "",
            id="send_typing-bad-topic",
        ),
        Pair(
            "create_poll",
            "invalid_argument",
            ({**create_ok[0], "options": ["a", "a"]}, lambda: fake()),
            "must be unique",
            create_ok,
            "Poll created successfully",
            id="create_poll-duplicate-options",
        ),
        Pair(
            "create_poll",
            "invalid_argument",
            ({**create_ok[0], "close_date": "someday"}, lambda: fake()),
            "Invalid close_date",
            create_ok,
            "Poll created successfully",
            id="create_poll-close-date",
        ),
        Pair(
            "vote_poll",
            "not_found",
            (poll_args, lambda: fake(get_messages=_msgs(None))),
            "does not contain a poll",
            poll_ok,
            "Vote submitted",
            id="vote_poll-no-poll",
        ),
        Pair(
            "vote_poll",
            "invalid_argument",
            (poll_args, lambda: fake(get_messages=_msgs(poll_message(closed=True)))),
            "Poll is closed",
            poll_ok,
            "Vote submitted",
            id="vote_poll-closed",
        ),
        Pair(
            "vote_poll",
            "invalid_argument",
            (
                {**poll_args, "option_indices": [5]},
                lambda: fake(get_messages=_msgs(poll_message())),
            ),
            "out of range",
            poll_ok,
            "Vote submitted",
            id="vote_poll-out-of-range",
        ),
        Pair(
            "send_reaction",
            "invalid_argument",
            ({**sg, "message_id": 1, "emoji": "custom:abc"}, lambda: fake()),
            "Invalid custom reaction",
            (
                {**sg, "message_id": 1, "emoji": "custom:42"},
                lambda: fake({"SendReactionRequest": None}),
            ),
            "Reaction 'custom:42' sent",
        ),
    ]


def export_pairs():
    out = "o.json"
    ok_export = (
        {"chat_id": 100, "output_path": out, "resume": False},
        lambda: fake(get_messages=_msgs([])),
    )
    patch = out_path_patch(messages)
    return [
        Pair(
            "export_chat_history",
            "invalid_argument",
            (
                {"chat_id": 100, "output_path": out, "resume": False, "offset_date": "never"},
                lambda: fake(),
            ),
            "offset_date must be in ISO format",
            ok_export,
            '"status": "ok"',
            setup=patch,
            id="export_chat_history-offset-date",
        ),
        Pair(
            "export_chat_history",
            "invalid_argument",
            ({"chat_id": 100, "output_path": out, "resume": False, "limit": 0}, lambda: fake()),
            "limit must be a positive integer",
            ok_export,
            '"status": "ok"',
            setup=patch,
            id="export_chat_history-limit",
        ),
        Pair(
            "export_unread_messages",
            "not_found",
            (
                {"chat_ids": [100, 999], "output_path": out, "resume": False},
                lambda: fake(get_messages=_msgs([])),
            ),
            "Could not resolve entity",
            (
                {"chat_ids": [100], "output_path": out, "resume": False},
                lambda: fake(get_messages=_msgs([])),
            ),
            '"status": "ok"',
            setup=patch,
            id="export_unread_messages-second-chat-unresolvable",
        ),
    ]


def profile_pairs():
    rules = types.account.PrivacyRules(rules=[types.PrivacyValueAllowAll()], chats=[], users=[])
    return [
        Pair(
            "get_privacy_settings",
            "internal",
            ({}, lambda: fake({"GetPrivacyRequest": TypeError("TLObject was expected")})),
            "type mismatch",
            ({}, lambda: fake({"GetPrivacyRequest": rules})),
            "rules",
        ),
        Pair(
            "set_privacy_settings",
            "invalid_argument",
            ({"key": "bogus", "allow_users": [5]}, lambda: fake()),
            "Unsupported privacy key",
            (
                {"key": "status", "allow_users": [5]},
                lambda: fake({"GetPrivacyRequest": rules, "SetPrivacyRequest": rules}),
            ),
            "updated successfully",
            id="set_privacy_settings-bad-key",
        ),
        Pair(
            "set_privacy_settings",
            "not_found",
            ({"key": "status", "allow_users": [5, 999]}, lambda: fake()),
            "Could not find a requested user",
            (
                {"key": "status", "allow_users": [5]},
                lambda: fake({"GetPrivacyRequest": rules, "SetPrivacyRequest": rules}),
            ),
            "updated successfully",
            id="set_privacy_settings-unknown-user",
        ),
        Pair(
            "set_bot_commands",
            "private",
            (
                {"bot_username": "somebot", "commands": [{"command": "a", "description": "d"}]},
                lambda: fake(get_me=user(1, bot=False)),
            ),
            "only be used by bot accounts",
            (
                {"bot_username": "somebot", "commands": [{"command": "a", "description": "d"}]},
                lambda: fake({"SetBotCommandsRequest": True}, get_me=user(1, bot=True)),
            ),
            "Bot commands set",
        ),
    ]


ALL = (
    chats_pairs()
    + contacts_pairs()
    + folders_pairs()
    + groups_pairs()
    + media_pairs()
    + messages_pairs()
    + profile_pairs()
    + export_pairs()
)


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", _paired(*ALL))
async def test_refusal_is_error_and_nearby_success_is_not(monkeypatch, pair):
    await check_pair(monkeypatch, pair)


# --- successes that sit next to a refusal (per-item errors, no-ops, fallbacks) ------


def _allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000100")
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


def _already_member():
    chat = types.ChatInviteAlready(chat=supergroup())
    return fake({"CheckChatInviteRequest": chat})


SUCCESS_ONLY = [
    (
        "invite_to_group",
        {"group_id": -42, "user_ids": [5]},
        lambda: fake({"AddChatUserRequest": tg("UserPrivacyRestrictedError")}),
        nothing,
        "(failed:",
    ),
    (
        "update_folder",
        {"folder_id": 5, "patch": {"title": "Work"}},
        lambda: fake(folder_routes(folder(5))),
        nothing,
        '"changed": false',
    ),
    (
        "subscribe_public_channel",
        {"channel": 100},
        lambda: fake({"JoinChannelRequest": tg("UserAlreadyParticipantError")}),
        nothing,
        "Already subscribed",
    ),
    (
        "add_chat_to_folder",
        {"folder_id": 5, "chat_id": 100},
        lambda: fake(folder_routes(folder(5, include_peers=[types.InputPeerChannel(100, 2)]))),
        nothing,
        "already in folder",
    ),
    (
        "get_message_link",
        {"chat_id": 5, "message_id": 1},
        lambda: fake(),
        nothing,
        "Cannot export message link",
    ),
    (
        "join_chat_by_link",
        {"link": "https://t.me/+abc"},
        _already_member,
        nothing,
        "already a member",
    ),
    (
        "import_chat_invite",
        {"hash": "abc"},
        _already_member,
        nothing,
        "already a member",
    ),
    (
        "resolve_phone",
        {"phone": "+44 20 7946 0958"},
        lambda: fake({"ResolvePhoneRequest": tg("PhoneNotOccupiedError")}),
        nothing,
        "No Telegram user",
    ),
    (
        "export_chat_history",
        {"chat_id": [100, 101], "output_path": "o.json", "resume": False},
        lambda: fake(get_messages=_msgs([])),
        lambda mp: (_allowlist(mp), out_path_patch(messages)(mp)),
        '"status": "ok"',
    ),
    (
        "export_unread_messages",
        {"chat_ids": [100, 101], "output_path": "o.json", "resume": False},
        lambda: fake(get_messages=_msgs([])),
        lambda mp: (_allowlist(mp), out_path_patch(messages)(mp)),
        '"status": "ok"',
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", SUCCESS_ONLY, ids=[c[0] + str(i) for i, c in enumerate(SUCCESS_ONLY)]
)
async def test_success_next_to_a_refusal_is_not_an_error(monkeypatch, case):
    tool, args, make, setup, expected = case
    off = await run(monkeypatch, tool, (args, make), setup, False)
    on = await run(monkeypatch, tool, (args, make), setup, True)
    assert expected in text(off), text(off)
    assert (on.isError, on.meta) == (False, None), text(on)
    assert on.model_dump(by_alias=True) == off.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_export_with_a_refused_chat_records_it_per_item_not_as_a_call_error(
    monkeypatch,
):
    setup = lambda mp: (_allowlist(mp), out_path_patch(messages)(mp))  # noqa: E731
    args = {"chat_id": [100, 101], "output_path": "o.json", "resume": False}
    on = await run(
        monkeypatch,
        "export_chat_history",
        (args, lambda: fake(get_messages=_msgs([]))),
        setup,
        True,
    )
    exported = json.loads((_TMP["path"] / "out.bin").read_text())
    assert "error" in exported["chats"]["101"] and "-1000000000100" in exported["chats"]
    assert (on.isError, on.meta) == (False, None)


@pytest.mark.asyncio
async def test_overlapping_typing_only_the_second_call_is_an_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"one": fake({"SetTypingRequest": True})})
    first, second = await asyncio.gather(
        call("send_typing", chat_id=100, seconds=0.6),
        call("send_typing", chat_id=100, seconds=0.5),
    )
    assert (first.isError, first.meta) == (False, None), text(first)
    assert second.isError is True
    assert second.meta == {META: {"kind": "invalid_argument"}}
    assert "already active" in text(second)


# --- needs_user from validate_id: single and list params, ambiguous, fan-out -------

ASK = "Nothing was sent"


def _two_lenas(monkeypatch):
    runtime.save_aliases(
        {
            "лена иванова": {"id": 303, "name": "Лена И."},
            "лена петрова": {"id": 404, "name": "Лена П."},
        }
    )


NEEDS_USER = [
    ("single-unknown", "get_message_link", {"chat_id": "неттакой", "message_id": 1}, nothing),
    (
        "list-unknown",
        "invite_to_group",
        {"group_id": 100, "user_ids": [5, "неттакой"]},
        nothing,
    ),
    (
        "single-ambiguous",
        "get_message_link",
        {"chat_id": "лена", "message_id": 1},
        _two_lenas,
    ),
    (
        "list-ambiguous",
        "invite_to_group",
        {"group_id": 100, "user_ids": ["лена"]},
        _two_lenas,
    ),
    (
        "single-subject-param",
        "block_user",
        {"user_id": "неттакой"},
        nothing,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,tool,args,setup", NEEDS_USER, ids=[c[0] for c in NEEDS_USER])
async def test_unknown_or_ambiguous_contact_asks_the_user(monkeypatch, name, tool, args, setup):
    spec = (args, lambda: fake())
    off = await run(monkeypatch, tool, spec, setup, False)
    on = await run(monkeypatch, tool, spec, setup, True)
    assert ASK in text(off), text(off)
    if "ambiguous" in name:
        assert "matches several saved contacts" in text(off)
    assert text(on) == text(off), "the ask-the-user payload is unchanged"
    assert (off.isError, off.meta) == (False, None)
    assert on.isError is True
    assert on.meta == {META: {"kind": "invalid_argument", "needs_user": True}}


@pytest.mark.asyncio
async def test_needs_user_flag_is_not_sticky_to_a_plain_validation_error(monkeypatch):
    """An id out of range is invalid_argument without the flag."""
    spec = ({"chat_id": 2**70, "message_id": 1}, lambda: fake())
    on = await run(monkeypatch, "get_message_link", spec, nothing, True)
    assert on.meta == {META: {"kind": "invalid_argument"}}


@pytest.mark.asyncio
async def test_needs_user_in_a_list_after_valid_items_flags_the_failing_item_only(monkeypatch):
    spec = ({"group_id": 100, "user_ids": [5, "неттакой", 5]}, lambda: fake())
    on = await run(monkeypatch, "invite_to_group", spec, nothing, True)
    assert on.meta == {META: {"kind": "invalid_argument", "needs_user": True}}


@pytest.mark.asyncio
async def test_needs_user_through_fan_out_over_two_accounts(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"a": fake(), "b": fake()})
    result = await call("get_message_link", chat_id="неттакой", message_id=1)
    meta = result.meta[META]
    assert result.isError is True
    assert (meta["kind"], meta["needs_user"]) == ("invalid_argument", True)
    assert [a["account"] for a in meta["accounts"]] == ["a", "b"]
    assert all(
        a == {"kind": "invalid_argument", "needs_user": True, "account": a["account"]}
        for a in meta["accounts"]
    )


@pytest.mark.asyncio
async def test_needs_user_not_set_when_one_account_asks_and_the_other_succeeds(monkeypatch):
    """Fan-out where only one account asks cannot happen (validation is per call,
    before any client is used); a partial failure with a real error stays plain."""
    link = SimpleNamespace(link="https://t.me/c/100/1", html=None)
    ok = fake({"ExportMessageLinkRequest": link})
    bad = fake({"ExportMessageLinkRequest": tg("ChannelPrivateError")})
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"a": ok, "b": bad})
    result = await call("get_message_link", chat_id=100, message_id=1)
    assert (result.isError, "needs_user" in json.dumps(result.meta)) == (False, False)


# --- flood waits, BadRequest and Forbidden subclasses through a real tool ----------

LINK_ARGS = {"chat_id": 100, "message_id": 1}


async def link_error(monkeypatch, exc):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"one": fake({"ExportMessageLinkRequest": exc})})
    return await call("get_message_link", **LINK_ARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            tg("FloodPremiumWaitError", capture=33),
            {"kind": "flood", "retry_after": 33, "rpc_error": "FloodPremiumWaitError"},
        ),
        (
            tg("FloodTestPhoneWaitError", capture=44),
            {"kind": "flood", "retry_after": 44, "rpc_error": "FloodTestPhoneWaitError"},
        ),
        (
            tg("SlowModeWaitError", capture=12),
            {"kind": "flood", "retry_after": 12, "rpc_error": "SlowModeWaitError"},
        ),
        (tg("FloodWaitError", capture=9), {"kind": "flood", "retry_after": 9}),
        (tg("PeerFloodError"), {"kind": "flood", "rpc_error": "PEER_FLOOD"}),
        (
            tg("FloodPremiumWaitError", capture=0),
            {"kind": "flood", "rpc_error": "FloodPremiumWaitError"},
        ),
        (tg("MessageTooLongError"), {"kind": "invalid_argument", "rpc_error": "MESSAGE_TOO_LONG"}),
        (
            errors.BadRequestError(None, "NEW_UNKNOWN_THING", 400),
            {"kind": "invalid_argument", "rpc_error": "NEW_UNKNOWN_THING"},
        ),
        (tg("ChatWriteForbiddenError"), {"kind": "private", "rpc_error": "CHAT_WRITE_FORBIDDEN"}),
        (
            tg("ChatSendMediaForbiddenError"),
            {"kind": "private", "rpc_error": "CHAT_SEND_MEDIA_FORBIDDEN"},
        ),
        (
            errors.ForbiddenError(None, "BRAND_NEW_FORBIDDEN", 403),
            {"kind": "private", "rpc_error": "BRAND_NEW_FORBIDDEN"},
        ),
        (tg("UserIsBlockedError"), {"kind": "invalid_argument", "rpc_error": "USER_IS_BLOCKED"}),
        (
            tg("FilerefUpgradeNeededError"),
            {"kind": "internal", "rpc_error": "FILEREF_UPGRADE_NEEDED"},
        ),
        (errors.RPCError(None, "X" * 70, 500), {"kind": "internal", "rpc_error": "RPCError"}),
    ],
    ids=lambda v: v.get("rpc_error", "") if isinstance(v, dict) else type(v).__name__,
)
async def test_rpc_classes_through_a_real_tool(monkeypatch, exc, expected):
    result = await link_error(monkeypatch, exc)
    assert result.isError is True
    meta = result.meta[META]
    if expected.get("retry_after") in (None, 0):
        assert "retry_after" not in meta
    assert meta == expected, text(result)
    off_text = text(result)
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS")
    assert text(await call("get_message_link", **LINK_ARGS)) == off_text


def _all_rpc_classes():
    import inspect

    from telethon.errors import rpcerrorlist

    return sorted(
        (
            c
            for c in vars(rpcerrorlist).values()
            if inspect.isclass(c)
            and issubclass(c, errors.RPCError)
            and c.__module__ == rpcerrorlist.__name__
        ),
        key=lambda c: c.__name__,
    )


def _instance(cls):
    import inspect

    if "capture" in inspect.signature(cls.__init__).parameters:
        return cls(request=None, capture=7)
    return cls(request=None)


@pytest.mark.parametrize("cls", _all_rpc_classes(), ids=lambda c: c.__name__)
def test_every_telethon_rpc_class_maps_to_a_documented_kind(cls):
    info = runtime.classify_error(_instance(cls))
    assert info["kind"] in {"not_found", "private", "flood", "invalid_argument", "internal"}
    assert set(info) <= {"kind", "retry_after", "rpc_error"}
    assert len(info.get("rpc_error", "")) <= 64
    if issubclass(cls, errors.FloodError):
        import inspect

        assert info["kind"] == "flood"
        takes_seconds = "capture" in inspect.signature(cls.__init__).parameters
        assert info.get("retry_after") == (7 if takes_seconds else None)
    explicit = runtime._NOT_FOUND_RPC | runtime._PRIVATE_RPC | runtime._FLOOD_RPC
    named = {info.get("rpc_error"), cls.__name__}
    if not (named & explicit) and not issubclass(cls, errors.FloodError):
        if issubclass(cls, errors.BadRequestError):
            assert info["kind"] == "invalid_argument", cls.__name__
        elif issubclass(cls, errors.ForbiddenError):
            assert info["kind"] == "private", cls.__name__
        else:
            assert info["kind"] == "internal", cls.__name__


# --- (4) a raised ToolError that nothing recorded ----------------------------------


@pytest.fixture
def raise_probe():
    from mcp.server.fastmcp.exceptions import ToolError

    @runtime.mcp.tool(name="zz_raise_probe")
    @runtime.with_account(readonly=True)
    @runtime.validate_id("chat_id", raise_errors=True)
    async def probe(chat_id: int, mode: str, account: str = None) -> str:
        if mode == "toolerror":
            raise ToolError("backend exploded")
        if mode == "runtime":
            raise RuntimeError("backend exploded")
        if mode == "recorded":
            raise ToolError(runtime.log_and_format_error("zz_raise_probe", RuntimeError("x")))
        return "fine"

    yield
    runtime.mcp._tool_manager._tools.pop("zz_raise_probe", None)


async def probe_call(monkeypatch, labels, mode):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {label: fake() for label in labels})
    return await call("zz_raise_probe", chat_id=5, mode=mode)


@pytest.mark.asyncio
async def test_get_participants_toolerrors_are_all_recorded_with_the_real_kind(monkeypatch):
    """Every ToolError the real raise_errors tool raises passes log_and_format_error."""

    async def boom(*a, **k):
        raise RuntimeError("iterator broke")
        yield  # pragma: no cover

    cases = [
        ({"chat_id": 100, "page": 0}, fake(), "invalid_argument"),
        ({"chat_id": 999}, fake(), "not_found"),
        ({"chat_id": "неттакой"}, fake(), "invalid_argument"),
        ({"chat_id": 100}, Fake(entities=world(), iter_participants=None), "internal"),
    ]
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    for args, client, kind in cases:
        client.iter_participants = lambda *a, **k: boom()
        monkeypatch.setattr(runtime, "clients", {"one": client})
        result = await call("get_participants", **args)
        assert result.isError is True and result.meta[META]["kind"] == kind, (args, result.meta)


@pytest.mark.asyncio
async def test_toolerror_raised_by_a_tool_body_is_recorded_internal_only_through_fan_out(
    monkeypatch, raise_probe
):
    """Documents the single-account / fan-out difference for a ToolError nothing recorded."""
    single = await probe_call(monkeypatch, ["one"], "toolerror")
    multi = await probe_call(monkeypatch, ["a", "b"], "toolerror")
    recorded = await probe_call(monkeypatch, ["one"], "recorded")
    assert recorded.meta == {META: {"kind": "internal"}}
    assert multi.meta[META]["kind"] == "internal"
    assert single.isError is True
    assert single.meta == {META: {"kind": "invalid_argument"}}  # see the xfail below


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="an unrecorded raised ToolError / escaped exception is reported as invalid_argument "
    "in single-account mode (fan-out says internal): an internal failure that skipped "
    "log_and_format_error invites a pointless retry with different arguments",
)
@pytest.mark.parametrize("mode", ["toolerror", "runtime"])
async def test_unrecorded_internal_failure_should_be_internal(monkeypatch, raise_probe, mode):
    result = await probe_call(monkeypatch, ["one"], mode)
    assert result.meta == {META: {"kind": "internal"}}


# --- parameter checks that run before (and between) the Telegram calls -------------


def _poll_create(**over):
    base = {"chat_id": 100, "question": "Q?", "options": ["a", "b"]}
    return {**base, **over}


def _poll_vote(**over):
    return {"chat_id": 100, "message_id": 3, "option_indices": [0], **over}


PARAM_REFUSALS = [
    ("create_poll", _poll_create(question="q" * 301), "exceed 300"),
    ("create_poll", _poll_create(options=["a", ""]), "cannot be empty"),
    ("create_poll", _poll_create(options=["a", "b" * 101]), "exceed 100"),
    ("create_poll", _poll_create(options=["a"]), "at least 2"),
    ("create_poll", _poll_create(options=[str(i) for i in range(11)]), "at most 10"),
    ("vote_poll", _poll_vote(message_id=0), "message_id must be a positive"),
    ("vote_poll", _poll_vote(option_indices=[]), "non-empty list"),
    ("vote_poll", _poll_vote(option_indices=[0, 0]), "duplicates"),
    ("vote_poll", _poll_vote(option_indices=[0, 1]), "only allows one answer"),
]
PARAM_OK = {
    "create_poll": (_poll_create(), lambda: fake({"SendMediaRequest": None}), "Poll created"),
    "vote_poll": (
        _poll_vote(),
        lambda: fake({"SendVoteRequest": None}, get_messages=_msgs(poll_message())),
        "Vote submitted",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,expected", PARAM_REFUSALS, ids=lambda v: v if isinstance(v, str) else ""
)
async def test_poll_parameter_refusals_and_the_poll_that_goes_through(
    monkeypatch, tool, args, expected
):
    ok_args, ok_make, ok_text = PARAM_OK[tool]
    pair = Pair(
        tool,
        "invalid_argument",
        (args, lambda: fake(get_messages=_msgs(poll_message()))),
        expected,
        (ok_args, ok_make),
        ok_text,
        id=f"{tool}-{expected}",
    )
    await check_pair(monkeypatch, pair)
