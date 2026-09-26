from app.ai.wait_time import predict_wait_time


def test_empty_queue_uses_recent_weighted_prep_time():
    result = predict_wait_time(0, 0, [4, 8])

    assert result["estimated_minutes"] == 7
    assert result["average_prep_minutes"] == 7
    assert result["crowd_level"] == "Low"
    assert result["sample_size"] == 2


def test_queue_load_increases_estimate_and_crowd_level():
    result = predict_wait_time(8, 2, [6, 6, 6])

    assert result["estimated_minutes"] == 16
    assert result["crowd_level"] == "Moderate"


def test_prep_samples_are_bounded():
    result = predict_wait_time(1, 0, [0, 400])

    assert result["average_prep_minutes"] <= 90