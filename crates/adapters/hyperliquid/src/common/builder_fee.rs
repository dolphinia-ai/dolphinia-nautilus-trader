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

//! Builder fee approval and verification functionality.
//!
//! Note: Hyperliquid uses non-standard EIP-712 type names with colons
//! (e.g., "HyperliquidTransaction:ApproveBuilderFee") which cannot be
//! represented using alloy's `sol!` macro. The struct hash is computed
//! manually while the domain uses alloy's `Eip712Domain`.

use std::{collections::HashMap, str::FromStr, time::SystemTime};

use alloy::sol_types::Eip712Domain;
use alloy_primitives::{Address, B256, keccak256};
use alloy_signer::SignerSync;
use alloy_signer_local::PrivateKeySigner;
use nautilus_network::http::{HttpClient, Method};
use serde::{Deserialize, Serialize};

use super::consts::{
    NAUTILUS_BUILDER_FEE_ADDRESS, NAUTILUS_BUILDER_FEE_PERP_TENTHS_BP,
    NAUTILUS_BUILDER_FEE_SPOT_TENTHS_BP, exchange_url,
};
use crate::{common::credential::EvmPrivateKey, http::error::Result};

/// Builder fee approval rate (1% to cover both spot and perp).
const APPROVAL_FEE_RATE: &str = "1%";

/// Hyperliquid signing chain ID (0x66eee = 421614 decimal).
const HYPERLIQUID_CHAIN_ID: u64 = 421614;

/// Information about the Nautilus builder fee configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BuilderFeeInfo {
    /// The builder address that receives fees.
    pub address: String,
    /// Fee rate for perpetuals in basis points.
    pub perp_rate_bps: u32,
    /// Fee rate for spot in basis points.
    pub spot_rate_bps: u32,
    /// The approval rate required (covers both products).
    pub approval_rate: String,
}

impl Default for BuilderFeeInfo {
    fn default() -> Self {
        Self::new()
    }
}

impl BuilderFeeInfo {
    /// Creates builder fee info from the hardcoded constants.
    #[must_use]
    pub fn new() -> Self {
        Self {
            address: NAUTILUS_BUILDER_FEE_ADDRESS.to_string(),
            perp_rate_bps: NAUTILUS_BUILDER_FEE_PERP_TENTHS_BP / 10, // Convert tenths to bps
            spot_rate_bps: NAUTILUS_BUILDER_FEE_SPOT_TENTHS_BP / 10,
            approval_rate: APPROVAL_FEE_RATE.to_string(),
        }
    }

    /// Prints the builder fee configuration to stdout.
    pub fn print(&self) {
        let separator = "=".repeat(60);

        println!("{separator}");
        println!("NautilusTrader Hyperliquid Builder Fee Configuration");
        println!("{separator}");
        println!();
        println!("Builder address: {}", self.address);
        println!();
        println!("Fee rates charged per trade:");
        println!(
            "  - Perpetuals: {:.1}% ({} basis points)",
            self.perp_rate_bps as f64 / 100.0,
            self.perp_rate_bps
        );
        println!(
            "  - Spot:       {:.1}% ({} basis points)",
            self.spot_rate_bps as f64 / 100.0,
            self.spot_rate_bps
        );
        println!();
        println!("Approval rate required: {}", self.approval_rate);
        println!("(This covers both product types)");
        println!();
        println!("These fees are paid from Hyperliquid's rebate program,");
        println!("not from your trading balance.");
        println!();
        println!("The builder address is controlled by the NautilusTrader team.");
        println!();
        println!("Source: crates/adapters/hyperliquid/src/common/consts.rs");
        println!("{separator}");
    }
}

/// Result of a builder fee approval request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BuilderFeeApprovalResult {
    /// Whether the approval was successful.
    pub success: bool,
    /// The status returned by Hyperliquid.
    pub status: String,
    /// Optional response message or error details.
    pub message: Option<String>,
    /// The wallet address that made the approval.
    pub wallet_address: String,
    /// The builder address that was approved.
    pub builder_address: String,
    /// Whether this was on testnet.
    pub is_testnet: bool,
}

