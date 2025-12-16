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

//! Approve the Nautilus builder fee for Hyperliquid trading.
//!
//! This is a ONE-TIME setup step required before trading on Hyperliquid.
//!
//! Prerequisites:
//! - Set environment variable: HYPERLIQUID_PK (mainnet) or HYPERLIQUID_TESTNET_PK (testnet)
//!
//! Usage:
//!     # Mainnet
//!     cargo run --bin hyperliquid-builder-fee-approve
//!
//!     # Testnet
//!     HYPERLIQUID_TESTNET=true cargo run --bin hyperliquid-builder-fee-approve

use nautilus_hyperliquid::common::builder_fee;

#[tokio::main]
async fn main() {
    let success = builder_fee::approve_from_env().await;
    if !success {
        std::process::exit(1);
    }
}
