from pathlib import Path
from state import load_seen_ids, save_seen_ids

def test_load_seen_ids_empty_when_no_file(tmp_path):
    result = load_seen_ids(tmp_path / "state.json")
    assert result == set()

def test_save_and_reload(tmp_path):
    path = tmp_path / "state.json"
    ids = {"abc123", "def456"}
    save_seen_ids(ids, path)
    assert load_seen_ids(path) == ids

def test_save_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    save_seen_ids({"id1"}, path)
    save_seen_ids({"id2"}, path)
    assert load_seen_ids(path) == {"id2"}
