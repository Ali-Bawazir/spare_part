"""Apply a translation dict to the Arabic .po file using polib.

Usage:
    python docs/i18n/apply.py <translation_json>

JSON format:
{
  "<msgid>": "<msgstr>",
  ...
  "_plural": {
    "<msgid>": "<msgstr[0]>",     # used for all 6 plural forms
    ...
  }
}
"""
import json
import sys
from pathlib import Path

import polib

PO_PATH = Path('locale/ar/LC_MESSAGES/django.po')


def main():
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    plurals = data.pop('_plural', {}) if '_plural' in data else {}

    po = polib.pofile(str(PO_PATH))

    applied = 0
    plural_applied = 0
    for entry in po:
        if not entry.msgid:
            continue
        # Plural entry — polib uses integer keys (0, 1, 2, ...) for msgstr_plural
        if entry.msgid_plural:
            if entry.msgid in plurals:
                val = plurals[entry.msgid]
                if val and val.strip():
                    for k in entry.msgstr_plural:
                        entry.msgstr_plural[k] = val
                    plural_applied += 1
            continue
        # Singular entry
        if entry.msgid in data:
            new = data[entry.msgid]
            if new and new.strip():
                entry.msgstr = new
                applied += 1

    po.save(str(PO_PATH))
    print(f'Applied {applied} singular + {plural_applied} plural translations to {PO_PATH}')


if __name__ == '__main__':
    main()
