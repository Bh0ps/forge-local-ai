"""Conservative Strata Coder placement using upstream expert-arena/cache geometry."""
GIB=1024**3
ARENA_BYTES=23_400_000_000


def strata_memory_plan(available_ram,free_vram,context):
    if available_ram<6*GIB or free_vram<10*GIB:
        raise ValueError('Insufficient free memory for Strata Coder.')
    if available_ram>=26*GIB:
        return {'mode':'arena','prepared_expert_bytes':0}
    # Upstream reserves roughly 5 GB for dense weights/buffers at 32K, with
    # extra KV space above 32K. The engine determines the actual cache size.
    longer=max(0,context-32768)*13*1056
    cached=max(0,min(ARENA_BYTES,free_vram-5_000_000_000-longer))
    rest=ARENA_BYTES-cached
    mode='resident' if available_ram>=rest+5*GIB else 'mmap'
    return {'mode':mode,'prepared_expert_bytes':ARENA_BYTES}
