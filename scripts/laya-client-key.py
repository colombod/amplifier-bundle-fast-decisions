#!/usr/bin/env python3
"""Issue one random client key to a private file; store only its SHA-256 hash.

Run while no other administrator edits the store. Replace the deployed directory
atomically to revoke a client. Never distribute the hash store as client keys.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.client):
        parser.error('Use a short alphanumeric client id')
    if args.store.resolve() == args.token_file.resolve():
        parser.error('Token and hash-store paths must differ')
    clients = json.loads(args.store.read_text()) if args.store.exists() else {}
    if args.client in clients:
        parser.error('Client exists; revoke its hash before issuing a replacement')
    args.store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    # Exclusive creation refuses to overwrite any existing credential.
    with os.fdopen(os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        stream.write(token + '\n')
    clients[args.client] = hashlib.sha256(token.encode()).hexdigest()
    fd, temporary = tempfile.mkstemp(dir=args.store.parent, prefix='.clients-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(clients, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, args.store)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({'client':args.client,'hash_store':str(args.store),
                      'private_token_file':str(args.token_file), 'token_printed':False}))


if __name__ == '__main__':
    main()
