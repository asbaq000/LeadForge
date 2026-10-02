#!/usr/bin/env python3
"""Start the point-and-click Lead Finder UI.

    python ui.py

Opens http://127.0.0.1:5000 in your browser. Everything the command line can
do is in there, plus a live view of leads as they are found.
"""

from webapp.app import main

if __name__ == "__main__":
    main()
