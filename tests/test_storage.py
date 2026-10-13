from ml_repo_sampler import storage as st


def test_append_upgrades_header_when_schema_grows(tmp_path):
    path = tmp_path / "m.csv"
    st.append_rows(path, ["full_name", "stars"], [{"full_name": "a/b", "stars": 3}])
    st.append_rows(path, ["full_name", "stars", "has_x"], [{"full_name": "c/d", "stars": 1, "has_x": True}])
    rows = st.read_rows(path, {"full_name": str, "stars": int, "has_x": bool, "topics": list})
    assert rows == [
        {"full_name": "a/b", "stars": 3, "has_x": None, "topics": []},
        {"full_name": "c/d", "stars": 1, "has_x": True, "topics": []},
    ]
