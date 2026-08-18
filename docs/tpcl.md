# TPCL-Schreibpfade – technische Notiz

Kurzreferenz für die Schreibpfade des Tools (TPCL-Grundparameter, Feinabgleich,
TPCL-General, Emulation, Reset) mit den verifizierten Byteformaten und offenen
Punkten. Offline-Abdeckung: `tests/test_tpcl_review.py`.

## Byteformate

Alle TPCL-Befehle nutzen den Rahmen `ESC + ASCII-Kommando + LF + NUL` (`frame`).

| Pfad | Bytes |
|---|---|
| Grundparameter | `ESC Z2; 1, <24 Zeichen> LF NUL` – 16 Einzelzeichen-Felder, dann 2-stelliger Hex-Euro-Code, dann 6 Einzelzeichen-Felder; vom B-FV4 ignorierte Felder sind fest auf `0` gepinnt; `requires_reset` |
| Feinabgleich | `ESC Z2; 2, <33 Zeichen> LF NUL` – X-Koordinate in Zehntel-mm mit Vorzeichen und 3 Ziffern, Wertebereich 0–995 |
| Transport-Umschlag | `ESC ESC setnvrr|setnvrs <Anzahl> CR LF <Body>`; optional mit `ESC Arg`-Präfix und `ESC ESC exit CR LF`-Trailer |
| TPCL-General | `ESC Arg` + `setnvrr <n>`-Umschlag + Items `code,länge,value;` in fester Reihenfolge (20, 21, 22, 23, 24, 25, 26, 190, 27, 28, 29, 30, 32, 2000, 2002, 2003, 3000) + `ESC ESC reboot 1 CR LF` + `ESC ESC exit CR LF`; `länge` = ASCII-Bytelänge des Werts |
| Emulation (benannt) | `setnvrs 2` + `31,2,<wert>;33,1,0;` mit D=65, E=66, I=73, Z=90, TPCL=69 |
| Emulation (AUTO) | aktuell `setnvrs 1` + `33,1,1;` (AUTO) bzw. `33,1,2;` (AUTO2) – siehe offener Punkt 1 |
| Reset | `ESC Z0 LF NUL` bzw. `ESC WR LF NUL`; `ESC ESC reboot 0|1|3 CR LF`, `facreset 0`, `resetcommand 0`, `selftest 0` |

## Sicherheitslogik

Alle Builder erzeugen ausschließlich `CommandPreview`-Objekte. Gesendet wird nur
in `apply_previews`, und nur wenn `--apply` **und** `--yes` gemeinsam gesetzt
sind. `--apply` ohne `--yes` bricht vor jedem Socket-Schreibvorgang ab
(`writes require both --apply and --yes`); ohne Schalter erscheint nur die
exakte Hex-/ASCII-Vorschau. Die Tests prüfen beides offline gegen einen
netzwerkfreien Stub.

## Offene Punkte

1. **Emulation AUTO/AUTO2:** Die Prozess-Dokumentation führt zusätzlich die
   Selektorwerte `AUTO=48` und `AUTO2=85`. Der Builder sendet für AUTO/AUTO2
   derzeit nur das Code-33-Item (`33,1,1;`/`33,1,2;`) und kein Code-31-Item.
   Ob ein zusätzliches `31,2,48;`/`31,2,85;` erforderlich ist, ist am einzelnen
   Testgerät zu klären, bevor ein Apply erfolgt. Die Tests pinnen bis dahin den
   aktuellen Stand.
2. **`single media-calibration <value>`:** der Wert wird ungeprüft in
   `ESC ESC sc <value> CR LF` übernommen (anders als `ribbon-calibration`, das
   Ziffern erzwingt). Werte mit Steuerzeichen würden die Preview-Bytes formen;
   Preview vor jedem Apply genau lesen.
3. **TPCL-General-Werte:** ASCII sowie NUL/CR/LF werden erzwungen, `;` und `,`
   im Wert sind jedoch zulässig und machen das `code,länge,value;`-Framing für
   delimiter-basierte Parser mehrdeutig. Bis zur Klärung am Testgerät nur
   einfache Dezimalwerte verwenden.
4. **`dangerous`-Flag:** rein informativ. Die Zweitore-Sperre (`--apply` +
   `--yes`) greift unabhängig davon; `Z2;1`/`Z2;2` sind z. B. nicht als
   `dangerous` markiert, werden aber genauso nur mit beiden Schaltern gesendet.
