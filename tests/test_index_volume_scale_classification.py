from scripts.classify_index_volume_scale_anomalies import formal_to_source_factor


def test_formal_to_source_factor_labels_common_scales():
    assert formal_to_source_factor(100.0, 10000.0) == "1/100"
    assert formal_to_source_factor(100.0, 1000.0) == "1/10"
    assert formal_to_source_factor(1000.0, 100.0) == "10"


def test_formal_to_source_factor_handles_zero_source():
    assert formal_to_source_factor(100.0, 0.0) == "source_zero"
    assert formal_to_source_factor(0.0, 0.0) == "both_zero"
