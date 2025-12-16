# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import math
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.hyperliquid.config import HyperliquidExecClientConfig
from nautilus_trader.adapters.hyperliquid.constants import HYPERLIQUID_VENUE
from nautilus_trader.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.execution.messages import BatchCancelOrders
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import QueryAccount
from nautilus_trader.execution.messages import QueryOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.live.cancellation import DEFAULT_FUTURE_CANCELLATION_TIMEOUT
from nautilus_trader.live.cancellation import cancel_tasks_with_timeout
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import order_side_to_str
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.events import OrderExpired
from nautilus_trader.model.events import OrderModifyRejected
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.events import OrderUpdated
from nautilus_trader.model.functions import order_side_to_pyo3
from nautilus_trader.model.functions import order_type_to_pyo3
from nautilus_trader.model.functions import time_in_force_to_pyo3
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money


class HyperliquidExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for the Hyperliquid decentralized exchange (DEX).

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : Any
        The Hyperliquid HTTP client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : HyperliquidInstrumentProvider
        The instrument provider.
    config : HyperliquidExecClientConfig
        The configuration for the client.
    name : str, optional
        The custom client ID.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: Any,  # TODO: Replace with actual HyperliquidHttpClient when available
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: HyperliquidInstrumentProvider,
        config: HyperliquidExecClientConfig,
        name: str | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name or HYPERLIQUID_VENUE.value),
            venue=HYPERLIQUID_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,  # Multi-currency account
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        # Configuration
        self._config = config
        self._client = client
        self._instrument_provider: HyperliquidInstrumentProvider = instrument_provider

        # Log configuration details
        self._log.info(f"config.testnet={config.testnet}", LogColor.BLUE)
        self._log.info(f"config.http_timeout_secs={config.http_timeout_secs}", LogColor.BLUE)
        self._log.info(f"{config.http_proxy_url=}", LogColor.BLUE)
        self._log.info(f"{config.ws_proxy_url=}", LogColor.BLUE)

        account_id = AccountId(f"{name or HYPERLIQUID_VENUE.value}-master")
        self._set_account_id(account_id)

        # WebSocket clients for order/execution updates
        self._ws_clients: dict[
            nautilus_pyo3.HyperliquidProductType,
            nautilus_pyo3.HyperliquidWebSocketClient,
        ] = {}
        self._ws_client_futures: set[asyncio.Future] = set()

        # Timeout for waiting for order to appear in cache when fills arrive early
        self._fill_wait_timeout_secs: float = 5.0

        # Track processed trade IDs to avoid duplicate fill warnings
        # Hyperliquid WebSocket sometimes sends duplicate userEvents messages
        self._processed_trade_ids: set[str] = set()

        # Get user address from HTTP client for WebSocket subscriptions
        self._user_address: str | None = None
        try:
            self._user_address = self._client.get_user_address()
            self._log.info(f"User address: {self._user_address}", LogColor.BLUE)
        except Exception as e:
            self._log.warning(f"Could not get user address: {e}")

        # Initialize WebSocket clients for each product type
        for product_type_str in ["PERP", "SPOT"]:
            product_type = nautilus_pyo3.HyperliquidProductType.from_str(product_type_str)
            ws_client = nautilus_pyo3.HyperliquidWebSocketClient(
                url=config.base_url_ws,
                testnet=config.testnet,
                product_type=product_type,
                account_id=str(account_id),
            )
            self._ws_clients[product_type] = ws_client
            self._log.info(f"Initialized WebSocket client for {product_type_str}", LogColor.BLUE)

    @property
    def hyperliquid_instrument_provider(self) -> HyperliquidInstrumentProvider:
        return self._instrument_provider

    def _cache_instruments(self) -> None:
        # Ensures instrument definitions are available for correct
        # price and size precisions when parsing responses
        instruments_pyo3 = self._instrument_provider.instruments_pyo3()
        for inst in instruments_pyo3:
            self._client.cache_instrument(inst)

        self._log.debug("Cached instruments", LogColor.MAGENTA)

    async def _update_account_state(self) -> None:
        try:
            pyo3_account_state = await self._client.request_account_state()
            account_state = AccountState.from_dict(pyo3_account_state.to_dict())

            self.generate_account_state(
                balances=account_state.balances,
                margins=account_state.margins,
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )

            if account_state.balances:
                self._log.info(
                    f"Generated account state with {len(account_state.balances)} balance(s)",
                )
            else:
                self._log.info("Generated account state with 0 balances")
        except Exception as e:
            self._log.error(f"Failed to update account state: {e}")
            # Generate fallback account state with zero balance to register the account
            # The engine requires at least one balance entry
            usdc = Currency.from_str("USDC")
            zero_balance = AccountBalance(
                total=Money(0, usdc),
                locked=Money(0, usdc),
                free=Money(0, usdc),
            )
            self.generate_account_state(
                balances=[zero_balance],
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )

    # -- CONNECTION HANDLERS -----------------------------------------------------------------------

    async def _connect(self) -> None:
        self._log.info("Loading instruments...", LogColor.BLUE)
        await self._instrument_provider.initialize()
        self._cache_instruments()

        # Set account ID on HTTP client for report generation
        self._client.set_account_id(str(self.account_id))

        self._log.info(
            f"Loaded {len(self._instrument_provider.list_all())} instruments",
            LogColor.GREEN,
        )

        # Generate initial account state
        await self._update_account_state()
        await self._await_account_registered()

        # Connect WebSocket clients and subscribe to order updates
        instruments = self._instrument_provider.instruments_pyo3()

        for product_type, ws_client in self._ws_clients.items():
            await ws_client.connect(instruments, self._handle_msg)
            self._log.info(
                f"Connected to {product_type} WebSocket {ws_client.url}",
                LogColor.BLUE,
            )

            # Subscribe to order updates if we have user address
            if self._user_address:
                await ws_client.subscribe_order_updates(self._user_address)
                self._log.info(
                    f"Subscribed to {product_type} order updates for {self._user_address}",
                    LogColor.BLUE,
                )

                await ws_client.subscribe_user_events(self._user_address)
                self._log.info(
                    f"Subscribed to {product_type} user events (includes fills) for {self._user_address}",
                    LogColor.BLUE,
                )

    async def _disconnect(self) -> None:
        # Delay to allow websocket to send any unsubscribe messages
        await asyncio.sleep(1.0)

        # Shutdown all WebSocket clients
        for product_type, ws_client in self._ws_clients.items():
            if not ws_client.is_closed():
                self._log.info(f"Disconnecting {product_type} WebSocket")
                await ws_client.close()
                self._log.info(
                    f"Disconnected from {product_type} WebSocket {ws_client.url}",
                    LogColor.BLUE,
                )

        # Cancel all WebSocket client futures with timeout
        if self._ws_client_futures:
            self._log.debug(f"Canceling {len(self._ws_client_futures)} WebSocket client futures...")
            await cancel_tasks_with_timeout(
                self._ws_client_futures,
                timeout_secs=DEFAULT_FUTURE_CANCELLATION_TIMEOUT,
                logger=self._log,
            )
            self._ws_client_futures.clear()

    # -- COMMANDS ---------------------------------------------------------------------------------

    async def _wait_for_quote(
        self,
        instrument_id: Any,
        timeout_secs: float = 5.0,
        poll_interval_secs: float = 0.1,
    ) -> Any | None:
        elapsed = 0.0
        while elapsed < timeout_secs:
            quote = self._cache.quote_tick(instrument_id)
            if quote is not None:
                return quote
            await asyncio.sleep(poll_interval_secs)
            elapsed += poll_interval_secs
        return None

    def _round_to_significant_figures(self, value: Decimal, sig_figs: int = 5) -> Decimal:
        # Hyperliquid requires max 5 significant figures for prices
        if value == 0:
            return Decimal(0)

        abs_val = abs(float(value))
        # Find order of magnitude (position of first significant digit)
        magnitude = math.floor(math.log10(abs_val))
        # Calculate the shift needed to round to sig_figs
        shift = sig_figs - 1 - magnitude
        factor = Decimal(10) ** shift
        rounded = (value * factor).quantize(Decimal(1)) / factor
        return rounded

    async def _calculate_market_order_price(self, order: Any) -> Any:
        # Default slippage: 0.5% for market orders
        slippage_pct = Decimal("0.005")

        # Get the quote from cache, waiting briefly if not available
        quote = self._cache.quote_tick(order.instrument_id)
        if quote is None:
            self._log.info(
                f"No cached quote for {order.instrument_id}, waiting for quote data...",
            )
            quote = await self._wait_for_quote(order.instrument_id)

        instrument = self._cache.instrument(order.instrument_id)

        if quote is None or instrument is None:
            self._log.error(
                f"Cannot calculate market order price: no cached quote for {order.instrument_id}. "
                "Ensure quote data is subscribed before submitting market orders.",
            )
            raise ValueError(
                f"No cached quote available for {order.instrument_id} to calculate market order price",
            )

        # Calculate price with slippage
        if order.side == OrderSide.BUY:
            # For buys, add slippage to the ask price
            base_price = Decimal(str(quote.ask_price))
            price = base_price * (Decimal(1) + slippage_pct)
        else:
            # For sells, subtract slippage from the bid price
            base_price = Decimal(str(quote.bid_price))
            price = base_price * (Decimal(1) - slippage_pct)

        # Hyperliquid requires max 5 significant figures AND max decimal places
        price = self._round_to_significant_figures(price, sig_figs=5)
        price = round(price, instrument.price_precision)

        self._log.debug(
            f"Calculated market order price: {price} (base: {base_price}, slippage: {slippage_pct})",
        )

        return nautilus_pyo3.Price.from_str(str(price))

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order

        if order.is_closed:
            self._log.warning(f"Order {order} is already closed")
            return

        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        try:
            pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(order.instrument_id.value)
            pyo3_client_order_id = nautilus_pyo3.ClientOrderId(order.client_order_id.value)
            pyo3_order_side = order_side_to_pyo3(order.side)
            pyo3_order_type = order_type_to_pyo3(order.order_type)
            pyo3_quantity = nautilus_pyo3.Quantity.from_str(str(order.quantity))
            pyo3_time_in_force = time_in_force_to_pyo3(order.time_in_force)

            # For market orders, calculate a slippage price from the cached quote
            if order.has_price:
                pyo3_price = nautilus_pyo3.Price.from_str(str(order.price))
            elif order.order_type in (
                OrderType.MARKET,
                OrderType.STOP_MARKET,
                OrderType.MARKET_IF_TOUCHED,
            ):
                pyo3_price = await self._calculate_market_order_price(order)
            else:
                pyo3_price = None

            pyo3_trigger_price = (
                nautilus_pyo3.Price.from_str(str(order.trigger_price))
                if order.has_trigger_price
                else None
            )

            report = await self._client.submit_order(
                instrument_id=pyo3_instrument_id,
                client_order_id=pyo3_client_order_id,
                order_side=pyo3_order_side,
                order_type=pyo3_order_type,
                quantity=pyo3_quantity,
                time_in_force=pyo3_time_in_force,
                price=pyo3_price,
                trigger_price=pyo3_trigger_price,
                post_only=order.is_post_only,
                reduce_only=order.is_reduce_only,
            )

            self._log.debug(f"Received order status report: {report}")

            venue_order_id = VenueOrderId(report.venue_order_id.value)
            report_status = report.order_status

            # Always generate accepted first
            self.generate_order_accepted(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                ts_event=self._clock.timestamp_ns(),
            )

            self._log.info(
                f"Order {order.client_order_id} accepted, venue_order_id={venue_order_id}",
            )

            # Note: If order was immediately filled (market orders), the fill will arrive
            # via WebSocket userEvents. The _process_fill_with_wait mechanism handles the
            # race condition where the WebSocket fill arrives before this HTTP response.
            if report_status.value == OrderStatus.FILLED.value:
                self._log.debug(
                    f"Order {order.client_order_id} reported as FILLED, "
                    "expecting fill via WebSocket",
                )

        except Exception as e:
            self._log.error(f"Error submitting order {order.client_order_id}: {e}")
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        order_list = command.order_list
        orders = order_list.orders

        if not orders:
            self._log.warning("Order list is empty, nothing to submit")
            return

        # Check if all orders are open
        closed_orders = [order for order in orders if order.is_closed]
        if closed_orders:
            self._log.warning(f"Skipping {len(closed_orders)} closed orders in batch")
            orders = [order for order in orders if not order.is_closed]

        if not orders:
            return

        now_ns = self._clock.timestamp_ns()

        for order in orders:
            self.generate_order_submitted(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                ts_event=now_ns,
            )

        try:
            reports = await self._client.submit_orders(orders)

            self._log.debug(f"Received {len(reports)} order status reports")

            # Generate acceptance events for all successfully submitted orders
            for report in reports:
                order = next(
                    (o for o in orders if o.client_order_id == report.client_order_id),
                    None,
                )
                if order:
                    venue_order_id = VenueOrderId(report.venue_order_id.value)
                    report_status = report.order_status

                    self.generate_order_accepted(
                        strategy_id=order.strategy_id,
                        instrument_id=order.instrument_id,
                        client_order_id=order.client_order_id,
                        venue_order_id=venue_order_id,
                        ts_event=self._clock.timestamp_ns(),
                    )
                    self._log.info(
                        f"Order {order.client_order_id} accepted, venue_order_id={venue_order_id}",
                    )

                    # Note: Fills arrive via WebSocket userEvents
                    if report_status.value == OrderStatus.FILLED.value:
                        self._log.debug(
                            f"Order {order.client_order_id} reported as FILLED, "
                            "expecting fill via WebSocket",
                        )

        except Exception as e:
            self._log.error(f"Error submitting order batch: {e}")
            # Generate rejection events for all orders
            for order in orders:
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    reason=str(e),
                    ts_event=self._clock.timestamp_ns(),
                )

    async def _modify_order(self, command: ModifyOrder) -> None:
        # The modify functionality exists in Rust but requires exposing post_action() to Python
        self._log.warning(
            f"Order modification requires venue_order_id and is not yet exposed via Python bindings for {command.client_order_id}",
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        # Try to get venue_order_id from cache first, fall back to command
        order = self._cache.order(command.client_order_id)
        venue_order_id = None
        if order and order.venue_order_id:
            venue_order_id = order.venue_order_id
        elif command.venue_order_id:
            venue_order_id = command.venue_order_id

        try:
            pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                command.instrument_id.value,
            )
            pyo3_client_order_id = nautilus_pyo3.ClientOrderId(command.client_order_id.value)
            pyo3_venue_order_id = (
                nautilus_pyo3.VenueOrderId(venue_order_id.value) if venue_order_id else None
            )

            await self._client.cancel_order(
                instrument_id=pyo3_instrument_id,
                client_order_id=pyo3_client_order_id,
                venue_order_id=pyo3_venue_order_id,
            )
            self._log.info(f"Order cancellation requested for {command.client_order_id}")
        except Exception as e:
            self._log.error(f"Failed to cancel order {command.client_order_id}: {e}")

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        open_orders = self._cache.orders_open(
            venue=self.venue,
            instrument_id=command.instrument_id,
            side=command.order_side,
        )

        if not open_orders:
            instrument_str = (
                f" for {command.instrument_id}" if command.instrument_id is not None else ""
            )
            self._log.info(f"No open orders to cancel{instrument_str}")
            return

        if command.order_side != OrderSide.NO_ORDER_SIDE:
            self._log.info(
                f"Filtering orders by side: {order_side_to_str(command.order_side)}",
            )

        self._log.info(f"Cancelling {len(open_orders)} open order(s)")
        for order in open_orders:
            try:
                pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                    order.instrument_id.value,
                )
                pyo3_client_order_id = nautilus_pyo3.ClientOrderId(order.client_order_id.value)
                pyo3_venue_order_id = (
                    nautilus_pyo3.VenueOrderId(order.venue_order_id.value)
                    if order.venue_order_id
                    else None
                )

                await self._client.cancel_order(
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    venue_order_id=pyo3_venue_order_id,
                )
                self._log.debug(f"Cancellation requested for {order.client_order_id}")
            except Exception as e:
                self._log.error(f"Failed to cancel order {order.client_order_id}: {e}")

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        if not command.cancels:
            self._log.info("No orders to cancel in batch")
            return

        for cancel_cmd in command.cancels:
            order = self._cache.order(cancel_cmd.client_order_id)
            if not order:
                self._log.warning(
                    f"Cannot cancel order {cancel_cmd.client_order_id}: not found in cache",
                )
                continue

            try:
                pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(
                    cancel_cmd.instrument_id.value,
                )
                pyo3_client_order_id = nautilus_pyo3.ClientOrderId(cancel_cmd.client_order_id.value)
                pyo3_venue_order_id = (
                    nautilus_pyo3.VenueOrderId(order.venue_order_id.value)
                    if order.venue_order_id
                    else None
                )

                await self._client.cancel_order(
                    instrument_id=pyo3_instrument_id,
                    client_order_id=pyo3_client_order_id,
                    venue_order_id=pyo3_venue_order_id,
                )
                self._log.debug(f"Cancellation requested for {cancel_cmd.client_order_id}")
            except Exception as e:
                self._log.error(f"Failed to cancel order {cancel_cmd.client_order_id}: {e}")

    # -- REPORTS ----------------------------------------------------------------------------------

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        try:
            # Query all open orders and filter by client_order_id or venue_order_id
            instrument_id = command.instrument_id.value if command.instrument_id else None
            pyo3_reports = await self._client.request_order_status_reports(
                instrument_id=instrument_id,
            )

            for pyo3_report in pyo3_reports:
                # Match by client_order_id if provided
                if (
                    command.client_order_id
                    and pyo3_report.client_order_id
                    and pyo3_report.client_order_id.value == command.client_order_id.value
                ):
                    report = OrderStatusReport.from_pyo3(pyo3_report)
                    self._log.debug(f"Found order status report: {report}")
                    return report

                # Match by venue_order_id if provided
                if (
                    command.venue_order_id
                    and pyo3_report.venue_order_id.value == command.venue_order_id.value
                ):
                    report = OrderStatusReport.from_pyo3(pyo3_report)
                    self._log.debug(f"Found order status report: {report}")
                    return report

            self._log.warning(
                f"No order status report found for client_order_id={command.client_order_id}, "
                f"venue_order_id={command.venue_order_id}",
            )
            return None
        except Exception as e:
            self._log.error(f"Failed to generate order status report: {e}")
            return None

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        try:
            instrument_id = command.instrument_id.value if command.instrument_id else None
            pyo3_reports = await self._client.request_order_status_reports(
                instrument_id=instrument_id,
            )

            # Convert PyO3 reports to Cython reports
            reports = [OrderStatusReport.from_pyo3(r) for r in pyo3_reports]

            self._log_report_receipt(
                len(reports),
                "OrderStatusReport",
                command.log_receipt_level,
                "Generated",
            )
            return reports
        except Exception as e:
            self._log.error(f"Failed to generate order status reports: {e}")
            return []

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        try:
            instrument_id = command.instrument_id.value if command.instrument_id else None
            pyo3_reports = await self._client.request_fill_reports(instrument_id=instrument_id)

            # Convert PyO3 reports to Cython reports
            reports = [FillReport.from_pyo3(r) for r in pyo3_reports]

            self._log_report_receipt(len(reports), "FillReport", LogLevel.INFO, "Generated")
            return reports
        except Exception as e:
            self._log.error(f"Failed to generate fill reports: {e}")
            return []

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        try:
            instrument_id = command.instrument_id.value if command.instrument_id else None
            pyo3_reports = await self._client.request_position_status_reports(
                instrument_id=instrument_id,
            )

            # Convert PyO3 reports to Cython reports
            reports = [PositionStatusReport.from_pyo3(r) for r in pyo3_reports]

            self._log_report_receipt(
                len(reports),
                "PositionStatusReport",
                command.log_receipt_level,
            )

            return reports
        except Exception as e:
            self._log.error(f"Failed to generate position status reports: {e}")
            return []

    async def _request_and_process_fills_for_order(
        self,
        order: Any,
        venue_order_id: VenueOrderId,
    ) -> None:
        try:
            pyo3_reports = await self._client.request_fill_reports(
                instrument_id=order.instrument_id.value,
            )

            instrument = self._cache.instrument(order.instrument_id)
            if instrument is None:
                self._log.error(
                    f"Cannot process fills - instrument {order.instrument_id} not found",
                )
                return

            for pyo3_report in pyo3_reports:
                report = FillReport.from_pyo3(pyo3_report)

                # Match by venue_order_id
                if report.venue_order_id != venue_order_id:
                    continue

                self._log.debug(f"Processing fill for order {order.client_order_id}: {report}")

                self.generate_order_filled(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    venue_position_id=report.venue_position_id,
                    trade_id=report.trade_id,
                    order_side=order.side,
                    order_type=order.order_type,
                    last_qty=report.last_qty,
                    last_px=report.last_px,
                    quote_currency=instrument.quote_currency,
                    commission=report.commission,
                    liquidity_side=report.liquidity_side,
                    ts_event=report.ts_event,
                )

        except Exception as e:
            self._log.error(f"Failed to request fill reports for {order.client_order_id}: {e}")

    # -- QUERIES ----------------------------------------------------------------------------------

    async def _query_order(self, command: QueryOrder) -> None:
        self._log.info(
            f"Direct order query not implemented for {command.client_order_id}, "
            f"order state is maintained through WebSocket updates and reconciliation",
        )

    async def _query_account(self, command: QueryAccount) -> None:
        self._log.info(
            "Direct account query not implemented, "
            "account state is maintained through WebSocket updates and reconciliation",
        )

    # -- MESSAGE HANDLERS -------------------------------------------------------------------------

    def _handle_msg(self, msg: Any) -> None:  # noqa: C901 (too complex)
        try:
            if isinstance(msg, nautilus_pyo3.AccountState):
                self._handle_account_state(msg)
            elif isinstance(msg, nautilus_pyo3.OrderAccepted):
                self._handle_order_accepted_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderCanceled):
                self._handle_order_canceled_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderExpired):
                self._handle_order_expired_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderUpdated):
                self._handle_order_updated_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderRejected):
                self._handle_order_rejected_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderCancelRejected):
                self._handle_order_cancel_rejected_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderModifyRejected):
                self._handle_order_modify_rejected_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.OrderStatusReport):
                self._handle_order_status_report_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.FillReport):
                self._handle_fill_report_pyo3(msg)
            elif isinstance(msg, nautilus_pyo3.PositionStatusReport):
                self._handle_position_status_report_pyo3(msg)
            else:
                self._log.debug(f"Received unhandled message type: {type(msg)}")
        except Exception as e:
            self._log.exception("Error handling websocket message", e)

    def _handle_account_state(self, msg: nautilus_pyo3.AccountState) -> None:
        account_state = AccountState.from_dict(msg.to_dict())
        self.generate_account_state(
            balances=account_state.balances,
            margins=account_state.margins,
            reported=account_state.is_reported,
            ts_event=account_state.ts_event,
        )

    def _handle_order_accepted_pyo3(self, msg: nautilus_pyo3.OrderAccepted) -> None:
        event = OrderAccepted.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_canceled_pyo3(self, msg: nautilus_pyo3.OrderCanceled) -> None:
        event = OrderCanceled.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_expired_pyo3(self, msg: nautilus_pyo3.OrderExpired) -> None:
        event = OrderExpired.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_updated_pyo3(self, msg: nautilus_pyo3.OrderUpdated) -> None:
        event = OrderUpdated.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_rejected_pyo3(self, msg: nautilus_pyo3.OrderRejected) -> None:
        event = OrderRejected.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_cancel_rejected_pyo3(self, msg: nautilus_pyo3.OrderCancelRejected) -> None:
        event = OrderCancelRejected.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_modify_rejected_pyo3(self, msg: nautilus_pyo3.OrderModifyRejected) -> None:
        event = OrderModifyRejected.from_dict(msg.to_dict())
        self._send_order_event(event)

    def _handle_order_status_report_pyo3(  # noqa: C901 (too complex)
        self,
        pyo3_report: nautilus_pyo3.OrderStatusReport,
    ) -> None:
        report = OrderStatusReport.from_pyo3(pyo3_report)

        # Check if we already track this order by venue_order_id
        if report.venue_order_id:
            existing_client_order_id = self._cache.client_order_id(report.venue_order_id)
            if existing_client_order_id:
                # We already track this order - skip WebSocket status report
                # as we already processed the order via HTTP response
                self._log.debug(
                    f"Skipping WebSocket order status for {report.venue_order_id} - "
                    f"already tracking as {existing_client_order_id}",
                )
                return

            # For rejected orders, skip if we don't already track them
            # We'll get rejection from HTTP response for our orders
            if report.order_status == OrderStatus.REJECTED:
                self._log.debug(
                    f"Skipping WebSocket rejected order {report.venue_order_id} - "
                    "not tracking (will receive rejection via HTTP if ours)",
                )
                return

            # WebSocket order status may arrive before HTTP response populates cache
            # Wait for the order to appear before processing (similar to fill handling)
            if report.order_status == OrderStatus.ACCEPTED:
                self._log.debug(
                    f"Order status for venue_order_id={report.venue_order_id} arrived before "
                    "HTTP response, scheduling async wait",
                )
                self._loop.create_task(self._process_order_status_with_wait(report))
                return

        if self._is_external_order(report.client_order_id):
            self._send_order_status_report(report)
            return

        order = self._cache.order(report.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot process order status report - order for {report.client_order_id!r} not found",
            )
            return

        if order.linked_order_ids is not None:
            report.linked_order_ids = list(order.linked_order_ids)

        if report.order_status == OrderStatus.REJECTED:
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=report.instrument_id,
                client_order_id=report.client_order_id,
                reason=report.cancel_reason or "Order rejected by exchange",
                ts_event=report.ts_last,
            )
        elif report.order_status == OrderStatus.ACCEPTED:
            if order.status in (
                OrderStatus.ACCEPTED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
            ):
                return
            self.generate_order_accepted(
                strategy_id=order.strategy_id,
                instrument_id=report.instrument_id,
                client_order_id=report.client_order_id,
                venue_order_id=report.venue_order_id,
                ts_event=report.ts_last,
            )
        elif report.order_status == OrderStatus.PENDING_CANCEL:
            if order.status == OrderStatus.PENDING_CANCEL:
                self._log.debug(
                    f"Received PENDING_CANCEL status for {report.client_order_id!r} - "
                    "order already in pending cancel state locally",
                )
            else:
                self._log.warning(
                    f"Received PENDING_CANCEL status for {report.client_order_id!r} - "
                    f"order status {order.status_string()}",
                )
        elif report.order_status == OrderStatus.CANCELED:
            if order.status == OrderStatus.CANCELED:
                return
            self.generate_order_canceled(
                strategy_id=order.strategy_id,
                instrument_id=report.instrument_id,
                client_order_id=report.client_order_id,
                venue_order_id=report.venue_order_id,
                ts_event=report.ts_last,
            )
        elif report.order_status == OrderStatus.EXPIRED:
            self.generate_order_expired(
                strategy_id=order.strategy_id,
                instrument_id=report.instrument_id,
                client_order_id=report.client_order_id,
                venue_order_id=report.venue_order_id,
                ts_event=report.ts_last,
            )
        elif report.order_status == OrderStatus.TRIGGERED:
            self.generate_order_triggered(
                strategy_id=order.strategy_id,
                instrument_id=report.instrument_id,
                client_order_id=report.client_order_id,
                venue_order_id=report.venue_order_id,
                ts_event=report.ts_last,
            )
        else:
            self._log.debug(f"Received unhandled OrderStatusReport: {report}")

    def _handle_fill_report_pyo3(self, pyo3_report: nautilus_pyo3.FillReport) -> None:
        report = FillReport.from_pyo3(pyo3_report)

        self._log.debug(
            f"Received fill from WebSocket: venue_order_id={report.venue_order_id}, "
            f"trade_id={report.trade_id}, qty={report.last_qty}, px={report.last_px}",
        )

        # Skip duplicate fills (Hyperliquid sometimes sends duplicate userEvents)
        trade_id_str = report.trade_id.value
        if trade_id_str in self._processed_trade_ids:
            self._log.debug(f"Skipping duplicate fill: trade_id={report.trade_id}")
            return
        self._processed_trade_ids.add(trade_id_str)

        # Try to find client_order_id from venue_order_id if not provided
        client_order_id = report.client_order_id
        if client_order_id is None and report.venue_order_id:
            client_order_id = self._cache.client_order_id(report.venue_order_id)
            if client_order_id:
                self._log.debug(
                    f"Resolved client_order_id={client_order_id} from venue_order_id={report.venue_order_id}",
                )

        # If we can't match the fill yet, it may have arrived before the HTTP response
        # Schedule async processing to wait for the order to appear in cache
        if client_order_id is None and report.venue_order_id:
            self._log.debug(
                f"Fill for venue_order_id={report.venue_order_id} arrived before order acceptance, "
                "scheduling async wait",
            )
            self._loop.create_task(self._process_fill_with_wait(report))
            return

        if self._is_external_order(client_order_id):
            self._log.debug(
                f"Fill for venue_order_id={report.venue_order_id} treated as external "
                f"(client_order_id={client_order_id})",
            )
            self._send_fill_report(report)
            return

        order = self._cache.order(client_order_id)
        if order is None:
            self._log.error(
                f"Cannot process fill report - order for {client_order_id!r} "
                f"(venue_order_id={report.venue_order_id}) not found in cache",
            )
            return

        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            self._log.error(
                f"Cannot process fill report - instrument {order.instrument_id} not found",
            )
            return

        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=report.venue_order_id,
            venue_position_id=report.venue_position_id,
            trade_id=report.trade_id,
            order_side=order.side,
            order_type=order.order_type,
            last_qty=report.last_qty,
            last_px=report.last_px,
            quote_currency=instrument.quote_currency,
            commission=report.commission,
            liquidity_side=report.liquidity_side,
            ts_event=report.ts_event,
        )

    def _handle_position_status_report_pyo3(
        self,
        msg: nautilus_pyo3.PositionStatusReport,
    ) -> None:
        report = PositionStatusReport.from_pyo3(msg)
        self._log.debug(f"Received {report}", LogColor.MAGENTA)

    def _is_external_order(self, client_order_id: ClientOrderId) -> bool:
        return not client_order_id or not self._cache.strategy_id_for_order(client_order_id)

    async def _process_fill_with_wait(self, report: FillReport) -> None:
        """
        Process a fill report, waiting for the order to appear in cache if necessary.

        This handles the race condition where WebSocket fills arrive before the HTTP
        response that registers the venue_order_id -> client_order_id mapping.

        """
        venue_order_id = report.venue_order_id
        start_time = self._clock.timestamp_ns()
        timeout_ns = int(self._fill_wait_timeout_secs * 1e9)

        # Poll for the order to appear in cache
        while (self._clock.timestamp_ns() - start_time) < timeout_ns:
            client_order_id = self._cache.client_order_id(venue_order_id)
            if client_order_id:
                self._log.debug(
                    f"Order appeared in cache after wait: {client_order_id} "
                    f"for venue_order_id={venue_order_id}",
                )
                break
            await asyncio.sleep(0.05)  # 50ms poll interval
        else:
            # Timeout - treat as external order
            self._log.warning(
                f"Timeout waiting for order with venue_order_id={venue_order_id}, "
                "treating fill as external",
            )
            self._send_fill_report(report)
            return

        # Now we have the client_order_id, process the fill
        if self._is_external_order(client_order_id):
            self._send_fill_report(report)
            return

        order = self._cache.order(client_order_id)
        if order is None:
            self._log.error(
                f"Cannot process fill report - order for {client_order_id!r} "
                f"(venue_order_id={venue_order_id}) not found in cache after wait",
            )
            return

        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            self._log.error(
                f"Cannot process fill report - instrument {order.instrument_id} not found",
            )
            return

        self._log.info(
            f"Processing delayed fill for {order.client_order_id}: "
            f"trade_id={report.trade_id}, qty={report.last_qty}, px={report.last_px}",
        )

        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=report.venue_position_id,
            trade_id=report.trade_id,
            order_side=order.side,
            order_type=order.order_type,
            last_qty=report.last_qty,
            last_px=report.last_px,
            quote_currency=instrument.quote_currency,
            commission=report.commission,
            liquidity_side=report.liquidity_side,
            ts_event=report.ts_event,
        )

    async def _process_order_status_with_wait(self, report: OrderStatusReport) -> None:
        """
        Process an order status report, waiting for the order to appear in cache if
        necessary.

        This handles the race condition where WebSocket order status arrives before the
        HTTP response that registers the venue_order_id -> client_order_id mapping.

        """
        venue_order_id = report.venue_order_id
        start_time = self._clock.timestamp_ns()
        timeout_ns = int(self._fill_wait_timeout_secs * 1e9)

        # Poll for the order to appear in cache
        while (self._clock.timestamp_ns() - start_time) < timeout_ns:
            client_order_id = self._cache.client_order_id(venue_order_id)
            if client_order_id:
                self._log.debug(
                    f"Order appeared in cache after wait: {client_order_id} "
                    f"for venue_order_id={venue_order_id}",
                )
                # Order is now in cache, HTTP already processed it
                # Skip the WebSocket update as it's redundant
                return
            await asyncio.sleep(0.05)  # 50ms poll interval

        # Timeout - order never appeared, treat as external
        self._log.debug(
            f"Timeout waiting for order with venue_order_id={venue_order_id}, "
            "treating as external order",
        )
        self._send_order_status_report(report)
