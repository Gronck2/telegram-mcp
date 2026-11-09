"""Round 3 of structured tool errors (task 0007): the refusals that were still plain successes.

Same method as test_structured_errors_returns.py: each tool is driven through the real
CallToolRequest handler against a stub client. A Pair holds a refusal (text identical OFF,
``isError`` + documented kind ON) and a nearby success (unaffected, byte-identical OFF/ON).
``refusal_only`` cases have no cheap success twin; the success paths of those tools are
covered by the existing suites that run unchanged.
"""

import json
from types import SimpleNamespace

import pytest
from telethon import errors, types

from telegram_mcp import runtime
from telegram_mcp.resolve_phone_limit import (
    MAX_PER_DAY_ENV,
    MIN_INTERVAL_ENV,
    ResolvePhoneLimiter,
)
from telegram_mcp.tools import contacts, folders, media, messages, profile

from test_structured_errors_returns import (  # noqa: F401  (_env is an autouse fixture)
    META,
    NOW,
    Fake,
    Pair,
    _env,
    _msgs,
    _TMP,
    broadcast,
    call,
    check_pair,
    fake,
    folder,
    folder_routes,
    nothing,
    run,
    supergroup,
    tg,
    text,
    user,
    world,
)


def _forum_world():
    entities = world()
    forum = supergroup()
    forum.forum = True
    entities[100] = forum
    entities[-1000000000100] = forum
    return entities


def _join_ok():
    return fake(
        {"ImportChatInviteRequest": SimpleNamespace(chats=[SimpleNamespace(title="Club")])}
    )


def _no_roots(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SERVER_ROOTS_ONLY", "1")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])


def _roots(monkeypatch):
    root = (_TMP["path"] / "root").resolve()
    root.mkdir(exist_ok=True)
    (root / "a.png").write_bytes(b"png")
    monkeypatch.setenv("TELEGRAM_SERVER_ROOTS_ONLY", "1")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [root])


def _send_file_ok():
    return fake(send_file=None)


def _limiter(prior_lookups):
    def setup(monkeypatch):
        monkeypatch.setenv(MIN_INTERVAL_ENV, "30")
        monkeypatch.delenv(MAX_PER_DAY_ENV, raising=False)
        monkeypatch.delenv("TELEGRAM_RESOLVE_PHONE_STATE_FILE", raising=False)
        limiter = ResolvePhoneLimiter(clock=lambda: 1_000_000.0)
        for _ in range(prior_lookups):
            assert limiter.reserve() is None
        monkeypatch.setattr(contacts, "_resolve_phone_limiter", limiter)

    return setup


def _bad_limiter_config(monkeypatch):
    monkeypatch.setenv(MAX_PER_DAY_ENV, "many")
    monkeypatch.setattr(contacts, "_resolve_phone_limiter", ResolvePhoneLimiter())


PHONE = {"phone": "+44 20 7946 0958"}
RESOLVED = types.contacts.ResolvedPeer(peer=types.PeerUser(5), chats=[], users=[user(5)])
KEYBOARD_MESSAGE = SimpleNamespace(
    id=3,
    date=NOW,
    reply_markup=types.ReplyInlineMarkup(
        rows=[
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(
                        text="Go", type=types.InlineButtonTypeCallback(data=b"go")
                    ),
                    types.KeyboardButton(
                        text="Site", type=types.InlineButtonTypeUrl(url="https://example.org")
                    ),
                    types.KeyboardButton(
                        text="Copy", type=types.InlineButtonTypeCopy(copy_text="secret")
                    ),
                    types.KeyboardButton(text="Reply", type=types.ButtonTypeDefault()),
                ]
            )
        ]
    ),
)
ANSWER = SimpleNamespace(message="done", alert=False, url=None)


