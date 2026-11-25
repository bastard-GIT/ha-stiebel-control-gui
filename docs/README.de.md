[English](../README.md)

# ha-stiebel-control-gui

PySide6-Oberfläche für die Bus-Analyse und Debug-Funktionen der Stiebel Eltron WPL-A (Elster-CAN). Sie spricht per MQTT mit der Firmware aus [ha-stiebel-control](https://github.com/bullitt186/ha-stiebel-control) (Upstream bullitt186, Topic-Wurzel `heatingpump/`, siehe PR #138 und PR #141).

## 1. Zweck

- CAN-Bus mitlesen (Rohframes, dekodiert anhand der Elster-Referenz)
- Einzelne Elster-Signale gezielt abfragen
- Index-Bereiche eines CAN-Members scannen
- Teilnehmer auf dem Bus suchen
- Schreiben mit Rücklesen (nur nach Freischalten und Bestätigung)
- Mitschnitte als CSV oder JSONL exportieren und offline wieder laden

## 2. Voraussetzungen

- Python 3 (Windows, Git-Bash oder PowerShell)
- Virtuelle Umgebung im Repo-Ordner:

  ```
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r gui/requirements.txt
  pip install pytest
  ```

- Abhängigkeiten laut `gui/requirements.txt`: PySide6, paho-mqtt (1.6.x-API)
- Referenztabelle `ElsterTable.json` (siehe Abschnitt 4)

## 3. Start

Aus dem Repo-Wurzelordner:

```
python -I main.py
```

`-I` ist Pflicht, damit Module aus fremden Ordnern nicht geladen werden.

## 4. Zugangsdaten und Referenz

Die MQTT-Zugangsdaten stehen in `secrets.yaml` im Repo-Wurzelordner (nicht im Repo, nie committen). Alternativ Pfad über die Umgebungsvariable `STIEBEL_SECRETS`. Benötigte Schlüssel:

- `mqtt_server`
- `mqtt_port`
- `mqtt_username`
- `mqtt_password`

Der Verbindungsdialog ist mit diesen Werten vorbelegt. Das Passwort wird nicht gespeichert und nicht geloggt.

Die Referenztabelle `ElsterTable.json` wird aus der Umgebungsvariable `ELSTER_REFERENCE_JSON` gelesen. Ohne diese Variable sucht die GUI unter `../elster_reference/ElsterTable.json` relativ zum Repo.

## 5. Funktionen (Tabs)

- **Live** (`tab_live.py`): zeigt CAN-Frames vom ESP in Echtzeit mit Elster-Dekodierung, filterbar nach CAN-ID und Art.
- **Abfrage** (`tab_query.py`): liest oder schreibt ein Elster-Signal per MQTT und zeigt den Verlauf der Ergebnisse.
- **Scanner** (`tab_scanner.py`): fragt einen Index-Bereich eines CAN-Members ab und zeigt die Trefferliste mit Namen aus der Referenz.
- **Status** (`tab_status.py`): zeigt Verfügbarkeit, MQTT-Verbindung und den Status der Firmware (Zähler, Ringpuffer, TWAI-Fehler).
- **Analyse** (`tab_analyse.py`): vergleicht einen Index über mehrere Members und hilft bei der Typwahl.
- **Index-Browser** (`tab_index_browser.py`): zeigt alle Indizes einer Messung mit Abgleich gegen die Referenz.

## 6. Tests

```
python -I -m pytest tests
```

Die Tests laufen ohne Broker; der MQTT-Transport wird durch Fakes ersetzt. Sie nutzen einen eingecheckten Auszug der Elster-Tabelle (`tests/data/ElsterTable.test.json`, LGPL-3.0, aus ElsterTable.h); eine private Referenzdatei ist nicht nötig.

## 7. Sprache

Die GUI gibt es auf Englisch (Standard) und Deutsch. Auswahl im Menü **Sprache**; die Wahl wird gespeichert und gilt nach einem Neustart der Anwendung. `STIEBEL_GUI_LANG=de` (oder `en`) überschreibt die gespeicherte Wahl für einen Lauf.

Neue Sprache: Schlüsselliste mit `python -I tools/i18n_check.py --keys` ausgeben, Werte in `gui/locales/<code>.json` übersetzen (`{Platzhalter}` unverändert lassen), Kürzel in `LANGUAGE_NAMES` in `gui/i18n.py` eintragen, dann mit `python -I tools/i18n_check.py` prüfen (fehlende und unbenutzte Schlüssel, Platzhalter). Im Code stehen sichtbare Texte als `_("English text")`; der englische Text ist der Schlüssel. Tests laufen immer auf Englisch.

## 8. Hinweis zum Debug-Modus

Der Debug-Firmware-Modus (Topic-Präfix `stiebel-debug/`) ist eine Altlast. Ziel ist die Upstream-API (`heatingpump/…`, Schnittstelle siehe [docs/INTERFACES.md](INTERFACES.md) (englisch), Abschnitt 13).

## 9. Lizenz

GPL-3.0, siehe [LICENSE](../LICENSE).
