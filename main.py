"""Start the Stiebel bus analysis GUI.

    python -I main.py

Sets the language (saved choice or $STIEBEL_GUI_LANG) before the tab modules are imported and opens the main window.
"""
import os
import sys

# With -I the script directory is not in sys.path: add the modules from gui/ explicitly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui"))

import i18n  # noqa: E402

i18n.init_from_settings()

from elster_gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