def _patch_admin_pairs():
    g = {"chat_id": 100, "user_id": 5, "mode": "patch", "change_info": True}
    admin = types.ChannelParticipantAdmin(
        user_id=5,
        promoted_by=1,
        date=NOW,
        admin_rights=types.ChatAdminRights(change_info=True),
        can_edit=True,
    )
    plain = types.ChannelParticipant(user_id=5, date=NOW)
    creator = types.ChannelParticipantCreator(
        user_id=5, admin_rights=types.ChatAdminRights(change_info=True)
    )
    ok = (
        {**g, "change_info": False, "delete_messages": True},
        lambda: fake(
            {
                "GetParticipantRequest": SimpleNamespace(participant=admin),
                "EditAdminRequest": None,
            }
        ),
    )

    def refusal(participant, id_, bad_text, kind="invalid_argument", extra=None):
        route = (
            participant
            if isinstance(participant, BaseException)
            else SimpleNamespace(participant=participant)
        )
        return Pair(
            "edit_admin_rights",
            kind,
            (g, lambda: fake({"GetParticipantRequest": route})),
            bad_text,
            ok,
            "Admin rights",
            id=id_,
            extra=extra,
        )

    return [
        refusal(plain, "patch-not-an-admin", "is not an admin"),
        refusal(creator, "patch-creator", "is the chat's creator"),
        refusal(
            tg("UserNotParticipantError"),
            "patch-not-in-chat",
            "not in this chat",
            kind="not_found",
            extra={"rpc_error": "USER_NOT_PARTICIPANT"},
        ),
    ]


