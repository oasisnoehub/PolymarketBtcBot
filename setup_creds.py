"""
setup_creds.py — One-time Polymarket API credential derivation.

Polymarket's API keys are deterministically derived from your wallet private key.
Run this once to populate your .env file.

Usage:
    python setup_creds.py --key 0xYOUR_PRIVATE_KEY

This writes the derived api_key, api_secret, and api_passphrase to stdout
so you can paste them into your .env file.
"""

import argparse
import sys


def derive_api_credentials(private_key: str) -> dict:
    """
    Derive Polymarket API credentials from a private key using the
    official py-clob-client library.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("❌ py-clob-client not installed. Run: pip install py-clob-client==0.34.5")
        sys.exit(1)

    try:
        # Temporary client just for key derivation
        client = ClobClient(
            host     = "https://clob.polymarket.com",
            chain_id = 137,
            key      = private_key,
        )
        creds = client.create_or_derive_api_creds()
        return {
            "POLY_API_KEY":        creds.api_key,
            "POLY_API_SECRET":     creds.api_secret,
            "POLY_API_PASSPHRASE": creds.api_passphrase,
        }
    except Exception as e:
        print(f"❌ Credential derivation failed: {e}")
        sys.exit(1)


def get_proxy_wallet(private_key: str) -> str:
    """Get the proxy wallet address for the given private key."""
    try:
        from eth_account import Account
        acct = Account.from_key(private_key)
        return acct.address
    except Exception as e:
        print(f"⚠️  Could not derive proxy wallet: {e}")
        return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Derive Polymarket API credentials from private key"
    )
    parser.add_argument(
        "--key", required=True,
        help="Your wallet private key (hex, starting with 0x)"
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Append credentials to .env file"
    )
    args = parser.parse_args()

    key = args.key.strip()
    
    # Validate the key format
    if not key.startswith("0x"):
        key = "0x" + key
    
    # Remove 0x prefix for validation
    key_without_prefix = key[2:] if key.startswith("0x") else key
    
    # Check if it's a valid hex string
    if not all(c in '0123456789abcdefABCDEF' for c in key_without_prefix):
        print("❌ Invalid private key format!")
        print("   Private keys must be 64 hexadecimal characters (0-9, a-f)")
        print("   Example: 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")
        print("\n⚠️  The key you provided appears to be a UUID or other identifier, not an Ethereum private key.")
        print("   Please use your actual Ethereum wallet private key (64 hex characters).")
        sys.exit(1)
    
    if len(key_without_prefix) != 64:
        print(f"❌ Invalid private key length: {len(key_without_prefix)} characters")
        print("   Private keys must be exactly 64 hexadecimal characters")
        print("   Example: 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")
        sys.exit(1)

    print("🔑 Deriving API credentials…")
    creds  = derive_api_credentials(key)
    wallet = get_proxy_wallet(key)

    print("\n─── Paste into your .env file ───────────────────────")
    print(f"POLY_PRIVATE_KEY={key}")
    print(f"POLY_FUNDER_ADDRESS={wallet}")
    for k, v in creds.items():
        print(f"{k}={v}")
    print(f"POLY_SIGNATURE_TYPE=1")
    print("─────────────────────────────────────────────────────")

    if args.write:
        existing = ""
        try:
            with open(".env", "r") as f:
                existing = f.read()
        except FileNotFoundError:
            pass

        additions = []
        for k, v in {**creds, "POLY_PRIVATE_KEY": key,
                     "POLY_FUNDER_ADDRESS": wallet,
                     "POLY_SIGNATURE_TYPE": "1"}.items():
            if k not in existing:
                additions.append(f"{k}={v}")

        if additions:
            with open(".env", "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")
            print(f"\n✅ Appended {len(additions)} keys to .env")
        else:
            print("\nℹ️  All keys already present in .env")
