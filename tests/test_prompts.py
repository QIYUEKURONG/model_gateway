from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from model_gateway import (
    MaterialInput,
    MaterialSpec,
    PromptError,
    PromptSlot,
    PromptSpec,
    PromptStore,
    SlotKind,
    coerce_variables,
    split_slots,
    substitute_variables,
)


def test_split_and_substitute_do_not_recurse_or_touch_escapes():
    variables, externals = split_slots(
        [
            PromptSlot(name="city", kind=SlotKind.VARIABLE),
            PromptSlot(name="policy", kind=SlotKind.EXTERNAL),
        ]
    )
    assert [slot.name for slot in variables] == ["city"]
    assert [slot.name for slot in externals] == ["policy"]
    rendered = substitute_variables("go to {city} {{not}}", {"city": "{other}"})
    assert rendered == "go to {other} {not}"
    assert coerce_variables(SimpleNamespace(city="Paris")) == {"city": "Paris"}
    with pytest.raises(PromptError):
        substitute_variables("{missing}", {})


def test_prompt_versions_variables_and_external_materials(tmp_path):
    store = PromptStore(tmp_path / "prompts.db")
    spec = PromptSpec(
        key="trip.plan",
        role="system",
        body='Plan {city} for {nights} nights. Example: {{"city": "{city}"}}',
        slots=[
            PromptSlot(name="city", kind=SlotKind.VARIABLE, description="目的地"),
            PromptSlot(name="nights", kind=SlotKind.VARIABLE, required=False, default=2),
            PromptSlot(name="policy", kind=SlotKind.EXTERNAL, description="退改政策原文"),
        ],
        changelog="first",
    )
    first = store.publish(spec)
    assert first.version == 1
    assert first.status == "active"
    second = store.publish(spec.model_copy(update={"body": "Visit {city} for {nights} nights.", "changelog": "shorter"}))
    assert second.version == 2
    assert store.get("trip.plan").version == 2
    assert store.get("trip.plan", 1).status == "archived"
    assert [item.version for item in store.list_versions("trip.plan")] == [1, 2]

    draft = store.publish(
        PromptSpec(
            key="trip.plan",
            body="Draft {city} for {nights} nights.",
            slots=spec.slots,
            status="draft",
        )
    )
    assert draft.status == "draft"
    assert store.get("trip.plan").version == 2
    restored = store.set_status("trip.plan", 1, "active")
    assert restored.status == "active"
    assert store.get("trip.plan", 2).status == "archived"

    store.put_material(MaterialSpec(material_id="refund", title="退改", content="旧政策 {city}", source="handbook"))
    latest = store.put_material(MaterialSpec(material_id="refund", title="退改", content="新政策 {city}"))
    assert latest.version == 2
    assert store.get_material("refund").content.startswith("新政策")
    assert len(store.list_material_versions("refund")) == 2

    rendered = store.render(
        "trip.plan",
        version=1,
        variables=SimpleNamespace(city="Paris"),
        materials=[MaterialInput(slot="policy", material_id="refund", version=1)],
    )
    assert rendered.text == 'Plan Paris for 2 nights. Example: {"city": "Paris"}'
    assert rendered.materials[0].content == "旧政策 {city}"
    assert "旧政策 {city}" not in rendered.text
    assert "旧政策 {city}" in rendered.composed_text()
    assert rendered.variables_used["nights"] == "2"

    with pytest.raises(PromptError):
        store.render("trip.plan", version=1, variables={"city": "Paris", "policy": "nope"})
    with pytest.raises(PromptError):
        store.render("trip.plan", version=1, variables={})
    with pytest.raises(PromptError):
        store.publish(PromptSpec(key="bad", body="see {policy}", slots=[PromptSlot(name="policy", kind=SlotKind.EXTERNAL)]))
    with pytest.raises(ValidationError):
        PromptSpec(key="1bad", body="hello")
    store.close()
