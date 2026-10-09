from forge_speed import TokenSpeedEstimator


def test_buffered_first_chunk_cannot_create_a_speed_spike():
    meter=TokenSpeedEstimator()
    meter.observe('x'*4000,now=100)
    assert meter.estimate(now=100) is None
    for i in range(1,41): meter.observe('abcd',now=100+i/20)
    assert 19.9<meter.estimate(now=102)<20.1


def test_sliding_window_adapts_to_slow_model_and_counts_thinking_once():
    meter=TokenSpeedEstimator()
    for i in range(301): meter.observe('abcd',now=i/20)
    assert 19.9<meter.estimate(now=15)<20.1
    for i in range(1,101): meter.observe('abcd',now=15+i/10)
    assert 9.9<meter.estimate(now=25)<10.1


def test_empty_packets_and_cancelled_response_do_not_invent_output():
    meter=TokenSpeedEstimator()
    meter.observe('',now=5)
    assert meter.estimate(now=10) is None
    meter.observe('字',now=20)
    assert meter.estimate(now=22) is None
