from core.xp_policy import XpPolicy


def test_text_cap_zero_does_not_limit_when_global_zero():
    config = {"globalDailyCapXp": 0, "textDailyCapXp": 0, "voiceDailyCapXp": 100}
    award = XpPolicy.award_text_xp(base_xp=20, config=config, today_total_xp=10_000)
    assert award.granted_xp == 20
    assert award.blocked is False


def test_voice_cap_zero_does_not_limit_when_global_zero():
    config = {"globalDailyCapXp": 0, "textDailyCapXp": 100, "voiceDailyCapXp": 0}
    award = XpPolicy.award_voice_xp(base_xp=20, config=config, today_total_xp=10_000)
    assert award.granted_xp == 20
    assert award.blocked is False


def test_global_cap_overrides_zero_source_caps():
    config = {"globalDailyCapXp": 50, "textDailyCapXp": 0, "voiceDailyCapXp": 0}
    award = XpPolicy.award_text_xp(base_xp=20, config=config, today_total_xp=45)
    assert award.granted_xp == 5
    assert award.blocked is False


def test_text_source_cap_is_enforced_independently():
    config = {"globalDailyCapXp": 0, "textDailyCapXp": 30, "voiceDailyCapXp": 0}
    award = XpPolicy.award_text_xp(base_xp=20, config=config, today_total_xp=5, today_source_xp=25)
    assert award.granted_xp == 5


def test_global_and_source_caps_apply_together():
    config = {"globalDailyCapXp": 50, "textDailyCapXp": 30, "voiceDailyCapXp": 0}
    award = XpPolicy.award_text_xp(base_xp=20, config=config, today_total_xp=45, today_source_xp=10)
    assert award.granted_xp == 5


def test_text_source_can_be_disabled_explicitly():
    config = {
        "textXpEnabled": False,
        "globalDailyCapXp": 0,
        "textDailyCapXp": 0,
    }
    award = XpPolicy.award_text_xp(base_xp=16, config=config, today_total_xp=0)
    assert award.granted_xp == 0
    assert award.blocked is True
    assert award.reason == "source_disabled"


def test_voice_source_can_be_disabled_explicitly():
    config = {
        "voiceXpEnabled": False,
        "globalDailyCapXp": 0,
        "voiceDailyCapXp": 0,
    }
    award = XpPolicy.award_voice_xp(base_xp=20, config=config, today_total_xp=0)
    assert award.granted_xp == 0
    assert award.blocked is True
    assert award.reason == "source_disabled"
