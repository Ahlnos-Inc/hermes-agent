"""Bare Jira ticket ids in outgoing Telegram text become clickable links."""

from plugins.platforms.telegram.adapter import JIRA_BROWSE_URL, TelegramAdapter


def _fm(text: str) -> str:
    class _Stub:
        pass

    stub = _Stub()
    stub.format_message = TelegramAdapter.format_message.__get__(stub)
    return stub.format_message(text)


def test_bare_ticket_ids_become_links():
    out = _fm("Kanban t_1 blocked: OPS-132 approval required, see BUILD-503")
    assert f"[OPS\\-132]({JIRA_BROWSE_URL}OPS-132)" in out
    assert f"[BUILD\\-503]({JIRA_BROWSE_URL}BUILD-503)" in out


def test_code_and_existing_links_untouched():
    out = _fm("code `OPS-99`, block\n```\nlog OPS-777\n```\nold [OPS-1](https://x.co/a)")
    assert "`OPS-99`" in out
    assert "log OPS-777" in out
    assert "[OPS\\-1](https://x.co/a)" in out
    assert JIRA_BROWSE_URL not in out


def test_non_ticket_words_not_linkified():
    out = _fm("BUILDER-12 and OPS12 and PREOPS-3 stay plain")
    assert JIRA_BROWSE_URL not in out
