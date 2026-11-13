"""FlowDesk: Finam Trade API is the only market data provider."""
from flowdesk.finam_gui import run


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--check-finam-runtime":
        from flowdesk.runtime_probe import check_finam_runtime
        raise SystemExit(check_finam_runtime(sys.argv[2]))
    raise SystemExit(run())
