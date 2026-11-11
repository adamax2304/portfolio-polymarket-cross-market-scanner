from dataclasses import dataclass

from polymarket_cross_scanner.storage import append_csv


@dataclass
class OldRow:
    a: int


@dataclass
class NewRow:
    a: int
    b: int


def test_append_csv_rotates_on_schema_change(tmp_path):
    path = tmp_path / "rows.csv"
    append_csv(path, [OldRow(a=1)])
    append_csv(path, [NewRow(a=2, b=3)])
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "a,b"
    assert "2,3" in text
    backups = list(tmp_path.glob("rows.schema_mismatch_*.csv.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").splitlines()[0] == "a"
