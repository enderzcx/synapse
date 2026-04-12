"""claude agent — port 9001, peer = codex@9002."""
from a2a_local.apps._server import run


def main() -> None:
    run(name="claude", port=9001, peer_url="http://127.0.0.1:9002/")


if __name__ == "__main__":
    main()
