"""Built-in fallback for the Elster reference table (no Qt, no file access).

Used when ELSTER_REFERENCE_JSON is missing, unreadable or incomplete, so that the GUI
starts and shows raw values. Format as in tests/data/ElsterTable.test.json (can_members, types).

Source: public upstream bullitt186/ha-stiebel-control (can_members.h).
"""
from __future__ import annotations

DEFAULT_OWN_CAN_ID = 0x680  # ESPCLIENT, own node ID

DEFAULT_CAN_MEMBERS: list[dict] = [
    {"name": "KESSEL", "enum_name": "cm_kessel", "can_id": "0x180",
     "read_id": ["0x31", "0x00"], "write_id": ["0x30", "0x00"]},
    {"name": "SPEICHER", "enum_name": "cm_speicher", "can_id": "0x201",
     "read_id": ["0x41", "0x01"], "write_id": ["0x40", "0x01"]},
    {"name": "ATEZ", "enum_name": "cm_atez", "can_id": "0x280",
     "read_id": ["0x51", "0x00"], "write_id": ["0x50", "0x00"]},
    {"name": "BEDIENMODUL_1", "enum_name": "cm_bedienmodul_1", "can_id": "0x300",
     "read_id": ["0x61", "0x00"], "write_id": ["0x60", "0x00"]},
    {"name": "BEDIENMODUL_2", "enum_name": "cm_bedienmodul_2", "can_id": "0x301",
     "read_id": ["0x61", "0x01"], "write_id": ["0x60", "0x01"]},
    {"name": "BEDIENMODUL_3", "enum_name": "cm_bedienmodul_3", "can_id": "0x302",
     "read_id": ["0x61", "0x02"], "write_id": ["0x60", "0x02"]},
    {"name": "BEDIENMODUL_4", "enum_name": "cm_bedienmodul_4", "can_id": "0x303",
     "read_id": ["0x61", "0x03"], "write_id": ["0x60", "0x03"]},
    {"name": "RAUMFERNFUEHLER", "enum_name": "cm_raumfernfuehler", "can_id": "0x400",
     "read_id": ["0x00", "0x00"], "write_id": ["0x00", "0x00"]},
    {"name": "MANAGER", "enum_name": "cm_manager", "can_id": "0x480",
     "read_id": ["0x91", "0x00"], "write_id": ["0x90", "0x00"]},
    {"name": "WPUMPE", "enum_name": "cm_wpumpe", "can_id": "0x514",
     "read_id": ["0xa1", "0x14"], "write_id": ["0xa0", "0x14"]},
    {"name": "HEIZMODUL", "enum_name": "cm_heizmodul", "can_id": "0x500",
     "read_id": ["0xa1", "0x00"], "write_id": ["0xa0", "0x00"]},
    {"name": "BUSKOPPLER", "enum_name": "cm_buskoppler", "can_id": "0x580",
     "read_id": ["0xb1", "0x00"], "write_id": ["0xb0", "0x00"]},
    {"name": "MISCHERMODUL_1", "enum_name": "cm_mischermodul_1", "can_id": "0x600",
     "read_id": ["0xc1", "0x00"], "write_id": ["0xc0", "0x00"]},
    {"name": "MISCHERMODUL_2", "enum_name": "cm_mischermodul_2", "can_id": "0x601",
     "read_id": ["0xc1", "0x01"], "write_id": ["0xc0", "0x01"]},
    {"name": "MISCHERMODUL_3", "enum_name": "cm_mischermodul_3", "can_id": "0x602",
     "read_id": ["0xc1", "0x02"], "write_id": ["0xc0", "0x02"]},
    {"name": "MISCHERMODUL_4", "enum_name": "cm_mischermodul_4", "can_id": "0x603",
     "read_id": ["0xc1", "0x03"], "write_id": ["0xc0", "0x03"]},
    {"name": "ESPCLIENT", "enum_name": "cm_espclient", "can_id": "0x680",
     "read_id": ["0xd1", "0x00"], "write_id": ["0xd0", "0x00"]},
    {"name": "FREMDGERAET", "enum_name": "cm_fremdgeraet", "can_id": "0x700",
     "read_id": ["0xe1", "0x00"], "write_id": ["0xe0", "0x00"]},
    {"name": "DCF_MODUL", "enum_name": "cm_dcf_modul", "can_id": "0x780",
     "read_id": ["0xf1", "0x00"], "write_id": ["0xf0", "0x00"]},
    {"name": "FES", "enum_name": "cm_neue_id", "can_id": "0x100",
     "read_id": ["0x21", "0x00"], "write_id": ["0x20", "0x00"]},
    {"name": "OTHER", "enum_name": "cm_other", "can_id": "0x000",
     "read_id": ["0x01", "0x00"], "write_id": ["0x00", "0x00"]},
]

DEFAULT_TYPES: list[dict] = [
    {"name": "et_default", "writable": True},
    {"name": "et_dec_val", "writable": True},
    {"name": "et_cent_val", "writable": True},
    {"name": "et_mil_val", "writable": True},
    {"name": "et_byte", "writable": True},
    {"name": "et_bool", "writable": True},
    {"name": "et_little_bool", "writable": True},
    {"name": "et_double_val", "writable": False},
    {"name": "et_triple_val", "writable": False},
    {"name": "et_little_endian", "writable": True},
    {"name": "et_betriebsart", "writable": True},
    {"name": "et_zeit", "writable": True},
    {"name": "et_datum", "writable": True},
    {"name": "et_time_domain", "writable": True},
    {"name": "et_dev_nr", "writable": False},
    {"name": "et_err_nr", "writable": False},
    {"name": "et_dev_id", "writable": False},
    {"name": "et_hours", "writable": False},
    {"name": "et_err_status", "writable": False},
]