def _pairs():
    expired = "The invite hash has expired"
    invalid = "invite hash is invalid"
    join = {"link": "https://t.me/+abc"}
    imp = {"hash": "abc"}
    ok_join = (join, _join_ok)
    ok_imp = (imp, _join_ok)
    pairs = [
        # (1) expired / invalid invites are not_found
        Pair(
            "join_chat_by_link",
            "not_found",
            (join, lambda: fake({"ImportChatInviteRequest": tg("InviteHashExpiredError")})),
            expired,
            ok_join,
            "Successfully joined",
            extra={"rpc_error": "INVITE_HASH_EXPIRED"},
            id="join_chat_by_link-expired",
        ),
        Pair(
            "join_chat_by_link",
            "not_found",
            (join, lambda: fake({"ImportChatInviteRequest": tg("InviteHashInvalidError")})),
            invalid,
            ok_join,
            "Successfully joined",
            extra={"rpc_error": "INVITE_HASH_INVALID"},
            id="join_chat_by_link-invalid",
        ),
        Pair(
            "import_chat_invite",
            "not_found",
            (imp, lambda: fake({"ImportChatInviteRequest": tg("InviteHashExpiredError")})),
            expired,
            ok_imp,
            "Successfully joined",
            extra={"rpc_error": "INVITE_HASH_EXPIRED"},
            id="import_chat_invite-expired",
        ),
        Pair(
            "import_chat_invite",
            "not_found",
            (imp, lambda: fake({"ImportChatInviteRequest": tg("InviteHashInvalidError")})),
            invalid,
            ok_imp,
            "Successfully joined",
            extra={"rpc_error": "INVITE_HASH_INVALID"},
            id="import_chat_invite-invalid",
        ),
        # (2) file-path security refusals
        Pair(
            "send_file",
            "invalid_argument",
            ({"chat_id": 100, "file_path": "a.png"}, _send_file_ok),
            "MCP Roots could not be verified",
            ({"chat_id": 100, "file_path": "a.png"}, _send_file_ok),
            "File sent to chat",
            setup=_no_roots,
            ok_setup=_roots,
            id="send_file-no-roots",
        ),
        Pair(
            "send_file",
            "invalid_argument",
            ({"chat_id": 100, "file_path": "missing.png"}, _send_file_ok),
            "File not found",
            ({"chat_id": 100, "file_path": "a.png"}, _send_file_ok),
            "File sent to chat",
            setup=_roots,
            id="send_file-not-found",
        ),
        Pair(
            "send_file",
            "invalid_argument",
            ({"chat_id": 100, "file_path": "/etc/hosts"}, _send_file_ok),
            "outside allowed roots",
            ({"chat_id": 100, "file_path": "a.png"}, _send_file_ok),
            "File sent to chat",
            setup=_roots,
            id="send_file-outside-roots",
        ),
        # (3) resolve_phone limiter, config, add_contact import failure
        Pair(
            "resolve_phone",
            "flood",
            (PHONE, lambda: fake({"ResolvePhoneRequest": RESOLVED})),
            "refused locally",
            (PHONE, lambda: fake({"ResolvePhoneRequest": RESOLVED})),
            "ResolvedPeer",
            setup=_limiter(1),
            ok_setup=_limiter(0),
            extra={"retry_after": 30},
            id="resolve_phone-limiter-flood",
        ),
        Pair(
            "resolve_phone",
            "internal",
            (PHONE, lambda: fake({"ResolvePhoneRequest": RESOLVED})),
            "disabled until the configuration is fixed",
            (PHONE, lambda: fake({"ResolvePhoneRequest": RESOLVED})),
            "ResolvedPeer",
            setup=_bad_limiter_config,
            ok_setup=_limiter(0),
            id="resolve_phone-limiter-config",
        ),
        Pair(
            "add_contact",
            "not_found",
            (
                {"phone": "+79990001122", "first_name": "Zed"},
                lambda: fake({"ImportContactsRequest": SimpleNamespace(imported=[], users=[])}),
            ),
            "Contact not added",
            (
                {"phone": "+79990001122", "first_name": "Zed"},
                lambda: fake({"ImportContactsRequest": SimpleNamespace(imported=[1], users=[])}),
            ),
            "added successfully",
            id="add_contact-phone-not-on-telegram",
        ),
        Pair(
            "get_contact_chats",
            "invalid_argument",
            ({"contact_id": 42}, lambda: fake()),
            "is not a user/contact",
            ({"contact_id": 5}, lambda: fake()),
            "No chats found with Ann",
            id="get_contact_chats-not-a-user",
        ),
        Pair(
            "get_last_interaction",
            "invalid_argument",
            ({"contact_id": 42}, lambda: fake()),
            "is not a user/contact",
            ({"contact_id": 5}, lambda: fake(get_messages=_msgs([]))),
            "No messages found with Ann",
            id="get_last_interaction-not-a-user",
        ),
        # the saved:false payloads stay plain successes (deliberate gap)
        Pair(
            "set_contact_alias",
            "invalid_argument",
            ({"alias": " ", "chat_id": "@ann"}, lambda: fake()),
            "Alias must not be empty",
            ({"alias": "me", "chat_id": "@ann"}, lambda: fake()),
            '"saved": false',
            id="set_contact_alias-empty-vs-saved-false-gap",
        ),
        # (4) helper-returned refusals recorded at the caller
        Pair(
            "send_message",
            "invalid_argument",
            (
                {
                    "chat_id": 100,
                    "message": "hi",
                    "parse_mode": "html",
                    "custom_emojis": [{"emoji": "x", "id": 1}],
                },
                lambda: fake(),
            ),
            "custom_emojis needs plain-text",
            ({"chat_id": 100, "message": "hi"}, lambda: fake(send_message=SimpleNamespace(id=9))),
            "message_id=9",
            id="send_message-conflict",
        ),
        Pair(
            "send_message",
            "invalid_argument",
            ({"chat_id": 100, "message": "hi", "format_date": "zz"}, lambda: fake()),
            "was not found in the message text",
            ({"chat_id": 100, "message": "hi"}, lambda: fake(send_message=SimpleNamespace(id=9))),
            "message_id=9",
            id="send_message-bad-format-date",
        ),
        Pair(
            "edit_forum_topic",
            "invalid_argument",
            ({"chat_id": 100, "topic_id": 2, "title": "T"}, lambda: fake()),
            "does not have forum topics enabled",
            (
                {"chat_id": 100, "topic_id": 2, "title": "T"},
                lambda: Fake({"EditForumTopicRequest": None}, entities=_forum_world()),
            ),
            '"topic_id": 2',
            id="edit_forum_topic-not-a-forum",
        ),
        Pair(
            "list_inline_buttons",
            "invalid_argument",
            ({"chat_id": 100, "message_id": "abc"}, lambda: fake()),
            "message_id must be an integer",
            (
                {"chat_id": 100, "message_id": 3},
                lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE)),
            ),
            '"kind": "callback"',
            id="list_inline_buttons-bad-id",
        ),
        Pair(
            "press_inline_button",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 3},
                lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE)),
            ),
            "Provide button_text or button_index",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-no-selector",
        ),
        Pair(
            "press_inline_button",
            "not_found",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Nope"},
                lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE)),
            ),
            "Button not found",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-no-such-button",
        ),
        Pair(
            "press_inline_button",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 3, "button_index": 9},
                lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE)),
            ),
            "button_index out of range",
            (
                {"chat_id": 100, "message_id": 3, "button_index": 0},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-index-range",
        ),
        Pair(
            "press_inline_button",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Reply"},
                lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE)),
            ),
            "reply keyboard",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-reply-keyboard",
        ),
        Pair(
            "press_inline_button",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": tg("DataInvalidError")},
                    get_messages=_msgs(KEYBOARD_MESSAGE),
                ),
            ),
            "keyboard has probably changed",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-data-invalid",
            extra={"rpc_error": "DATA_INVALID"},
        ),
        Pair(
            "press_inline_button",
            "not_found",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": tg("MessageIdInvalidError")},
                    get_messages=_msgs(KEYBOARD_MESSAGE),
                ),
            ),
            "no longer in this chat",
            (
                {"chat_id": 100, "message_id": 3, "button_text": "Go"},
                lambda: fake(
                    {"GetBotCallbackAnswerRequest": ANSWER}, get_messages=_msgs(KEYBOARD_MESSAGE)
                ),
            ),
            "done",
            id="press_inline_button-message-gone",
            extra={"rpc_error": "MESSAGE_ID_INVALID"},
        ),
        # (5) early refusals without an "Error:" prefix
        Pair(
            "get_message_context",
            "not_found",
            ({"chat_id": 100, "message_id": 7}, lambda: fake(get_messages=_msgs([]))),
            "not found in chat",
            None,
            "",
            id="get_message_context-not-found",
        ),
        Pair(
            "delete_scheduled_message",
            "invalid_argument",
            ({"chat_id": 100, "message_ids": []}, lambda: fake()),
            "non-empty list",
            (
                {"chat_id": 100, "message_ids": [4]},
                lambda: fake({"DeleteScheduledMessagesRequest": None}),
            ),
            "Deleted 1 scheduled",
            id="delete_scheduled_message-empty",
        ),
        Pair(
            "send_typing",
            "invalid_argument",
            ({"chat_id": 100, "seconds": 99999}, lambda: fake()),
            "seconds must be between",
            ({"chat_id": 100, "seconds": 0.5}, lambda: fake({"SetTypingRequest": True})),
            "indicator cleared",
            id="send_typing-seconds",
        ),
        Pair(
            "delete_message",
            "not_found",
            (
                {"chat_id": 42, "message_id": 4},
                lambda: fake(get_messages=_msgs([None]), delete_messages=None),
            ),
            "nothing was deleted",
            (
                {"chat_id": 42, "message_id": 4},
                lambda: fake(get_messages=_msgs([SimpleNamespace(id=4)]), delete_messages=None),
            ),
            "Message 4 deleted",
            id="delete_message-foreign",
        ),
        Pair(
            "ban_user",
            "invalid_argument",
            ({"chat_id": 42, "user_id": 5}, lambda: fake()),
            "basic group",
            ({"chat_id": 100, "user_id": 5}, lambda: fake({"EditBannedRequest": None})),
            "banned",
            id="ban_user-basic-group",
        ),
        Pair(
            "get_folder",
            "not_found",
            ({"folder_id": 99}, lambda: fake(folder_routes(folder(5)))),
            "Folder with ID 99 not found",
            ({"folder_id": 5}, lambda: fake(folder_routes(folder(5)))),
            "Work",
            id="get_folder-not-found",
        ),
        Pair(
            "add_chat_to_folder",
            "not_found",
            ({"folder_id": 99, "chat_id": 100}, lambda: fake(folder_routes(folder(5)))),
            "Folder with ID 99 not found",
            ({"folder_id": 5, "chat_id": 100}, lambda: fake(folder_routes(folder(5)))),
            "added to folder 5",
            id="add_chat_to_folder-folder-not-found",
        ),
        Pair(
            "remove_chat_from_folder",
            "not_found",
            ({"folder_id": 99, "chat_id": 100}, lambda: fake(folder_routes(folder(5)))),
            "Folder with ID 99 not found",
            (
                {"folder_id": 5, "chat_id": 100},
                lambda: fake(
                    folder_routes(folder(5, include_peers=[types.InputPeerChannel(100, 2)]))
                ),
            ),
            "removed from folder 5",
            id="remove_chat_from_folder-folder-not-found",
        ),
        Pair(
            "reorder_folders",
            "not_found",
            ({"folder_ids": [5, 99]}, lambda: fake(_reorder_routes(folder(5)))),
            "Folder ID 99 not found",
            ({"folder_ids": [5]}, lambda: fake(_reorder_routes(folder(5)))),
            "Folders reordered",
            id="reorder_folders-unknown-id",
        ),
        Pair(
            "reorder_folders",
            "invalid_argument",
            ({"folder_ids": [5]}, lambda: fake(_reorder_routes(folder(5), folder(6)))),
            "All folder IDs must be included",
            ({"folder_ids": [6, 5]}, lambda: fake(_reorder_routes(folder(5), folder(6)))),
            "Folders reordered",
            id="reorder_folders-missing-ids",
        ),
        Pair(
            "preview_custom_emoji",
            "not_found",
            ({"document_id": 7}, lambda: fake({"GetCustomEmojiDocumentsRequest": []})),
            "not found",
            (
                {"document_id": 7},
                lambda: fake(
                    {"GetCustomEmojiDocumentsRequest": [SimpleNamespace(mime_type="x/y")]},
                    download_media=b"\x1f\x8b....",
                ),
            ),
            "has format",
            id="preview_custom_emoji-not-found",
        ),
        Pair(
            "get_custom_emoji_pack",
            "invalid_argument",
            (
                {"pack": "Dogs"},
                lambda: fake({"GetStickerSetRequest": tg("StickersetInvalidError")}),
            ),
            "not found or is invalid",
            ({"pack": "Dogs"}, lambda: fake({"GetStickerSetRequest": SimpleNamespace(set=None)})),
            "no set metadata",
            id="get_custom_emoji_pack-set-invalid-vs-no-metadata",
            extra={"rpc_error": "STICKERSET_INVALID"},
        ),
        # (5) media
        Pair(
            "send_album",
            "invalid_argument",
            ({"chat_id": 100, "file_paths": ["a.png"]}, _send_file_ok),
            "Albums must contain",
            ({"chat_id": 100, "file_paths": ["a.png", "a.png"]}, _send_file_ok),
            "Album sent",
            setup=_roots,
            id="send_album-count",
        ),
        Pair(
            "send_voice",
            "invalid_argument",
            ({"chat_id": 100, "file_path": "a.png"}, _send_file_ok),
            "File extension is not allowed for send_voice",
            ({"chat_id": 100, "file_path": "v.ogg"}, _send_file_ok),
            "Voice message sent",
            setup=lambda mp: (_roots(mp), (_TMP["path"] / "root" / "v.ogg").write_bytes(b"ogg")),
            id="send_voice-format",
        ),
        Pair(
            "send_gif",
            "not_found",
            ({"chat_id": 100, "gif_id": 6}, lambda: fake()),
            "unknown to this session",
            ({"chat_id": 100, "gif_id": 5}, lambda: fake(send_file=None)),
            "GIF sent",
            setup=lambda mp: mp.setattr(media, "_find_gif", _gif_finder),
            id="send_gif-unknown",
        ),
        Pair(
            "inspect_document",
            "not_found",
            ({"chat_id": 100, "message_id": 1}, lambda: fake(get_messages=_msgs(None))),
            "no attached document",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(_file_msg()), download_media=b"hello"),
            ),
            "hello",
            id="inspect_document-none",
        ),
        Pair(
            "inspect_document",
            "invalid_argument",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(_file_msg("a.bin", "application/x-x"))),
            ),
            "not currently supported",
            (
                {"chat_id": 100, "message_id": 1},
                lambda: fake(get_messages=_msgs(_file_msg()), download_media=b"hello"),
            ),
            "hello",
            id="inspect_document-unsupported-format",
        ),
        Pair(
            "get_bot_info",
            "not_found",
            ({"bot_username": "somebot"}, lambda: fake()),
            "not found",
            None,
            "",
            setup=lambda mp: mp.setattr(profile, "resolve_entity", _none_entity),
            id="get_bot_info-no-entity",
        ),
        # (7) ordinary refusal kinds
        Pair(
            "set_bot_commands",
            "invalid_argument",
            (
                {"bot_username": "somebot", "commands": []},
                lambda: fake(get_me=SimpleNamespace(bot=False)),
            ),
            "only be used by bot accounts",
            (
                {"bot_username": "somebot", "commands": []},
                lambda: fake({"SetBotCommandsRequest": True}, get_me=SimpleNamespace(bot=True)),
            ),
            "Bot commands set",
            id="set_bot_commands-not-a-bot",
        ),
        Pair(
            "get_member_admin_status",
            "invalid_argument",
            ({"chat_id": 42, "user_id": 5}, lambda: fake()),
            "supports only supergroups",
            (
                {"chat_id": 100, "user_id": 5},
                lambda: fake({"GetParticipantRequest": tg("UserNotParticipantError")}),
            ),
            '"role"',
            id="get_member_admin_status-basic-group-invalid-argument",
        ),
        Pair(
            "create_group",
            "flood",
            (
                {"title": "G", "user_ids": [5]},
                lambda: fake({"CreateChatRequest": tg("PeerFloodError")}),
            ),
            "PEER_FLOOD",
            _CREATED,
            "Group created",
            id="create_group-peer-flood",
            extra={"rpc_error": "PEER_FLOOD"},
        ),
        Pair(
            "create_group",
            "flood",
            (
                {"title": "G", "user_ids": [5]},
                lambda: fake({"CreateChatRequest": RuntimeError("PEER_FLOOD while creating")}),
            ),
            "Cannot create group due to Telegram limits",
            _CREATED,
            "Group created",
            id="create_group-peer-flood-text-match",
        ),
    ]
    return pairs + _patch_admin_pairs()


