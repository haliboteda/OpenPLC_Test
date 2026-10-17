"""Feed answers into another script's stdin from a text file, so an operator can
answer hands-on y/n prompts without a console (run_examples.py reads whole lines
when stdin is not a terminal).

    python -u tools/stdin_feeder.py answers.txt | python -u tools/run_examples.py --only Relays

Append a line ("y" / "n") to answers.txt when a prompt appears; a line containing
QUIT ends the feeder.
"""
import sys
import time


def main():
    path = sys.argv[1]
    open(path, "a").close()
    pos = 0
    while True:
        with open(path) as f:
            f.seek(pos)
            data = f.read()
            pos = f.tell()
        if data:
            sys.stdout.write(data)
            sys.stdout.flush()
            if "QUIT" in data:
                return
        time.sleep(0.3)


if __name__ == "__main__":
    main()
