import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from permit_monitor import parse_epa_csv_payload, parse_epa_facility_payload


def test_parse_epa_facility_payload_uses_live_shape():
    payload = {
        "Results": {
            "Message": "Success",
            "QueryRows": "2",
            "QueryID": "999",
            "Facilities": [
                {
                    "RegistryID": "110071141730",
                    "FacName": "Example Facility",
                    "Owner": "Example Owner",
                    "State": "TX",
                    "PermitStatus": "Active",
                    "EmissionUnitDesc": "LM6000 turbine",
                    "CapacityMW": "123.4",
                    "IssueDate": "2024-01-01",
                    "ExpirationDate": "2029-01-01",
                    "LastActionDate": "2024-02-01",
                }
            ],
        }
    }

    records = parse_epa_facility_payload(payload)

    assert len(records) == 1
    record = records[0]
    assert record.permit_id == "110071141730"
    assert record.facility_name == "Example Facility"
    assert record.owner_entity == "Example Owner"
    assert record.state == "TX"
    assert record.status == "ACTIVE"
    assert record.equipment_desc == "LM6000 turbine"
    assert record.capacity_mw == 123.4
    assert record.issue_date == "2024-01-01"


def test_parse_epa_csv_payload_uses_registry_id_and_name():
    csv_text = '"AIRName","SourceID"\n"Example Facility","110071141730"\n'

    records = parse_epa_csv_payload(csv_text)

    assert len(records) == 1
    record = records[0]
    assert record.permit_id == "110071141730"
    assert record.facility_name == "Example Facility"
