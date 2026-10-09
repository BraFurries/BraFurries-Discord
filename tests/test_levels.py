from core.levels import level_from_total_xp, xp_to_reach_level


def test_xp_to_reach_level_zero_is_zero():
    config = {"phase1K": 12, "phase1P": 2, "phase1B": 3}
    assert xp_to_reach_level(0, config) == 0


def test_xp_curve_is_monotonic_for_positive_levels():
    config = {"phase1K": 12, "phase1P": 2, "phase1B": 3}
    for level in range(1, 250):
        assert xp_to_reach_level(level + 1, config) > xp_to_reach_level(level, config)


def test_first_level_has_no_excessive_initial_barrier():
    config = {"phase1K": 12, "phase1P": 2, "phase1B": 3}
    # For L=1, required = k*(1**p) + b*1 == k + b
    assert xp_to_reach_level(1, config) == 15


def test_level_inverse_consistency_on_exact_thresholds():
    config = {"phase1K": 12, "phase1P": 2, "phase1B": 3}
    for level in [1, 2, 3, 10, 25, 100, 500]:
        total_xp = xp_to_reach_level(level, config)
        assert level_from_total_xp(total_xp, config) >= level


def test_level_inverse_fallback_on_flat_curve_returns_finite_value():
    config = {"phase1K": 0.001, "phase1P": 0.001, "phase1B": 0}
    assert level_from_total_xp(1, config, max_iterations=8) == 1
