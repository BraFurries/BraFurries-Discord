from core.disboard_bump import (
    monthly_ranking_channel_id,
    resolve_realtime_bump_channel_id,
    should_process_disboard_confirmation,
)


def test_empty_processing_selection_preserves_legacy_behavior():
    assert should_process_disboard_confirmation(100, [])


def test_processing_selection_filters_confirmations_without_looking_at_legacy_fields():
    assert should_process_disboard_confirmation(100, [100])
    assert not should_process_disboard_confirmation(200, [100])


def test_processing_selection_does_not_change_monthly_ranking_channel():
    assert monthly_ranking_channel_id({"disboardChannelId": 777}) == 777
    assert monthly_ranking_channel_id({"disboardChannelId": 777, "bumpProcessingChannelId": 111}) == 777


def test_realtime_bump_channel_prefers_processing_over_legacy_warning_source():
    assert resolve_realtime_bump_channel_id([100], 200) == 100


def test_realtime_bump_channel_uses_legacy_fallback_then_preserves_open_behavior():
    assert resolve_realtime_bump_channel_id([], 200) == 200
    assert resolve_realtime_bump_channel_id([], None) is None


def test_realtime_bump_channel_deterministically_uses_first_processing_row():
    assert resolve_realtime_bump_channel_id([300, 100, 200], 400) == 300
