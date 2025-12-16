// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

use std::{env, str::FromStr};

use nautilus_hyperliquid::http::{
    client::HyperliquidHttpClient,
    models::{
        Cloid, HyperliquidExecAction, HyperliquidExecGrouping, HyperliquidExecLimitParams,
        HyperliquidExecOrderKind, HyperliquidExecPlaceOrderRequest, HyperliquidExecTif,
    },
};
use nautilus_model::identifiers::ClientOrderId;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_target(false).with_env_filter(filter).init();

    // Check for testnet flag from environment (default to mainnet)
    let is_testnet = env::var("HYPERLIQUID_TESTNET")
        .map(|v| v.to_lowercase() == "true" || v == "1")
        .unwrap_or(false);

    let network_name = if is_testnet { "TESTNET" } else { "MAINNET" };
    tracing::info!("Starting Hyperliquid {network_name} Order Placer");

    let client = match HyperliquidHttpClient::from_env(is_testnet) {
        Ok(client) => {
            let is_testnet = client.is_testnet();
            tracing::info!("Client created (testnet: {is_testnet})");
            client
        }
        Err(e) => {
            tracing::error!("Failed to create client: {e}");
            let pk_var = if is_testnet {
                "HYPERLIQUID_TESTNET_PK"
            } else {
                "HYPERLIQUID_PK"
            };
            tracing::error!("Make sure {pk_var} environment variable is set");
            return Err(e.into());
        }
    };

    tracing::info!("Fetching market metadata...");
    let meta = client.info_meta().await?;

    // Debug: Print all assets
    tracing::debug!("Available assets:");
    for (idx, asset) in meta.universe.iter().enumerate() {
        tracing::debug!(
            "  [{}] {} (sz_decimals: {})",
            idx,
            asset.name,
            asset.sz_decimals
        );
    }

    let btc_asset_id = meta
        .universe
        .iter()
        .position(|asset| asset.name == "BTC")
        .expect("BTC not found in universe");

    tracing::info!("BTC asset ID: {btc_asset_id}");
    let sz_decimals = meta.universe[btc_asset_id].sz_decimals;
    tracing::info!("BTC sz_decimals: {sz_decimals}");

    // Get the wallet address to verify authentication
    let wallet_address = client
        .get_user_address()
        .expect("Failed to get wallet address");
    tracing::info!("Wallet address: {wallet_address}");

    // Check account state before placing order
    tracing::info!("Fetching account state...");
    match client.info_clearinghouse_state(&wallet_address).await {
        Ok(state) => {
            let state_json =
                serde_json::to_string_pretty(&state).unwrap_or_else(|_| "N/A".to_string());
            tracing::info!("Account state: {state_json}");
        }
        Err(e) => {
            tracing::warn!("Failed to fetch account state: {e}");
        }
    }

    tracing::info!("Fetching BTC order book...");
    let book = client.info_l2_book("BTC").await?;

    let best_bid_str = &book.levels[0][0].px;
    let best_bid = Decimal::from_str(best_bid_str)?;

    tracing::info!("Best bid: ${best_bid}");

    // BTC prices on Hyperliquid must be whole dollars (no decimal places)
    let limit_price = (best_bid * dec!(0.95)).round();
    tracing::info!("Limit order price: ${limit_price}");

    // Create cloid from a test ClientOrderId (production-like)
    let client_order_id = ClientOrderId::from("O-20241210-TEST-001-001-1");
    let cloid = Cloid::from_client_order_id(client_order_id);
    tracing::info!("ClientOrderId: {client_order_id}");
    let cloid_hex = cloid.to_hex();
    tracing::info!("Cloid: {cloid_hex}");

    let order = HyperliquidExecPlaceOrderRequest {
        asset: btc_asset_id as u32,
        is_buy: true,
        price: limit_price,
        size: dec!(0.001),
        reduce_only: false,
        kind: HyperliquidExecOrderKind::Limit {
            limit: HyperliquidExecLimitParams {
                tif: HyperliquidExecTif::Gtc,
            },
        },
        cloid: Some(cloid),
    };

    tracing::info!("Order details:");
    tracing::info!("  Asset: {btc_asset_id} (BTC)");
    tracing::info!("  Side: BUY");
    tracing::info!("  Price: ${limit_price}");
    tracing::info!("  Size: 0.001 BTC");
    let order_cloid = order.cloid.as_ref().unwrap().to_hex();
    tracing::info!("  Cloid: {order_cloid}");

    tracing::info!("Placing order...");

    // Create the action using the typed HyperliquidExecAction enum
    let action = HyperliquidExecAction::Order {
        orders: vec![order],
        grouping: HyperliquidExecGrouping::Na,
        builder: None,
    };

    tracing::debug!("ExchangeAction: {action:?}");

    // Also log the action as JSON
    if let Ok(action_json) = serde_json::to_value(&action) {
        let action_json_pretty = serde_json::to_string_pretty(&action_json)?;
        tracing::debug!("Action JSON: {action_json_pretty}");
    }

    match client.post_action_exec(&action).await {
        Ok(response) => {
            tracing::info!("Order placed successfully!");
            tracing::info!("Response: {response:#?}");

            // Also log as JSON for easier reading
            if let Ok(json) = serde_json::to_string_pretty(&response) {
                tracing::info!("Response JSON:\n{json}");
            }
        }
        Err(e) => {
            tracing::error!("Failed to place order: {e}");
            tracing::error!("Error details: {e:?}");
            return Err(e.into());
        }
    }
    tracing::info!("Done!");
    Ok(())
}
