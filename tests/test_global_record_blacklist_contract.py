from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_record_blacklist_runtime_is_global_and_cannot_mutate_from_slash():
    database = (ROOT / "core/database.py").read_text(encoding="utf-8")
    records = (ROOT / "cogs/records.py").read_text(encoding="utf-8")
    assert "FROM blacklisted_games" not in database
    assert "record_global_blacklisted_games" in database
    assert "def addGameToBlacklist(" not in database
    assert "recorde_adicionar_blacklist" not in records


def test_both_profile_and_rank_sql_exclude_globally_and_writes_are_guarded():
    database = (ROOT / "core/database.py").read_text(encoding="utf-8")
    rank = database.split("def getGameRecordPosition(", 1)[1].split("def getProfileData(", 1)[0]
    profile = database.split("game_sql = (", 1)[1].split("cursor.execute(game_sql", 1)[0]
    writer = database.split("def updateGameRecord(", 1)[1].split("def increment_boop_counts(", 1)[0]
    assert rank.count("record_global_blacklisted_games") == 2
    assert profile.count("record_global_blacklisted_games") == 2
    assert "record_global_blacklisted_games" in writer
    assert "if cursor.fetchone():" in writer
