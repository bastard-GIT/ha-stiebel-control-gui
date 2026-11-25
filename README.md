[Deutsch](docs/README.de.md)

# ha-stiebel-control-gui

PySide6 GUI for bus analysis and debug functions of the Stiebel Eltron WPL-A (Elster CAN). It talks over MQTT to the firmware from [ha-stiebel-control](https://github.com/bullitt186/ha-stiebel-control) (upstream bullitt186, topic root `heatingpump/`, see PR #138 and PR #141).

## 1. Purpose

- Monitor the CAN bus (raw frames, decoded using the Elster reference)
- Query individual Elster signals on demand
- Scan index ranges of a CAN member
- Search for participants on the bus
- Write with read-back (only after unlocking and confirmation)
- Export recordings as CSV or JSONL and reload them offline

## 2. Requirements

- Python 3 (Windows, Git Bash or PowerShell)
- Virtual environment in the repo folder:

  ```
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r gui/requirements.txt
  pip install pytest
  ```

- Dependencies per `gui/requirements.txt`: PySide6, paho-mqtt (1.6.x API)
- Reference table `ElsterTable.json` (see section 4)

## 3. Start

From the repo root folder:

```
python -I main.py
```

`-I` is mandatory so that modules from foreign folders are not loaded.

## 4. Credentials and reference

The MQTT credentials are stored in `secrets.yaml` in the repo root folder (not in the repo, never commit). Alternatively, the path can be set via the environment variable `STIEBEL_SECRETS`. Required keys:

- `mqtt_server`
- `mqtt_port`
- `mqtt_username`
- `mqtt_password`

The connection dialog is pre-filled with these values. The password is neither stored nor logged.

The reference table `ElsterTable.json` is read from the environment variable `ELSTER_REFERENCE_JSON`. Without this variable, the GUI looks under `../elster_reference/ElsterTable.json` relative to the repo.

The GUI also starts without an Elster table (missing or invalid ElsterTable.json): a note appears in the status bar, and values are shown raw (hex).

## 5. Functions (tabs)

- **Live** (`tab_live.py`): shows CAN frames from the ESP in real time with Elster decoding, filterable by CAN ID and type.
- **Query** (`tab_query.py`): reads or writes an Elster signal via MQTT and shows the history of results.
- **Scanner** (`tab_scanner.py`): queries an index range of a CAN member and shows the hit list with names from the reference.
- **Status** (`tab_status.py`): shows availability, MQTT connection and the firmware status (counters, ring buffer, TWAI errors).
- **Analysis** (`tab_analyse.py`): compares one index across several members and helps choose the type.
- **Index browser** (`tab_index_browser.py`): shows all indices of a measurement with a comparison against the reference.

## 6. Tests

```
python -I -m pytest tests
```

The tests run without a broker; the MQTT transport is replaced by fakes. They use a checked-in excerpt of the Elster table (`tests/data/ElsterTable.test.json`, LGPL-3.0, from ElsterTable.h); no private reference file is needed.

## 7. Language

The GUI is available in English (default) and German. Choose it in the menu **Language**; the choice is saved and applies after restarting the application. `STIEBEL_GUI_LANG=de` (or `en`) overrides the saved choice for one run.

Add a language: copy the key list with `python -I tools/i18n_check.py --keys`, translate the values into `gui/locales/<code>.json` (keep the `{placeholders}` unchanged), add the code to `LANGUAGE_NAMES` in `gui/i18n.py`, then check with `python -I tools/i18n_check.py` (missing, unused keys, placeholder mismatch). In code, user-visible text is wrapped as `_("English text")`; the English text is the key. Tests always run in English.

## 8. Note on debug mode

The debug firmware mode (topic prefix `stiebel-debug/`) is legacy. The goal is the upstream API (`heatingpump/…`, interface described in [docs/INTERFACES.md](docs/INTERFACES.md), section 13).

## 9. License

GPL-3.0, see [LICENSE](LICENSE).
