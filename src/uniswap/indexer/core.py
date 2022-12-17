from decimal import Decimal
from apibara import EventFilter, Info
from apibara.model import BlockHeader, StarkNetEvent
from bson import Decimal128
from structlog import get_logger

from uniswap.indexer.abi import (
    decode_event,
    sync_decoder,
    swap_decoder,
    transfer_decoder,
    mint_decoder,
    burn_decoder,
)
from uniswap.indexer.context import IndexerContext
from uniswap.indexer.helpers import (
    create_liquidity_position,
    create_token,
    create_transaction,
    felt,
    fetch_token_balance,
    price,
    to_decimal,
)
from uniswap.indexer.jediswap import (
    find_eth_per_token,
    get_tracked_liquidity_usd,
    jediswap_factory,
)


logger = get_logger(__name__)


async def handle_transfer(
    info: Info[IndexerContext], header: BlockHeader, event: StarkNetEvent
):
    transfer = decode_event(transfer_decoder, event.data)
    pair_address = int.from_bytes(event.address, "big")
    logger.info("handle Transfer", **transfer._asdict())
    if transfer.from_ == 0 and transfer.to == 1 and transfer.value == 1000:
        return

    value = to_decimal(transfer.value, 18)
    await create_transaction(info, event.transaction_hash)

    # mints
    mints = await info.storage.find(
        "mints",
        {
            "pair_id": felt(pair_address),
            "transaction_hash": event.transaction_hash,
        },
    )
    mints = list(mints)

    if transfer.from_ == 0:
        logger.info("transfer is a mint")

        # update total supply
        await info.storage.find_one_and_update(
            "pairs",
            {"id": felt(pair_address)},
            {"$inc": {"total_supply": Decimal128(value)}},
        )

        # create new mint if no mints so far or if last one is done already
        if not mints or _is_complete_mint(mints[-1]):
            mint = {
                "transaction_hash": event.transaction_hash,
                "index": len(mints),
                "pair_id": felt(pair_address),
                "to": felt(transfer.to),
                "liquidity": Decimal128(value),
                "timestamp": info.context.block_timestamp,
            }
            await info.storage.insert_one("mints", mint)

    if transfer.to == pair_address:
        logger.info("transfer is burn (direct)")
        # send directly to pair
        burns = await info.storage.find(
            "burns",
            {
                "pair_id": felt(pair_address),
                "transaction_hash": event.transaction_hash,
            },
        )
        burns = list(burns)

        burn = {
            "transaction_hash": event.transaction_hash,
            "index": len(burns),
            "pair_id": felt(pair_address),
            "to": felt(transfer.to),
            "liquidity": Decimal128(value),
            "timestamp": info.context.block_timestamp,
            "needs_complete": True,
        }
        await info.storage.insert_one("burns", burn)

    # burns
    if transfer.to == 0 and transfer.from_ == pair_address:
        logger.info("transfer is a burn")

        # update total supply
        await info.storage.find_one_and_update(
            "pairs",
            {"id": felt(pair_address)},
            {"$inc": {"total_supply": Decimal128(-value)}},
        )

        burns = await info.storage.find(
            "burns",
            {
                "pair_id": felt(pair_address),
                "transaction_hash": event.transaction_hash,
            },
        )
        burns = list(burns)

        burn = None
        if burns:
            current_burn = burns[-1]
            # continue previous burn
            if current_burn["needs_complete"]:
                burn = current_burn

        if burn is None:
            burn = {
                "transaction_hash": event.transaction_hash,
                "index": len(burns),
                "pair_id": felt(pair_address),
                "to": felt(transfer.to),
                "liquidity": Decimal128(value),
                "timestamp": info.context.block_timestamp,
                "needs_complete": False,
            }

        # if this logical burn included a fee mint, account for this
        if mints and not _is_complete_mint(mints[-1]):
            mint = mints[-1]
            burn["fee_to"] = mint["to"]
            burn["fee_liquidity"] = mint["liquidity"]
            # remove logical mint
            await info.storage.delete_one(
                "mints",
                {"transaction_hash": mint["transaction_hash"], "index": mint["index"]},
            )

        del burn["_id"]
        if burn["needs_complete"]:
            # replace existing burn
            await info.storage.find_one_and_replace(
                "burns",
                {
                    "transaction_hash": burn["transaction_hash"],
                    "index": burn["index"],
                },
                burn,
            )
        else:
            await info.storage.insert_one("burns", burn)

    if transfer.from_ != 0 and transfer.from_ != pair_address:
        from_user_balance = await fetch_token_balance(
            info, pair_address, transfer.from_
        )
        from_user_balance = to_decimal(from_user_balance, 18)
        logger.debug("from user balance", balance=from_user_balance)
        await info.storage.find_one_and_replace(
            "liquidity_positions",
            {
                "pair_address": felt(pair_address),
                "user": felt(transfer.from_),
            },
            {
                "pair_address": felt(pair_address),
                "user": felt(transfer.from_),
                "liquidity_token_balance": Decimal128(from_user_balance),
            },
            upsert=True,
        )

    if transfer.to != 0 and transfer.to != pair_address:
        to_user_balance = await fetch_token_balance(info, pair_address, transfer.to)
        to_user_balance = to_decimal(to_user_balance, 18)
        logger.debug("to user balance", balance=to_user_balance)
        await info.storage.find_one_and_replace(
            "liquidity_positions",
            {
                "pair_address": felt(pair_address),
                "user": felt(transfer.to),
            },
            {
                "pair_address": felt(pair_address),
                "user": felt(transfer.to),
                "liquidity_token_balance": Decimal128(to_user_balance),
            },
            upsert=True,
        )


