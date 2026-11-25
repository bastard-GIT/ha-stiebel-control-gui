"""Tests for gui/reference_model.py (no Qt).

The original referenzen/elster_reference/ElsterTable.json is never written.
Write tests work on a copy in tmp_path. An autouse fixture checks
the SHA256 of the original before and after each test.
"""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parents[1] / "gui"
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

import pytest  # noqa: E402

from elster import DEFAULT_JSON  # noqa: E402
from reference_model import ReferenceError, ReferenceModel  # noqa: E402


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def original_unveraendert():
    before = _sha(DEFAULT_JSON)
    yield
    assert _sha(DEFAULT_JSON) == before, "Original ElsterTable.json was modified"


@pytest.fixture
def copy_path(tmp_path):
    dst = tmp_path / "ElsterTable.json"
    shutil.copy(DEFAULT_JSON, dst)
    return dst


@pytest.fixture
def model(copy_path):
    return ReferenceModel(str(copy_path))


def _free_index(model) -> int:
    used = model.indices()
    for i in range(0x0FFF, 0x0000, -1):
        if i not in used:
            return i
    raise AssertionError("no free index")


# ---- Laden ----

def test_standard_laedt_testtabelle():
    # tests/conftest.py sets ELSTER_REFERENCE_JSON to the checked-in excerpt
    # tests/data/ElsterTable.test.json (4 signals, 3 real indices + sentinel 0x0000).
    m = ReferenceModel()
    assert len(m.data["signals"]) == 4
    assert len(m.indices()) == 3  # sentinel INDEX_NOT_FOUND (0x0000) not counted
    assert m.dirty is False
    assert m.member_name(0x180) == "KESSEL"


def test_ohne_signals_schluessel_wirft_fehler(tmp_path):
    p = tmp_path / "kaputt.json"
    p.write_text(json.dumps({"types": []}), encoding="utf-8")
    with pytest.raises(ReferenceError):
        ReferenceModel(str(p))


# ---- upsert: neu ----

def test_upsert_neues_signal_setzt_felder_und_dirty(model):
    idx = _free_index(model)
    before = len(model.data["signals"])
    sig = model.upsert_signal(idx, "TEST_NEU", "et_default", 0x180, "Testkommentar")

    assert model.dirty is True
    assert len(model.data["signals"]) == before + 1
    assert model.signal(idx) is sig
    assert sig["name"] == "TEST_NEU"
    assert sig["index"] == f"0x{idx:04x}"
    assert sig["index_int"] == idx
    assert sig["type"] == "et_default"
    assert sig["owner"] == "0x180"
    assert sig["owner_name"] == "KESSEL"
    assert sig["poll"] == "pl_off"
    assert sig["comment"].startswith("Testkommentar | ")
    assert "GUI-Auswertung" in sig["comment"]


def test_upsert_neues_signal_ohne_owner(model):
    idx = _free_index(model)
    sig = model.upsert_signal(idx, "OHNE_OWNER", "et_default", None)
    assert sig["owner"] is None
    assert sig["owner_name"] is None
    assert "GUI-Auswertung" in sig["comment"]


def test_upsert_name_wird_getrimmt(model):
    idx = _free_index(model)
    sig = model.upsert_signal(idx, "  MIT_LEER  ", "et_default", None)
    assert sig["name"] == "MIT_LEER"


# ---- upsert: aendern ----

def test_upsert_aendern_setzt_dirty_und_felder(model):
    idx = sorted(model.indices())[0]
    old = model.signal(idx)
    old_poll = old["poll"]
    old_index = old["index"]
    before = len(model.data["signals"])

    sig = model.upsert_signal(idx, "GEAENDERT", model.types[1], 0x180, "neu")

    assert model.dirty is True
    assert len(model.data["signals"]) == before  # no new element
    assert model.signal(idx) is sig
    assert sig["name"] == "GEAENDERT"
    assert sig["type"] == model.types[1]
    assert sig["owner"] == "0x180"
    assert sig["owner_name"] == "KESSEL"
    assert sig["comment"].startswith("neu | ")
    assert "GUI-Auswertung" in sig["comment"]
    # index/index_int/poll are not changed on update
    assert sig["index"] == old_index
    assert sig["index_int"] == idx
    assert sig["poll"] == old_poll


def test_upsert_aendern_owner_none_und_ohne_kommentar(model):
    idx = sorted(model.indices())[0]
    sig = model.upsert_signal(idx, model.signal(idx)["name"], "et_default", None)
    assert sig["owner"] is None
    assert sig["owner_name"] is None
    assert sig["comment"].startswith("GUI-Auswertung")


def test_upsert_gleicher_name_gleicher_index_ist_erlaubt(model):
    idx = sorted(model.indices())[0]
    name = model.signal(idx)["name"]
    sig = model.upsert_signal(idx, name, "et_default", None)
    assert sig["name"] == name


# ---- Errors ----

def test_namenskollision_mit_anderem_index(model):
    idx = _free_index(model)
    other = sorted(model.indices())[0]
    other_name = model.signal(other)["name"]
    n_before = len(model.data["signals"])

    with pytest.raises(ReferenceError):
        model.upsert_signal(idx, other_name, "et_default", None)
    assert model.dirty is False
    assert len(model.data["signals"]) == n_before
    assert model.signal(idx) is None


def test_unbekannter_typ_wirft_fehler(model):
    idx = _free_index(model)
    with pytest.raises(ReferenceError):
        model.upsert_signal(idx, "TYP_FEHLT", "et_gibt_es_nicht", None)
    assert model.dirty is False
    assert model.signal(idx) is None


@pytest.mark.parametrize("name", ["", "   "])
def test_leerer_name_wirft_fehler(model, name):
    idx = _free_index(model)
    with pytest.raises(ReferenceError):
        model.upsert_signal(idx, name, "et_default", None)
    assert model.dirty is False


# ---- save_as ----

def test_save_as_schreibt_gueltiges_json_und_reload_zeigt_neuen_eintrag(model, tmp_path):
    original = copy.deepcopy(model.data)
    idx = _free_index(model)
    model.upsert_signal(idx, "TEST_SPEICHERN", "et_default", 0x180, "save")
    assert model.dirty is True

    target = tmp_path / "neu.json"
    model.save_as(str(target))

    assert model.dirty is False
    written = json.loads(target.read_text(encoding="utf-8"))  # gueltiges JSON
    assert written == model.data

    # remaining signals identical to the original, only the new entry is added
    assert written["signals"][:-1] == original["signals"]
    assert written["signals"][-1]["index_int"] == idx
    assert written["types"] == original["types"]
    assert written["can_members"] == original["can_members"]

    reloaded = ReferenceModel(str(target))
    assert reloaded.dirty is False
    assert reloaded.signal(idx)["name"] == "TEST_SPEICHERN"
    assert reloaded.indices() == model.indices()


def test_save_as_auf_geladene_datei_wirft_fehler(model, copy_path):
    idx = _free_index(model)
    model.upsert_signal(idx, "NICHT_SPEICHERN", "et_default", None)
    before = _sha(copy_path)

    with pytest.raises(ReferenceError):
        model.save_as(str(copy_path))

    assert _sha(copy_path) == before  # file unchanged
    assert model.dirty is True
