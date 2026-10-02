"""Executable Timeout Bot Fixture (Sleeps/Loops)."""

import sys
import time


def main():
    for line in sys.stdin:
        # Sleep for 5 seconds to trigger timeout
        time.sleep(5.0)
        sys.stdout.write('{"type": "PASS"}\n')
        sys.stdout.flush()


if __name__ == "__main__":
    main()
