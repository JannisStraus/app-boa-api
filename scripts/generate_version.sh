#!/bin/sh

# Collects the version of the package from Poetry (pyproject.toml) and outputs it to src/boa_ai/version.py

TARGET_FILE=src/boa_ai/_version.py
PACKAGE_VERSION=$(cat pyproject.toml | grep -e "^version" | grep -o -e "[0-9]\+\.[0-9]\+\.[0-9]\+")
GIT_VERSION=$(git rev-parse --short HEAD)

echo "# This file was generated automatically, do not edit" > $TARGET_FILE
echo "__version__ = '$PACKAGE_VERSION'" >> $TARGET_FILE
echo "__githash__ = '$GIT_VERSION'" >> $TARGET_FILE