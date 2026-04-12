"""codex agent — port 9002, peer = claude@9001."""
from warroom.apps._server import run


def main() -> None:
    run(name="codex", port=9002, peer_url="http://127.0.0.1:9001/")


if __name__ == "__main__":
    main()