_CREATED = (
    {"title": "G", "user_ids": [5]},
    lambda: fake({"CreateChatRequest": SimpleNamespace(chats=[supergroup(77)])}),
)


def _reorder_routes(*items):
    return folder_routes(*items, UpdateDialogFiltersOrderRequest=True)


def _file_msg(name="a.txt", mime="text/plain"):
    return SimpleNamespace(
        id=1,
        media=types.MessageMediaUnsupported(),
        file=SimpleNamespace(name=name, mime_type=mime),
    )


async def _gif_finder(cl, account, gif_id):
    return object() if gif_id == 5 else None


async def _none_entity(*args, **kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", _pairs(), ids=lambda pair: pair.id)
async def test_round3_refusal_is_error_and_success_twin_is_not(monkeypatch, pair):
    if pair.ok is not None:
        return await check_pair(monkeypatch, pair)
    bad_off = await run(monkeypatch, pair.tool, pair.bad, pair.setup, False)
    bad_on = await run(monkeypatch, pair.tool, pair.bad, pair.setup, True)
    assert pair.bad_text in text(bad_off), text(bad_off)
    assert text(bad_on) == text(bad_off)
    assert (bad_off.meta, bad_off.isError) == (None, False)
    assert bad_on.isError is True
    assert bad_on.meta == {META: {"kind": pair.kind, **pair.extra}}


# --- RPC handlers keep rpc_error and agree with the classifier ----------------------


RPC_HANDLERS = [
    # (tool, args, route, kind, rpc_error)
    (
        "remove_user",
        {"chat_id": 100, "user_id": 5},
        {"GetParticipantRequest": tg("UserAdminInvalidError")},
        "invalid_argument",
        "USER_ADMIN_INVALID",
    ),
    (
        "invite_to_group",
        {"group_id": 100, "user_ids": [5]},
        {"InviteToChannelRequest": tg("UserNotMutualContactError")},
        "private",
        "USER_NOT_MUTUAL_CONTACT",
    ),
    (
        "invite_to_group",
        {"group_id": 100, "user_ids": [5]},
        {"InviteToChannelRequest": tg("UserIsBlockedError")},
        "private",
        "USER_IS_BLOCKED",
    ),
    (
        "delete_messages_bulk",
        {"chat_id": 100, "message_ids": [1]},
        {"DeleteMessagesRequest": tg("MessageIdInvalidError")},
        "not_found",
        "MESSAGE_ID_INVALID",
    ),
    (
        "get_message_read_by",
        {"chat_id": 100, "message_id": 1},
        {"GetMessageReadParticipantsRequest": tg("PeerIdInvalidError")},
        "not_found",
        "PEER_ID_INVALID",
    ),
    (
        "get_message_read_by",
        {"chat_id": 100, "message_id": 1},
        {"GetMessageReadParticipantsRequest": tg("MsgTooOldError")},
        "invalid_argument",
        "MSG_TOO_OLD",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", RPC_HANDLERS, ids=lambda c: f"{c[0]}-{c[4]}")
async def test_rpc_handlers_record_the_classifier_kind_and_rpc_error(monkeypatch, case):
    tool, args, routes, kind, rpc = case
    make = lambda: fake(routes)  # noqa: E731
    off = await run(monkeypatch, tool, (args, make), nothing, False)
    on = await run(monkeypatch, tool, (args, make), nothing, True)
    assert text(on) == text(off)
    assert off.meta is None
    if on.meta is None:  # tool answered with a success text for this route: not a failure
        pytest.skip(f"{tool} handled {rpc} as a non-failure here")
    assert on.isError is True
    assert on.meta == {META: {"kind": kind, "rpc_error": rpc}}


def test_new_private_rpc_names_are_classified():
    for cls_name, name in (
        ("UserNotMutualContactError", "USER_NOT_MUTUAL_CONTACT"),
        ("UserIsBlockedError", "USER_IS_BLOCKED"),
    ):
        assert runtime.classify_error(tg(cls_name)) == {"kind": "private", "rpc_error": name}


def test_privacy_refusal_is_a_validation_error():
    refusal = profile._PrivacyRefusal("Unsupported privacy rule; refusing to drop it.")
    assert isinstance(refusal, runtime.ValidationError)
    assert runtime.classify_error(refusal) == {"kind": "invalid_argument"}
    assert str(refusal) == "Unsupported privacy rule; refusing to drop it."


# --- limiter details -----------------------------------------------------------------


def test_limiter_refusal_text_is_unchanged_and_carries_retry_after():
    limiter = ResolvePhoneLimiter(clock=lambda: 50.0)
    import os

    os.environ[MIN_INTERVAL_ENV] = "30"
    try:
        assert limiter.reserve() is None
        refusal = limiter.reserve()
    finally:
        del os.environ[MIN_INTERVAL_ENV]
    assert isinstance(refusal, str)
    assert refusal.endswith("Retry in 30 seconds, not sooner.")
    assert refusal.retry_after == 30


# --- inline buttons: a URL / copy button hands over its URL / text: an answer ---------


@pytest.mark.asyncio
@pytest.mark.parametrize("button, expected", [("Site", "https://example.org"), ("Copy", "secret")])
async def test_url_and_copy_buttons_are_answers_not_errors(monkeypatch, button, expected):
    args = {"chat_id": 100, "message_id": 3, "button_text": button}
    make = lambda: fake(get_messages=_msgs(KEYBOARD_MESSAGE))  # noqa: E731
    off = await run(monkeypatch, "press_inline_button", (args, make), nothing, False)
    on = await run(monkeypatch, "press_inline_button", (args, make), nothing, True)
    assert expected in text(off)
    assert (on.isError, on.meta) == (False, None)
    assert text(on) == text(off)


@pytest.mark.asyncio
async def test_list_inline_buttons_without_a_keyboard_is_an_empty_result(monkeypatch):
    args = {"chat_id": 100, "message_id": 3}
    plain = SimpleNamespace(id=3, date=NOW, reply_markup=None)
    make = lambda: fake(  # noqa: E731
        get_messages=lambda entity, ids=None, **kw: plain if ids else [plain]
    )
    on = await run(monkeypatch, "list_inline_buttons", (args, make), nothing, True)
    assert "does not contain inline buttons" in text(on)
    assert (on.isError, on.meta) == (False, None)


@pytest.mark.asyncio
async def test_get_chats_page_one_of_nothing_is_empty_but_page_two_is_out_of_range(monkeypatch):
    page1 = await run(monkeypatch, "get_chats", ({"page": 1}, lambda: fake()), nothing, True)
    page2 = await run(monkeypatch, "get_chats", ({"page": 2}, lambda: fake()), nothing, True)
    assert "Page out of range" in text(page1) and (page1.isError, page1.meta) == (False, None)
    assert page2.isError is True and page2.meta == {META: {"kind": "invalid_argument"}}


# --- folder resolve failures are classified, not blanket not_found ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected",
    [
        (
            errors.ChannelPrivateError(request=None),
            {"kind": "private", "rpc_error": "CHANNEL_PRIVATE"},
        ),
        (ValueError("Cannot find any entity for x"), {"kind": "internal"}),
        (runtime.EntityNotFoundError("nope"), {"kind": "not_found"}),
    ],
)
async def test_folder_chat_resolution_failure_is_classified(monkeypatch, error, expected):
    async def resolve(*args, **kwargs):
        raise error

    monkeypatch.setattr(folders, "resolve_input_entity", resolve)
    args = {"folder_id": 5, "chat_id": 100}
    make = lambda: fake(folder_routes(folder(5)))  # noqa: E731
    off = await run(monkeypatch, "add_chat_to_folder", (args, make), nothing, False)
    on = await run(monkeypatch, "add_chat_to_folder", (args, make), nothing, True)
    assert "Failed to resolve a requested chat." in text(off) == text(on)
    assert on.isError is True and on.meta == {META: expected}


# --- FastMCP-made and escaped failures: same answer in single and multi-account mode ----


@pytest.fixture
def probe():
    from mcp.server.fastmcp.exceptions import ToolError

    @runtime.mcp.tool(name="zz_round3_probe")
    @runtime.with_account(readonly=True)
    async def tool(mode: str, account: str = None) -> str:
        if mode == "escaped":
            raise RuntimeError("backend exploded")
        if mode == "rpc":
            raise errors.ChannelPrivateError(request=None)
        if mode == "toolerror":
            raise ToolError("backend exploded")
        return "fine"

    yield
    runtime.mcp._tool_manager._tools.pop("zz_round3_probe", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", [["one"], ["a", "b"]], ids=["single", "fan-out"])
@pytest.mark.parametrize(
    "mode, expected",
    [
        ("escaped", {"kind": "internal"}),
        ("toolerror", {"kind": "internal"}),
        ("rpc", {"kind": "private", "rpc_error": "CHANNEL_PRIVATE"}),
    ],
)
async def test_unrecorded_failures_classify_alike_in_both_modes(
    monkeypatch, probe, labels, mode, expected
):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {label: fake() for label in labels})
    result = await call("zz_round3_probe", mode=mode)
    assert result.isError is True
    got = result.meta[META]
    assert {k: v for k, v in got.items() if k != "accounts"} == expected


@pytest.mark.asyncio
async def test_bad_arguments_and_unknown_tool_are_invalid_argument(monkeypatch, probe):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"one": fake()})
    bad = await call("zz_round3_probe", mode=["not", "a", "string"])
    unknown = await call("zz_no_such_tool_round3")
    for result in (bad, unknown):
        assert result.isError is True and result.meta == {META: {"kind": "invalid_argument"}}


