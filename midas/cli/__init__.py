"""Arguments, signals, stop IPC, output routing and exit handling."""


def main(argv=None, **options):
    from .service import main
    return main(argv, **options)
