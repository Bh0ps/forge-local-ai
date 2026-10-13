"""Bounded, selected-model thread proposals; no engine configuration writes."""
import math
import statistics

REPETITIONS = 5
MAX_CANDIDATES = 2
MAX_REQUESTS = 36
MAX_SECONDS = 300
MIN_DECODE_GAIN = .05
MAX_NOISE = .10
SELECTION_KEYS = ('provider_id', 'model', 'context', 'thinking', 'tokens', 'temperature', 'num_thread', 'keep_alive')


def selected(settings):
    return {key: settings.get(key) for key in SELECTION_KEYS}


def thread_candidates(telemetry, baseline):
    cpu = telemetry.get('cpu', {})
    physical, logical = cpu.get('physical_cores'), cpu.get('logical_cores')
    if any(type(value) is not int or value < 1 for value in (physical, logical, baseline)):
        return []
    ceiling = min(64, logical)
    physical = min(physical, ceiling)
    values = [physical, ceiling, max(1, physical // 2)]
    return list(dict.fromkeys(value for value in values if value != baseline and 1 <= value <= ceiling))[:MAX_CANDIDATES]


def headroom_reason(telemetry, model):
    """Unknown GPU telemetry never becomes an assumption of CPU-only execution."""
    if telemetry.get('stale') or telemetry.get('probe_state') in ('failed', 'unavailable', 'refreshing'):
        return 'hardware_unknown'
    ram = telemetry.get('ram', {})
    available = ram.get('available_bytes')
    if available is None and all(type(ram.get(key)) in (int, float) for key in ('total_bytes', 'used_bytes')):
        available = ram['total_bytes'] - ram['used_bytes']
    size = model.get('size')
    if type(size) not in (int, float) or not math.isfinite(size) or size <= 0:
        return 'model_size_unknown'
    if type(available) not in (int, float) or not math.isfinite(available):
        return 'hardware_unknown'
    if available < max(1024 ** 3, size * .15):
        return 'ram_headroom_low'
    gpus = telemetry.get('gpus', [])
    if not gpus:
        return 'gpu_telemetry_unknown'
    for gpu in gpus:
        total, free = gpu.get('total_bytes'), gpu.get('free_bytes')
        if free is None and type(total) in (int, float) and type(gpu.get('used_bytes')) in (int, float):
            free = total - gpu['used_bytes']
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (total, free)):
            return 'gpu_telemetry_unknown'
        if free < max(256 * 1024 ** 2, total * .05):
            return 'gpu_headroom_low'
    return None


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def exact_sample(sample):
    return (sample.get('status') == 'passed' and sample.get('estimated') is False
            and sample.get('timing_provenance') == 'engine_reported'
            and all(_positive(sample.get(key)) for key in ('tps', 'decode_seconds', 'ttft_seconds'))
            and type(sample.get('prefill_seconds')) in (int,float) and math.isfinite(sample['prefill_seconds']) and sample['prefill_seconds']>=0
            and all(type(sample.get(key)) is int and sample[key] > 0 for key in ('input_tokens', 'output_tokens')))


def _median(samples, key):
    return statistics.median(sample[key] for sample in samples)


def _noisy(samples, key):
    median = _median(samples, key)
    if median==0:return any(sample[key]!=0 for sample in samples)
    values=[sample[key] for sample in samples]
    return (statistics.median(abs(value - median) for value in values) / median > MAX_NOISE
            or (max(values)-min(values))/median>.25)


def compare(baseline, candidate, controls):
    """Exact engine timings alone support speed claims; quality is probe scoped."""
    result = {'accepted': False, 'reason': 'incomplete_evidence', 'repetitions': len(candidate),
              'timing_provenance': 'engine_reported', 'minimum_decode_gain_fraction': MIN_DECODE_GAIN}
    if len(baseline) != REPETITIONS or len(candidate) != REPETITIONS:
        return result
    if not all(exact_sample(sample) for sample in baseline + candidate):
        return {**result, 'reason': 'quality_or_reported_timings_missing'}
    if any(left['input_tokens'] != right['input_tokens'] or
           abs(left['output_tokens'] - right['output_tokens']) > max(1, left['output_tokens'] * .20)
           for left, right in zip(baseline, candidate)):
        return {**result, 'reason': 'incomparable_samples'}
    if any(_noisy(samples, key) for samples in (baseline, candidate)
           for key in ('tps', 'ttft_seconds', 'prefill_seconds')):
        return {**result, 'reason': 'noisy_measurements'}
    baseline_tps, candidate_tps = _median(baseline, 'tps'), _median(candidate, 'tps')
    gain = candidate_tps / baseline_tps - 1
    result.update(baseline_median_tps=baseline_tps, candidate_median_tps=candidate_tps,
                  decode_gain_fraction=gain, baseline_median_ttft_seconds=_median(baseline, 'ttft_seconds'),
                  candidate_median_ttft_seconds=_median(candidate, 'ttft_seconds'),
                  baseline_median_prefill_seconds=_median(baseline, 'prefill_seconds'),
                  candidate_median_prefill_seconds=_median(candidate, 'prefill_seconds'),
                  candidate_median_total_seconds=_median(candidate, 'total_seconds'))
    if gain < MIN_DECODE_GAIN or sum(right['tps'] > left['tps'] for left, right in zip(baseline, candidate)) < 4:
        return {**result, 'reason': 'no_repeatable_decode_gain'}
    if any(_median(candidate, key) > _median(baseline, key) * 1.05 for key in ('ttft_seconds', 'prefill_seconds')):
        return {**result, 'reason': 'latency_regression'}
    if not controls or any(not exact_sample(left) or not exact_sample(right) or
                           left['input_tokens'] != right['input_tokens'] or
                           right['ttft_seconds'] > left['ttft_seconds'] * 1.05 or
                           right['prefill_seconds'] > left['prefill_seconds'] * 1.05
                           for left, right in controls):
        return {**result, 'reason': 'control_regression'}
    return {**result, 'accepted': True, 'reason': 'measured_gain'}
