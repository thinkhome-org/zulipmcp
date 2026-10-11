# Identity provisioning (development)

The local CLI and encrypted store are implemented. See [HTTP authorization](http-auth.md)
for the resource server; provisioning commands themselves do not expose a service.
Provisioning is an administrator operation outside MCP tools. It makes no Zulip
requests and does not prove that the provisioned credentials are valid upstream.

## Storage and access

Run as the service's OS user. Use a private directory (mode `0700`) outside the
checkout. Set `MCP_STORE_PATH` to the SQLite database and `MCP_KEY_FILE` to the
external Fernet key file. Database and secret files must have mode `0600` (or
more restrictive permissions); symlink secret inputs are rejected. Protect the
parent directory from replacement by other users. SQLite contains encrypted
identity records, SHA-256 digests of random 256-bit MCP tokens, expiry timestamps,
and exact `(issuer, subject)` OAuth bindings. The encryption key is not in SQLite.
The store does not retain messages or tool payloads. Metadata such as identity IDs
and OAuth subjects is not encrypted, so backups remain sensitive.

```sh
install -d -m 700 "$HOME/.local/share/thinkhome-zulip-mcp"
export MCP_STORE_PATH="$HOME/.local/share/thinkhome-zulip-mcp/identities.db"
export MCP_KEY_FILE="$HOME/.local/share/thinkhome-zulip-mcp/key"
thinkhome-zulip-mcp secrets init
thinkhome-zulip-mcp identity add --id alice --realm https://zulip.example \
  --email alice@example.com
```

`identity add` and `identity update` read the Zulip API key through a hidden
terminal prompt, or from `--api-key-file /private/path/key`. There is no raw-key
argument. Noninteractive use requires the file. Updating an identity preserves
its tokens and changes the credentials loaded by subsequent authentications.
`identity remove --id alice` deletes the identity, tokens, and OAuth bindings.
An already executing upstream request cannot be recalled by removing an identity.
HTTP for a local test realm requires explicit `--allow-insecure-http`; keep HTTPS
for deployment.

## Manual MCP tokens

```sh
thinkhome-zulip-mcp token issue --id alice --expires-at FUTURE_UNIX_TIMESTAMP \
  --output /private/new-token-file
thinkhome-zulip-mcp token revoke --token-file /private/new-token-file
```

Replace `FUTURE_UNIX_TIMESTAMP` with the desired expiration in Unix seconds.
Issuance requires a future expiration; a new random token is written only once to
a newly created mode-0600 file, never stdout. Existing files are not overwritten.
A failed output write revokes that new token. Keep the file in secret storage and
supply it to the client securely. Revocation is idempotent. Tokens are not Zulip
API keys and must never be forwarded to Zulip.

Provision an OAuth mapping for the resource server:

```sh
thinkhome-zulip-mcp identity bind-oauth --id alice \
  --issuer https://login.example/realms/mcp --subject SUBJECT_FROM_PROVIDER
```

Bindings use the exact issuer and subject, never an email claim. Duplicate
bindings cannot be reassigned silently. Binding alone does not validate or accept
an OAuth token; the resource server also verifies signature, issuer, audience, expiry and scope.

## Backup, rotation and recovery

1. Stop the MCP process and all provisioning writers. This is an offline
   administrative operation for the single-replica v1 design.
2. Back up the closed SQLite database and its matching encryption key to separate
   protected locations. Test restoration on a copy; do not overwrite the live DB.
3. Generate a second key at a new path by running `secrets init` with a temporary
   `MCP_KEY_FILE` pointing to that new path. Keep the original key intact.
4. With the original `MCP_KEY_FILE` restored in the environment, run
   `thinkhome-zulip-mcp secrets rotate --new-key-file /private/new-key`.
5. Rotation re-encrypts all identities and the key marker in one SQLite
   transaction. Any decryption failure rolls back the transaction. Set the
   service's `MCP_KEY_FILE` to the new path before restarting. Stale writers using
   the old key are rejected on their next transaction.
6. Retain the old key only as long as its matching backups require. After a crash,
   the committed DB matches either the old or the new key; preserve both while
   determining which opens it. Never generate a replacement over either file.

Schema version 1 is created transactionally. Unknown schema versions and
unversioned nonempty databases are refused. There is no downgrade migration yet;
rollback requires the matching database/key backup and compatible binary. Losing
the external key loses access to encrypted Zulip credentials. Deleting a local
secret file is not a promise of forensic erasure on SSDs or backup systems.