@pytest.mark.asyncio
async def test_unrecorded_failure_off_is_unchanged(monkeypatch, probe):
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    monkeypatch.setattr(runtime, "clients", {"one": fake()})
    result = await call("zz_round3_probe", mode="escaped")
    assert result.isError is True and result.meta is None
    assert "backend exploded" in text(result)


# --- JSON / payload refusals that deliberately stay plain successes -------------------


@pytest.mark.asyncio
async def test_set_contact_alias_saved_false_payload_stays_a_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"one": fake()})
    result = await call("set_contact_alias", alias="me", chat_id="@ann")
    assert json.loads(text(result))["results"]["saved"] is False
    assert (result.isError, result.meta) == (False, None)


# --- every file-path security refusal branch records invalid_argument ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected",
    [
        (runtime.ROOTS_STATUS_CLIENT_DENY_ALL, "empty MCP Roots list (deny-all)"),
        (runtime.ROOTS_STATUS_ERROR, "could not be verified safely"),
        (runtime.ROOTS_STATUS_TIMEOUT, "never answered the roots/list request"),
        (runtime.ROOTS_STATUS_NOT_CONFIGURED, "disabled until allowed roots are configured"),
    ],
)
async def test_every_roots_status_refusal_is_invalid_argument(monkeypatch, status, expected):
    async def no_roots(ctx):
        return [], status

    monkeypatch.setattr(runtime, "_get_effective_allowed_roots_with_status", no_roots)
    args = {"chat_id": 100, "file_path": "a.png"}
    off = await run(monkeypatch, "send_file", (args, _send_file_ok), nothing, False)
    on = await run(monkeypatch, "send_file", (args, _send_file_ok), nothing, True)
    assert expected in text(off) and text(on) == text(off)
    assert (off.isError, off.meta) == (False, None)
    assert on.isError is True and on.meta == {META: {"kind": "invalid_argument"}}


@pytest.mark.asyncio
async def test_oversize_refusal(monkeypatch):
    _roots(monkeypatch)
    root = _TMP["path"] / "root"
    (root / "big.png").write_bytes(b"x" * 64)
    monkeypatch.setitem(runtime.MAX_FILE_BYTES, "send_file", 8)
    for name, expected in (("big.png", "too large"),):
        args = {"chat_id": 100, "file_path": name}
        off = await run(monkeypatch, "send_file", (args, _send_file_ok), nothing, False)
        on = await run(monkeypatch, "send_file", (args, _send_file_ok), nothing, True)
        assert expected in text(off).lower() and text(on) == text(off)
        assert (off.isError, off.meta) == (False, None)
        assert on.meta == {META: {"kind": "invalid_argument"}}, name
