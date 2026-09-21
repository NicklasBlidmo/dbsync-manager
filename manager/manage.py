#!/usr/bin/env python
"""Django's command-line utility for dbsync-manager."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbsync_manager.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