/// Approves the Nautilus builder fee for a wallet.
///
/// This signs an EIP-712 `ApproveBuilderFee` action and submits it to Hyperliquid.
/// The approval allows NautilusTrader to include builder fees on orders for this wallet.
///
/// # Arguments
///
/// * `private_key` - The EVM private key (hex string with or without 0x prefix)
/// * `is_testnet` - Whether to use testnet or mainnet
///
/// # Returns
///
/// The result of the approval request.
///
/// # Errors
///
/// Returns an error if the private key is invalid, signing fails, or the HTTP request fails.
///
/// # Panics
///
/// Panics if the JSON response structure is unexpected.
pub async fn approve_builder_fee(
    private_key: &str,
    is_testnet: bool,
) -> Result<BuilderFeeApprovalResult> {
    let pk = EvmPrivateKey::new(private_key.to_string())?;
    let wallet_address = derive_address(&pk)?;

    // Get current timestamp for nonce
    let nonce = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_err(|e| crate::http::error::Error::transport(format!("Time error: {e}")))?
        .as_millis() as u64;

    // Sign the approval
    let signature = sign_approve_builder_fee(&pk, is_testnet, nonce)?;

    // Build the request payload
    let action = serde_json::json!({
        "type": "approveBuilderFee",
        "hyperliquidChain": if is_testnet { "Testnet" } else { "Mainnet" },
        "signatureChainId": "0x66eee",
        "maxFeeRate": APPROVAL_FEE_RATE,
        "builder": NAUTILUS_BUILDER_FEE_ADDRESS,
        "nonce": nonce,
    });

    let payload = serde_json::json!({
        "action": action,
        "nonce": nonce,
        "signature": signature,
    });

    // Send the request
    let url = exchange_url(is_testnet);
    let client =
        HttpClient::new(HashMap::new(), vec![], vec![], None, None, None).map_err(|e| {
            crate::http::error::Error::transport(format!("Failed to create client: {e}"))
        })?;

    let body_bytes = serde_json::to_vec(&payload)
        .map_err(|e| crate::http::error::Error::transport(format!("Failed to serialize: {e}")))?;

    let headers = HashMap::from([(
        "Content-Type".to_string(),
        vec!["application/json".to_string()],
    )]);
    let response = client
        .request(
            Method::POST,
            url.to_string(),
            Some(&headers),
            None,
            Some(body_bytes),
            None,
            None,
        )
        .await
        .map_err(|e| crate::http::error::Error::transport(format!("HTTP request failed: {e}")))?;

    let response_json: serde_json::Value = serde_json::from_slice(&response.body).map_err(|e| {
        crate::http::error::Error::transport(format!("Failed to parse response: {e}"))
    })?;

    // Parse the response
    let status = response_json
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();

    let success = status == "ok";
    let message = response_json.get("response").map(|v: &serde_json::Value| {
        if v.is_string() {
            v.as_str().unwrap().to_string()
        } else {
            v.to_string()
        }
    });

    Ok(BuilderFeeApprovalResult {
        success,
        status,
        message,
        wallet_address,
        builder_address: NAUTILUS_BUILDER_FEE_ADDRESS.to_string(),
        is_testnet,
    })
}

/// Approves the Nautilus builder fee using environment variables.
///
/// Reads private key from environment:
/// - Testnet: `HYPERLIQUID_TESTNET_PK`
/// - Mainnet: `HYPERLIQUID_PK`
///
/// Set `HYPERLIQUID_TESTNET=true` to use testnet.
///
/// Prints progress and results to stdout.
///
/// # Returns
///
/// `true` if approval succeeded, `false` otherwise.
pub async fn approve_from_env() -> bool {
    let is_testnet = std::env::var("HYPERLIQUID_TESTNET")
        .map(|v| v.to_lowercase() == "true")
        .unwrap_or(false);

    let env_var = if is_testnet {
        "HYPERLIQUID_TESTNET_PK"
    } else {
        "HYPERLIQUID_PK"
    };

    let private_key = match std::env::var(env_var) {
        Ok(pk) => pk,
        Err(_) => {
            println!("Error: {env_var} environment variable not set");
            return false;
        }
    };

    let info = BuilderFeeInfo::new();
    let network = if is_testnet { "testnet" } else { "mainnet" };

    println!("Approving Nautilus builder fee on {network}");
    println!("Builder address: {}", info.address);
    println!(
        "Approval rate: {} (covers both perp and spot)",
        info.approval_rate
    );
    println!();
    println!("Approving builder fee...");

    match approve_builder_fee(&private_key, is_testnet).await {
        Ok(result) => {
            println!();
            println!("Wallet address: {}", result.wallet_address);
            println!("Status: {}", result.status);
            if let Some(msg) = &result.message {
                println!("Response: {msg}");
            }
            println!();

            if result.success {
                println!("Builder fee approved successfully!");
                println!("You can now trade on Hyperliquid via NautilusTrader.");
            } else {
                println!("Approval may have failed. Check the response above.");
            }

            result.success
        }
        Err(e) => {
            println!("Error: {e}");
            false
        }
    }
}

