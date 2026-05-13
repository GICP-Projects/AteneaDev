#!/usr/bin/env bash
set -euo pipefail

# Directories
DATA_DIR="./data"
GEOIP_DIR="$DATA_DIR/geoip"
GEOIP_FILE="GeoLite2-Country.mmdb"
GEOIP_PATH="$GEOIP_DIR/$GEOIP_FILE"

# Download URL
DOWNLOAD_URL="https://git.io/GeoLite2-Country.mmdb"

# Check if the GeoLite2-Country.mmdb already exists
if [[ -f "$GEOIP_PATH" ]]; then
    echo "GeoLite2-Country.mmdb already exists at '$GEOIP_PATH', skipping download."
    exit 0
fi

echo "Downloading GeoLite2-Country.mmdb..."

# Create the geoip directory if it doesn't exist
mkdir -p "$GEOIP_DIR"

# Download directly into the geoip directory
wget -q "$DOWNLOAD_URL" -O "$GEOIP_PATH"

echo "Download complete: '$GEOIP_PATH'"

