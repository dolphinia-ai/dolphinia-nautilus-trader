// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
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

//! Sanity-check binary that exercises the Coinbase authenticated REST API.
//!
//! Run with:
//!
//! ```bash
//! cargo run -p nautilus-coinbase --bin coinbase-http-private
//! ```
//!
//! Requires `COINBASE_API_KEY` and `COINBASE_API_SECRET` in the environment
//! (CDP API key name and PEM-encoded EC private key). Reads only — submits
//! no orders. Exercises the new typed domain methods end-to-end so the
//! parse path and HTTP signing can be verified against a live account.

use nautilus_coinbase::{
    common::enums::{CoinbaseEnvironment, CoinbaseProductType},
    http::client::CoinbaseHttpClient,
};
use nautilus_model::identifiers::{AccountId, InstrumentId};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    nautilus_common::logging::ensure_logging_initialized();

    let client = CoinbaseHttpClient::from_env(CoinbaseEnvironment::Live)?;
    let account_id = AccountId::new("COINBASE-001");

    log::info!("Bootstrapping spot instruments");
    let instruments = client
        .request_instruments(Some(CoinbaseProductType::Spot))
        .await?;
    log::info!("Cached {} spot instruments", instruments.len());

    log::info!("Requesting account state");
    match client.request_account_state(account_id).await {
        Ok(state) => {
            log::info!("Account has {} balance(s)", state.balances.len());
            for balance in &state.balances {
                log::info!(
                    "  {} total={} free={} locked={}",
                    balance.currency.code,
                    balance.total,
                    balance.free,
                    balance.locked,
                );
            }
        }
        Err(e) => log::error!("{e:?}"),
    }

    log::info!("Requesting open order status reports");

    match client
        .request_order_status_reports(account_id, None, true, None, None, Some(50))
        .await
    {
        Ok(reports) => {
            log::info!("Received {} open order report(s)", reports.len());
            for report in reports.iter().take(5) {
                log::debug!("{report:?}");
            }
        }
        Err(e) => log::error!("{e:?}"),
    }

    log::info!("Requesting recent BTC-USD order history");
    let btc_usd = InstrumentId::from("BTC-USD.COINBASE");
    match client
        .request_order_status_reports(account_id, Some(btc_usd), false, None, None, Some(25))
        .await
    {
        Ok(reports) => {
            log::info!("Received {} BTC-USD order report(s)", reports.len());
            for report in reports.iter().take(5) {
                log::debug!("{report:?}");
            }
        }
        Err(e) => log::error!("{e:?}"),
    }

    log::info!("Requesting recent fill reports");

    match client
        .request_fill_reports(account_id, None, None, None, None, Some(25))
        .await
    {
        Ok(reports) => {
            log::info!("Received {} fill report(s)", reports.len());
            for report in reports.iter().take(5) {
                log::debug!("{report:?}");
            }
        }
        Err(e) => log::error!("{e:?}"),
    }

    Ok(())
}
