#!/usr/bin/env python
"""
SeedSigner Max, SD di verifica.

This image does not run SeedSigner: it starts the nonce verifier (src/nonceverify),
which checks the signatures made by another signer (or by the signing SD) and never
signs anything itself.
"""
import sys


def main(sys_argv=None):
    from nonceverify.app import main as verifier_main
    verifier_main()


if __name__ == "__main__":
    main(sys.argv[1:])
