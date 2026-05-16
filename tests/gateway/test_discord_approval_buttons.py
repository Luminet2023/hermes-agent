"""Tests for Discord approval embeds and dynamic approval choices."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3,
        green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: "orange",
        green=lambda: "green",
        blue=lambda: "blue",
        red=lambda: "red",
        purple=lambda: "purple",
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import gateway.platforms.discord as discord_module  # noqa: E402
from gateway.platforms.discord import DiscordAdapter  # noqa: E402


class _FakeEmbed:
    def __init__(self, title, description, color):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []
        self.footer_text = None

    def add_field(self, *, name, value, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline})

    def set_footer(self, *, text):
        self.footer_text = text


class _FakeButton:
    def __init__(self, *, label, style):
        self.label = label
        self.style = style
        self.disabled = False
        self.callback = None


def _make_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = set()
    return adapter


@pytest.mark.asyncio
async def test_send_exec_approval_uses_custom_title_and_one_shot_buttons(monkeypatch):
    adapter = _make_adapter()
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=321)))
    adapter._client = SimpleNamespace(
        get_channel=lambda _target_id: channel,
        fetch_channel=AsyncMock(),
    )

    monkeypatch.setattr(discord_module.discord, "Embed", _FakeEmbed)
    monkeypatch.setattr(discord_module.discord.ui, "Button", _FakeButton)

    result = await adapter.send_exec_approval(
        chat_id="555",
        command="switch task abc from docker to host/local execution",
        session_key="operation-session",
        description="Allow this task to leave Docker and run on the host.",
        metadata={
            "approval_title": "Host Environment Approval",
            "approval_choices": ["once", "deny"],
        },
    )

    assert result.success is True
    kwargs = channel.send.call_args[1]
    embed = kwargs["embed"]
    view = kwargs["view"]
    assert embed.title == "⚠️ Host Environment Approval"
    assert embed.fields[0]["value"] == "Allow this task to leave Docker and run on the host."
    assert view.allowed_choices == ["once", "deny"]
    assert [button.label for button in view.children] == ["Allow Once", "Deny"]


@pytest.mark.asyncio
async def test_exec_approval_view_rejects_disallowed_choice(monkeypatch):
    monkeypatch.setattr(discord_module.discord.ui, "Button", _FakeButton)

    view = discord_module.ExecApprovalView(
        session_key="operation-session",
        allowed_user_ids=set(),
        choices=["once", "deny"],
    )

    interaction = SimpleNamespace(
        user=SimpleNamespace(id="u1", display_name="Bob"),
        message=SimpleNamespace(embeds=[_FakeEmbed("t", "d", "orange")]),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
    )

    with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
        await view._resolve(interaction, "session", "blue", "Approved for session")

    mock_resolve.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert "not allowed" in interaction.response.send_message.call_args[0][0].lower()
    assert view.resolved is False