/// Signs the ApproveBuilderFee action using EIP-712.
///
/// Note: The struct hash is computed manually because Hyperliquid uses a non-standard
/// type name "HyperliquidTransaction:ApproveBuilderFee" which cannot be represented
/// with alloy's sol! macro.
fn sign_approve_builder_fee(
    pk: &EvmPrivateKey,
    is_testnet: bool,
    nonce: u64,
) -> Result<serde_json::Value> {
    // EIP-712 domain separator hash (using alloy's Eip712Domain)
    let domain_hash = compute_domain_hash();

    // Struct type hash for HyperliquidTransaction:ApproveBuilderFee
    let type_hash = keccak256(
        b"HyperliquidTransaction:ApproveBuilderFee(string hyperliquidChain,string maxFeeRate,address builder,uint64 nonce)",
    );

    // Hash the message fields
    let chain_str = if is_testnet { "Testnet" } else { "Mainnet" };
    let chain_hash = keccak256(chain_str.as_bytes());
    let fee_rate_hash = keccak256(APPROVAL_FEE_RATE.as_bytes());

    // Parse builder address
    let builder_addr = Address::from_str(NAUTILUS_BUILDER_FEE_ADDRESS).map_err(|e| {
        crate::http::error::Error::transport(format!("Invalid builder address: {e}"))
    })?;

    // Encode the struct hash
    let mut struct_data = Vec::with_capacity(32 * 5);
    struct_data.extend_from_slice(type_hash.as_slice());
    struct_data.extend_from_slice(chain_hash.as_slice());
    struct_data.extend_from_slice(fee_rate_hash.as_slice());

    // Address is padded to 32 bytes (left-padded with zeros)
    let mut addr_bytes = [0u8; 32];
    addr_bytes[12..].copy_from_slice(builder_addr.as_slice());
    struct_data.extend_from_slice(&addr_bytes);

    // Nonce is uint64, padded to 32 bytes (left-padded with zeros)
    let mut nonce_bytes = [0u8; 32];
    nonce_bytes[24..].copy_from_slice(&nonce.to_be_bytes());
    struct_data.extend_from_slice(&nonce_bytes);

    let struct_hash = keccak256(&struct_data);

    // Create final EIP-712 hash: \x19\x01 + domain_hash + struct_hash
    let mut final_data = Vec::with_capacity(66);
    final_data.extend_from_slice(b"\x19\x01");
    final_data.extend_from_slice(&domain_hash);
    final_data.extend_from_slice(struct_hash.as_slice());

    let signing_hash = keccak256(&final_data);

    // Sign the hash
    let key_hex = pk.as_hex();
    let key_hex = key_hex.strip_prefix("0x").unwrap_or(key_hex);

    let signer = PrivateKeySigner::from_str(key_hex).map_err(|e| {
        crate::http::error::Error::transport(format!("Failed to create signer: {e}"))
    })?;

    let hash_b256 = B256::from(signing_hash);
    let signature = signer
        .sign_hash_sync(&hash_b256)
        .map_err(|e| crate::http::error::Error::transport(format!("Failed to sign: {e}")))?;

    // Format signature as {r, s, v} for Hyperliquid
    let r = format!("0x{:064x}", signature.r());
    let s = format!("0x{:064x}", signature.s());
    let v = if signature.v() { 28u8 } else { 27u8 };

    Ok(serde_json::json!({
        "r": r,
        "s": s,
        "v": v,
    }))
}

/// Returns the EIP-712 domain for Hyperliquid builder fee approval.
fn get_eip712_domain() -> Eip712Domain {
    Eip712Domain {
        name: Some("HyperliquidSignTransaction".into()),
        version: Some("1".into()),
        chain_id: Some(alloy_primitives::U256::from(HYPERLIQUID_CHAIN_ID)),
        verifying_contract: Some(Address::ZERO),
        salt: None,
    }
}

/// Computes the EIP-712 domain separator hash for Hyperliquid.
fn compute_domain_hash() -> [u8; 32] {
    *get_eip712_domain().hash_struct()
}

/// Derives the Ethereum address from a private key.
fn derive_address(pk: &EvmPrivateKey) -> Result<String> {
    let key_hex = pk.as_hex();
    let key_hex = key_hex.strip_prefix("0x").unwrap_or(key_hex);

    let signer = PrivateKeySigner::from_str(key_hex).map_err(|e| {
        crate::http::error::Error::transport(format!("Failed to create signer: {e}"))
    })?;

    Ok(format!("{:#x}", signer.address()))
}

////////////////////////////////////////////////////////////////////////////////
// Tests
////////////////////////////////////////////////////////////////////////////////

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    #[rstest]
    fn test_builder_fee_info() {
        let info = BuilderFeeInfo::new();
        assert_eq!(info.address, NAUTILUS_BUILDER_FEE_ADDRESS);
        assert_eq!(info.perp_rate_bps, 10); // 0.1%
        assert_eq!(info.spot_rate_bps, 100); // 1.0%
        assert_eq!(info.approval_rate, "1%");
    }

    #[rstest]
    fn test_derive_address() {
        let pk = EvmPrivateKey::new(
            "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef".to_string(),
        )
        .unwrap();
        let addr = derive_address(&pk).unwrap();
        assert!(addr.starts_with("0x"));
        assert_eq!(addr.len(), 42);
    }

    #[rstest]
    fn test_compute_domain_hash() {
        let hash = compute_domain_hash();
        assert_eq!(hash.len(), 32);
    }

    #[rstest]
    fn test_sign_approve_builder_fee() {
        let pk = EvmPrivateKey::new(
            "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef".to_string(),
        )
        .unwrap();
        let nonce = 1640995200000u64;

        let signature = sign_approve_builder_fee(&pk, false, nonce).unwrap();

        assert!(signature.get("r").is_some());
        assert!(signature.get("s").is_some());
        assert!(signature.get("v").is_some());

        let r = signature["r"].as_str().unwrap();
        let s = signature["s"].as_str().unwrap();

        assert!(r.starts_with("0x"));
        assert!(s.starts_with("0x"));
        assert_eq!(r.len(), 66); // 0x + 64 hex chars
        assert_eq!(s.len(), 66);
    }
}
