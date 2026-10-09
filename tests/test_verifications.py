from datetime import date

from core.verifications import verifyDate


def test_verify_date_accepts_spaced_numeric_tokens():
    assert verifyDate("0 3 / 0 1 / 1 9 9 4") == date(1994, 1, 3)


def test_verify_date_accepts_regular_numeric_format():
    assert verifyDate("03/01/1994") == date(1994, 1, 3)
