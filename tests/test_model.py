from __future__ import annotations

from formextract.model import (
    BBox,
    ControlType,
    Field,
    FieldAmbiguity,
    InstanceRecord,
    ReviewReason,
    SourceInfo,
    from_dict,
    from_json,
    to_dict,
    to_json,
)


def _source(**overrides) -> SourceInfo:
    base = {
        "content_hash": "c",
        "original_filename": "a.xlsx",
        "mime": "xlsx",
        "size": 1,
    }
    base.update(overrides)
    return SourceInfo(**base)


def test_v01_record_loads_with_defaults():
    old = {
        "instance_id": "i1",
        "schema_version": "1",
        "pipeline_version": "1",
        "run_id": "r1",
        "created_at": "2024-01-01T00:00:00Z",
        "source": {
            "content_hash": "c",
            "original_filename": "a.pdf",
            "mime": "pdf",
            "size": 1,
        },
        "fields": [
            {
                "label_text": "L",
                "control_type": "text",
                "bbox": {"page": 0, "x0": 0, "y0": 0, "x1": 1, "y1": 1},
            }
        ],
        "idempotency_key": "k",
    }
    rec = from_dict(InstanceRecord, old)
    assert rec.schema_version == "1"
    assert rec.hidden_sheets == []
    assert rec.source.sheet_state == {}
    field = rec.fields[0]
    assert field.tab is None
    assert field.source_elements == []
    assert field.field_id is None
    assert field.section_path == []
    assert field.ambiguity is None
    assert field.unresolved_source_refs == []


def test_record_roundtrip_preserves_new_schema_fields():
    field = Field(
        label_text="Which US states do they ship to",
        control_type=ControlType.MULTI_SELECT,
        bbox=BBox(page=0, x0=0, y0=1, x1=2, y1=3),
        tab="Checklist",
        source_elements=["e1", "e2"],
        field_id="f-abc123",
        section_path=["vendor compliance checklist"],
        ambiguity=FieldAmbiguity(
            marker_element_id="m1",
            candidate_element_ids=["c1", "c2"],
            reason=ReviewReason.AMBIGUOUS_MARK,
        ),
        unresolved_source_refs=["p0:c1:field-row:x_all:0:1"],
    )
    rec = InstanceRecord(
        instance_id="i1",
        schema_version="2",
        pipeline_version="1",
        run_id="r1",
        created_at="2024-01-01T00:00:00Z",
        source=_source(sheet_state={"S1": "hidden"}),
        fields=[field],
        hidden_sheets=["S1"],
    )

    data = to_dict(rec)
    assert data["schema_version"] == "2"
    assert data["hidden_sheets"] == ["S1"]
    assert data["source"]["sheet_state"] == {"S1": "hidden"}
    encoded_field = data["fields"][0]
    assert encoded_field["tab"] == "Checklist"
    assert encoded_field["source_elements"] == ["e1", "e2"]
    assert encoded_field["field_id"] == "f-abc123"
    assert encoded_field["section_path"] == ["vendor compliance checklist"]
    assert encoded_field["ambiguity"]["reason"] == "ambiguous_mark"
    assert encoded_field["unresolved_source_refs"] == ["p0:c1:field-row:x_all:0:1"]

    assert from_json(InstanceRecord, to_json(rec)) == rec


def test_decode_drops_unknown_keys():
    data = {
        "instance_id": "i1",
        "schema_version": "1",
        "pipeline_version": "1",
        "run_id": "r1",
        "created_at": "2024-01-01T00:00:00Z",
        "source": {
            "content_hash": "c",
            "original_filename": "a.pdf",
            "mime": "pdf",
            "size": 1,
            "bogus": 123,
        },
        "bogus_top": True,
    }
    rec = from_dict(InstanceRecord, data)
    assert rec.instance_id == "i1"
    assert not hasattr(rec, "bogus_top")
    assert not hasattr(rec.source, "bogus")
