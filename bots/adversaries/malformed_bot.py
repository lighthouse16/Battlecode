"""Executable Malformed Output Bot Fixture."""
import sys

def main():
    for line in sys.stdin:
        # Write non-JSON garbage on stdout
        sys.stdout.write("PROTOCOL_VIOLATION_NOT_JSON_DATA\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
