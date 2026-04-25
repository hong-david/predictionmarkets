import asyncio

from app.services.kalshi_ws import consume_market_data_forever


def main() -> None:
    asyncio.run(consume_market_data_forever())


if __name__ == "__main__":
    main()
