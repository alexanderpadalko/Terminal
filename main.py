import sys


def main():
    if len(sys.argv)>1 and sys.argv[1]=='--diagnose-stream':
        from flowdesk.stream_diagnostics import main as diagnose
        return diagnose(sys.argv[2:])
    from flowdesk.gui import run
    return run()

if __name__ == '__main__':
    raise SystemExit(main())
