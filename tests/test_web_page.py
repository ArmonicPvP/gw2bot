from gw2bot.web.page import CALENDAR_PAGE


class TestCalendarMarkdown:
    def test_renders_discord_subtext_lines(self) -> None:
        assert 'var subtext = /^-#\\s+(.*)$/.exec(line);' in CALENDAR_PAGE
        assert 'el("div", "md-subtext")' in CALENDAR_PAGE
        assert "#tooltip .desc .md-subtext" in CALENDAR_PAGE
