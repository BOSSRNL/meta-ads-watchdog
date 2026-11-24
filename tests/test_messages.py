import pytest

from meta_ads_watchdog.config import Rules
from meta_ads_watchdog.messages import TEXT, CampaignAlert, Messages, amount, rule_values
from meta_ads_watchdog.rules import Action

# The words a team searches Discord for. They must survive every translation.
KEYWORDS = {
    Action.PAUSE_SPIKE.value: "security", Action.PAUSE_NEW_BIG.value: "security",
    Action.PAUSE_ANOMALY.value: "anomaly", Action.PAUSE_HARD_LIMIT.value: "emergency pause",
    Action.PAUSE_ESCALATION.value: "escalation", "account_issue": "issue",
    "pause_failed": "issue", "scan_failed": "issue", "token_problem": "issue",
    "react_partial": "issue", "react_abort": "issue",
}


@pytest.mark.parametrize("language", sorted(TEXT))
def test_both_languages_have_the_same_keys(language):
    assert set(TEXT[language]) == set(TEXT["en"])


@pytest.mark.parametrize("language", sorted(TEXT))
@pytest.mark.parametrize("key, keyword", sorted(KEYWORDS.items()))
def test_alert_titles_keep_their_search_keyword(language, key, keyword):
    title = TEXT[language][key].splitlines()[0].lower()
    assert keyword in title


@pytest.mark.parametrize("language", sorted(TEXT))
def test_every_text_formats_with_the_values_it_gets(language):
    messages = Messages(language, rule_values(Rules()))
    fields = {"reason": "Payday", "team": "T", "status": "S", "name": "N", "account": "act_1",
              "campaign": "C", "campaign_id": "1", "planned": "p", "error": "e", "count": 2,
              "lines": "-", "ok": 1, "retry": 0, "manual": 0, "waiting": 0, "title": "t",
              "detail": "d", "actor": "a"}
    for key in TEXT[language]:
        text = messages.get(key, **fields)
        assert "{" not in text and "}" not in text


@pytest.mark.parametrize("action", [a for a in Action if a is not Action.KEEP])
@pytest.mark.parametrize("language", sorted(TEXT))
def test_campaign_alerts_render(action, language):
    alert = CampaignAlert("Team A", "act_1", "Shop", "111", "Promo", 500.0, 530.0, 50.0, None)
    text = Messages(language, rule_values(Rules())).campaign(action, alert)
    assert "Campaign ID: 111" in text and "Spend today: 530" in text
    assert ("50 → 500" in text) and (("Changed by" in text or "Ginawa ni" in text) is action.is_security)


def test_amount_formatting():
    assert (amount(1500), amount(1500.5), amount(None)) == ("1,500", "1,500.50", "-")

