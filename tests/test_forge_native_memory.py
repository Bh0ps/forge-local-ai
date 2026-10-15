import pytest
from forge_native_memory import strata_memory_plan,GIB,ARENA_BYTES


def test_memory_plan_keeps_full_arena_only_when_it_fits():
    assert strata_memory_plan(28*GIB,15*GIB,8192)=={'mode':'arena','prepared_expert_bytes':0}


def test_memory_plan_uses_resident_remainder_with_room_for_other_apps():
    plan=strata_memory_plan(20*GIB,15*GIB,8192)
    assert plan=={'mode':'resident','prepared_expert_bytes':ARENA_BYTES}


def test_limited_ram_uses_supported_mmap_experts():
    assert strata_memory_plan(9*GIB,15*GIB,8192)['mode']=='mmap'


@pytest.mark.parametrize('ram,vram',[(5*GIB,15*GIB),(20*GIB,8*GIB)])
def test_insufficient_resources_are_not_guessed(ram,vram):
    with pytest.raises(ValueError):strata_memory_plan(ram,vram,8192)
