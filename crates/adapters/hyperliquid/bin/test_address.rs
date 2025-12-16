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

//! Test address derivation and signing from environment variable.
//!
//! Usage:
//!     cargo run --bin hyperliquid-test-address

use std::env;

use nautilus_hyperliquid::{
    common::credential::EvmPrivateKey, signing::signers::HyperliquidEip712Signer,
};

fn main() {
    let pk = match env::var("HYPERLIQUID_PK") {
        Ok(pk) => pk,
        Err(_) => {
            println!("HYPERLIQUID_PK not set");
            return;
        }
    };

    println!("Key length: {}", pk.len());

    let private_key = match EvmPrivateKey::new(pk) {
        Ok(pk) => pk,
        Err(e) => {
            println!("Failed to create EvmPrivateKey: {e}");
            return;
        }
    };

    let signer = HyperliquidEip712Signer::new(private_key);

    // Test deriving address multiple times
    for i in 1..=5 {
        match signer.address() {
            Ok(addr) => println!("Attempt {i}: {addr}"),
            Err(e) => println!("Attempt {i}: Failed - {e}"),
        }
    }
}