async def handle_sync(
    info: Info[IndexerContext], header: BlockHeader, event: StarkNetEvent
):
    sync = decode_event(sync_decoder, event.data)
    pair_address = int.from_bytes(event.address, "big")
    logger.info("handle Sync", **sync._asdict())

    pair = await info.storage.find_one("pairs", {"id": felt(pair_address)})
    assert pair is not None

    token0 = await info.storage.find_one("tokens", {"id": pair["token0_id"]})
    assert token0 is not None

    token1 = await info.storage.find_one("tokens", {"id": pair["token1_id"]})
    assert token1 is not None

    reserve0 = to_decimal(sync.reserve0, token0["decimals"])
    reserve1 = to_decimal(sync.reserve1, token1["decimals"])

    token0_price = price(reserve0, reserve1)
    token1_price = price(reserve1, reserve0)

    logger.info(
        "new reserves and price",
        reserve0=reserve0,
        reserve1=reserve1,
        price0=token0_price,
        price1=token1_price,
    )

    old_pair = await info.storage.find_one_and_update(
        "pairs",
        {
            "id": felt(pair_address),
        },
        {
            "$set": {
                "reserve0": Decimal128(reserve0),
                "reserve1": Decimal128(reserve1),
                "token0_price": Decimal128(token0_price),
                "token1_price": Decimal128(token1_price),
            }
        },
    )

    token0_liquidity = (
        token0["total_liquidity"].to_decimal()
        - old_pair["reserve0"].to_decimal()
        + reserve0
    )
    token1_liquidity = (
        token1["total_liquidity"].to_decimal()
        - old_pair["reserve1"].to_decimal()
        + reserve1
    )

    await info.storage.find_one_and_update(
        "tokens",
        {"id": pair["token0_id"]},
        {"$set": {"total_liquidity": Decimal128(token0_liquidity)}},
    )

    await info.storage.find_one_and_update(
        "tokens",
        {"id": pair["token1_id"]},
        {"$set": {"total_liquidity": Decimal128(token1_liquidity)}},
    )

    logger.info("fetch prices", token0=token0["symbol"], token1=token1["symbol"])
    token0_derived_eth = await find_eth_per_token(info, token0["id"])
    token1_derived_eth = await find_eth_per_token(info, token1["id"])

    logger.info(
        "refresh token eth price",
        token0=token0_derived_eth,
        token1=token1_derived_eth,
    )

    tracked_liquidity_usd = await get_tracked_liquidity_usd(
        info, token0, reserve0, token1, reserve1
    )
    if info.context.eth_price != Decimal("0"):
        tracked_liquidity_eth = tracked_liquidity_usd / info.context.eth_price
    else:
        tracked_liquidity_eth = Decimal("0")

    reserve_eth = (
        reserve0 * token0["derived_eth"].to_decimal()
        + reserve1 * token1["derived_eth"].to_decimal()
    )
    reserve_usd = reserve_eth * info.context.eth_price

    # update derived amounts
    await info.storage.find_one_and_update(
        "pairs",
        {
            "id": felt(pair_address),
        },
        {
            "$set": {
                "tracked_reserve_eth": Decimal128(tracked_liquidity_eth),
                "reserve_eth": Decimal128(reserve_eth),
                "reserve_usd": Decimal128(reserve_usd),
            }
        },
    )

    factory = await info.storage.find_one("factories", {"id": felt(jediswap_factory)})

    total_liquidity_eth = factory["total_liquidity_eth"].to_decimal() + tracked_liquidity_eth
    total_liquidity_usd = total_liquidity_eth * info.context.eth_price

    await info.storage.find_one_and_update(
        "factories",
        {"id": felt(jediswap_factory)},
        {
            "$set": {
                "total_liquidity_eth": Decimal128(total_liquidity_eth),
                "total_liquidity_usd": Decimal128(total_liquidity_usd),
            }
        },
    )


async def handle_mint(info: Info, header: BlockHeader, event: StarkNetEvent):
    mint = decode_event(mint_decoder, event.data)
    logger.info("handle Mint", **mint._asdict())


async def handle_swap(info: Info, header: BlockHeader, event: StarkNetEvent):
    swap = decode_event(swap_decoder, event.data)
    logger.info("handle Swap", **swap._asdict())


async def handle_burn(info: Info, header: BlockHeader, event: StarkNetEvent):
    burn = decode_event(burn_decoder, event.data)
    logger.info("handle Burn", **burn._asdict())


def _is_complete_mint(mint):
    return mint["sender"] is not None
