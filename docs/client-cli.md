# Command-Only Banking CLI

This guide covers installation, configuration, every supported command, retry safety, and recovery of uncertain banking mutations. Run all examples from the repository root.

> Output blocks in this guide are illustrative expected output, not evidence from a live deployment. IDs, timestamps, balances, and ordering will differ.

## Installation and setup

The client requires Python 3.12 and uses synchronous HTTPX. Create a virtual environment and install only the client dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r client/requirements.txt
```

The default configuration file is `~/.mini-banking/config.json`. Copy the tracked example and replace both example endpoints with addresses from your deployment:

```bash
mkdir -p ~/.mini-banking
cp client/config.example.json ~/.mini-banking/config.json
chmod 700 ~/.mini-banking
chmod 600 ~/.mini-banking/config.json
```

The configuration format is:

```json
{
  "default_service": "standalone",
  "services": {
    "standalone": {"base_url": "http://SERVER_A_ADDRESS:8000"},
    "ha": {"base_url": "http://WITNESS_HAPROXY_ADDRESS:PORT"}
  },
  "pending_dir": "~/.mini-banking/pending",
  "timeouts": {"connect": 3, "read": 15, "write": 5, "pool": 3},
  "retry": {
    "max_attempts": 3,
    "deadline_seconds": 90,
    "lookup_timeout_seconds": 10,
    "backoff_seconds": [5, 15],
    "jitter_seconds": 1
  }
}
```

`standalone` connects directly to Server A. `ha` connects to HAProxy on Witness. The client does not discover Primaries or perform failover; HAProxy selects the authorized Primary.

Run the CLI with:

```bash
.venv/bin/python -m client --help
```

## Global options

Global options must appear **before** the command:

```text
python -m client [--service PROFILE] [--config PATH] [--json] COMMAND ...
```

| Option | Meaning |
|---|---|
| `--service PROFILE` | Select a configured service profile. The configured default is used when omitted. |
| `--config PATH` | Use a configuration file other than `~/.mini-banking/config.json`. |
| `--json` | Write one machine-readable JSON result to stdout. Retry diagnostics still use stderr. |
| `--request-id UUID` | Mutation-command option that supplies the idempotency UUID. A UUIDv4 is generated when omitted. |

For example, this placement is valid:

```bash
.venv/bin/python -m client --service ha --json balance --account-id 1
```

## Command reference

### `create-account`

Creates an active account with a zero balance. Names must contain non-whitespace and be at most 100 characters.

```text
python -m client [GLOBAL OPTIONS] create-account --name NAME [--request-id UUID]
```

```bash
.venv/bin/python -m client create-account --name "Ali"
```

Expected result: exit 0 and a `SUCCEEDED` result containing the generated request ID and account ID.

### `deposit`

Deposits a positive integer amount into an active account.

```text
python -m client [GLOBAL OPTIONS] deposit --account-id ID --amount AMOUNT [--request-id UUID]
```

```bash
.venv/bin/python -m client deposit --account-id 1 --amount 500
```

Expected result: exit 0 with the confirmed persisted outcome, including `balance_before` and `balance_after` in the server data.

### `withdraw`

Withdraws a positive integer amount when the active account has sufficient funds.

```text
python -m client [GLOBAL OPTIONS] withdraw --account-id ID --amount AMOUNT [--request-id UUID]
```

```bash
.venv/bin/python -m client withdraw --account-id 1 --amount 200
```

Expected result: exit 0 on success or exit 2 with `INSUFFICIENT_FUNDS` for a confirmed business rejection.

### `balance`

Reads the current balance and lifecycle status with one HTTP attempt. It does not retry or create a pending record.

```text
python -m client [GLOBAL OPTIONS] balance --account-id ID
```

```bash
.venv/bin/python -m client balance --account-id 1
```

Expected result: exit 0 with `account_id`, `balance`, and `status`.

### `close-account`

Closes an account with `DELETE /accounts/{account_id}`. The account must have a zero balance.

```text
python -m client [GLOBAL OPTIONS] close-account --account-id ID [--request-id UUID]
```

```bash
.venv/bin/python -m client close-account --account-id 1
```

Expected result: exit 0, or exit 2 with `NON_ZERO_BALANCE` or `ACCOUNT_ALREADY_CLOSED`.

### `request get`

Looks up a persisted server request without retrying. An existing persisted `REJECTED` record is a successful lookup and returns exit 0.

```text
python -m client [GLOBAL OPTIONS] request get UUID
```

```bash
.venv/bin/python -m client request get 550e8400-e29b-41d4-a716-446655440000
```

Expected result: exit 0 with the stored `SUCCEEDED` or `REJECTED` outcome; exit 4 when the request does not exist.

### `request list`

Lists persisted request history. Filters combine and pagination is applied by the server.

```text
python -m client [GLOBAL OPTIONS] request list [--account-id ID]
    [--status SUCCEEDED|REJECTED]
    [--operation CREATE_ACCOUNT|CLOSE_ACCOUNT|DEPOSIT|WITHDRAW]
    [--limit 1..100] [--offset N]
```

```bash
.venv/bin/python -m client request list --account-id 1 --status SUCCEEDED --limit 20
```

Expected result: exit 0 with `items`, `total`, `limit`, and `offset`.

### `pending list`

Displays unresolved local mutations without contacting a server.

```text
python -m client [GLOBAL OPTIONS] pending list
```

```bash
.venv/bin/python -m client pending list
```

Expected result: exit 0 with every pending request and its service, operation, original target, payload, and creation time.

### `pending recover`

Looks up every unresolved request through its stored service profile. Confirmed outcomes are removed; unresolved records remain. This command never resends a mutation.

```text
python -m client [GLOBAL OPTIONS] pending recover
```

```bash
.venv/bin/python -m client pending recover
```

Expected result: exit 0 when all records resolve, or exit 3 if any remain UNKNOWN.

### `pending check`

Performs one result lookup for a selected pending UUID. It never resends the mutation.

```text
python -m client [GLOBAL OPTIONS] pending check UUID
```

```bash
.venv/bin/python -m client pending check 550e8400-e29b-41d4-a716-446655440000
```

Expected result: exit 0 or 2 for a confirmed outcome, exit 3 if unresolved, or exit 4 if no local record exists.

### `pending retry`

Requires explicit `--confirm`. It first looks up the UUID. Only if still unresolved does it resend the exact stored method, target, payload, service identifier, and UUID under the normal retry policy.

```text
python -m client [GLOBAL OPTIONS] pending retry UUID --confirm
```

```bash
.venv/bin/python -m client pending retry 550e8400-e29b-41d4-a716-446655440000 --confirm
```

Expected result: the confirmed outcome, or exit 3 if the operation remains UNKNOWN.

## Retry and idempotency

Mutations are persisted before their first network attempt. By default, the client makes at most three total attempts within a 90-second deadline. Connection failures, HTTP timeouts, and HTTP 502/503/504 are retried. The delays before attempts two and three are 5 and 15 seconds plus up to one second of bounded jitter.

Business rejections, validation errors, idempotency conflicts, successful responses, and HTTP 500 are not automatically resent. HTTP 500 is uncertain, so the client immediately performs one request lookup. After exhausted transient attempts it also performs one lookup with an independent 10-second timeout.

Without `--request-id`, the client generates one UUIDv4 and uses it for every attempt. To control the UUID:

```bash
REQUEST_ID=550e8400-e29b-41d4-a716-446655440000
.venv/bin/python -m client deposit --account-id 1 --amount 50 --request-id "$REQUEST_ID"
```

Repeating the same UUID and payload returns the original result with `replayed: true`:

```bash
.venv/bin/python -m client deposit --account-id 1 --amount 50 --request-id "$REQUEST_ID"
```

Reusing it with a different payload produces `IDEMPOTENCY_CONFLICT` and exit 4:

```bash
.venv/bin/python -m client deposit --account-id 1 --amount 51 --request-id "$REQUEST_ID"
```

If that UUID already has a local unresolved pending record, a new mutation is
rejected with exit 4 before any HTTP request is sent. Use `pending check` or
`pending retry` so the original operation and payload remain recoverable.

## UNKNOWN handling and recovery

UNKNOWN means the client cannot determine whether a mutation committed. It does **not** mean failure. A missing request record also does not prove non-execution.

Pending files are stored by default under:

```text
~/.mini-banking/pending/<service-profile>/<request-id>.json
```

Directories use mode 0700 and files use mode 0600. Writes use flush, `fsync`, and atomic replacement. One OS-level lock prevents concurrent CLI processes from manipulating the store.

Inspect and recover unresolved work with:

```bash
.venv/bin/python -m client pending list
.venv/bin/python -m client pending check UUID
.venv/bin/python -m client pending recover
```

If lookup remains unresolved and an operator decides to resend:

```bash
.venv/bin/python -m client pending retry UUID --confirm
```

Never retry an UNKNOWN mutation with a new UUID. A new UUID represents a new logical operation and can duplicate a deposit, withdrawal, account creation, or closure attempt. `pending retry` safely reuses the original UUID and payload.

## Exit codes

| Code | Name | Example |
|---:|---|---|
| 0 | `SUCCESS` | Mutation confirmed, balance read, or persisted rejected request successfully inspected. |
| 1 | `GENERAL_ERROR` | An otherwise unclassified client failure reached the top-level safety boundary. |
| 2 | `REJECTED` | Confirmed insufficient funds or another banking rejection. |
| 3 | `UNKNOWN` | Mutation attempts and result lookup could not establish the outcome. |
| 4 | `CLIENT_ERROR` | Invalid arguments/configuration, HTTP validation failure, missing request, or idempotency conflict. |
| 5 | `TECHNICAL_ERROR` | Local pending-store failure or a read-only network/technical failure. |

Shell example:

```bash
.venv/bin/python -m client withdraw --account-id 1 --amount 999999
echo "$?"  # Expected example: 2 when the server confirms INSUFFICIENT_FUNDS
```

## Standalone and HA usage

Select direct Standalone service:

```bash
.venv/bin/python -m client --service standalone balance --account-id 1
```

Select HAProxy on Witness:

```bash
.venv/bin/python -m client --service ha balance --account-id 1
```

The HA endpoint must be configured locally. The client sends ordinary HTTP requests to HAProxy and does not select, promote, or fail over database servers itself.

## Demonstration scenarios

Set demonstration variables first:

```bash
PY=.venv/bin/python
RID=550e8400-e29b-41d4-a716-446655440000
```

### Create an account and inspect its balance

```bash
$PY -m client create-account --name "Demo Account"
$PY -m client balance --account-id 1
```

### Deposit and withdraw

```bash
$PY -m client deposit --account-id 1 --amount 500
$PY -m client withdraw --account-id 1 --amount 200
$PY -m client balance --account-id 1
```

### Idempotency replay

```bash
$PY -m client deposit --account-id 1 --amount 25 --request-id "$RID"
$PY -m client deposit --account-id 1 --amount 25 --request-id "$RID"
```

The second expected result contains `replayed: true` and does not deposit again.

### Idempotency conflict

```bash
$PY -m client deposit --account-id 1 --amount 26 --request-id "$RID"
echo "$?"  # Expected example: 4
```

### Insufficient funds

```bash
$PY -m client withdraw --account-id 1 --amount 999999
echo "$?"  # Expected example: 2
```

### Close an account

Bring the balance to zero, then close it:

```bash
$PY -m client balance --account-id 1
$PY -m client withdraw --account-id 1 --amount 300
$PY -m client close-account --account-id 1
```

Adjust the withdrawal to the actual displayed balance.

### Recover UNKNOWN after temporary unavailability

After a mutation reports UNKNOWN:

```bash
$PY -m client pending list
$PY -m client pending recover
$PY -m client pending check UNKNOWN_REQUEST_UUID
$PY -m client pending retry UNKNOWN_REQUEST_UUID --confirm
```

Use `pending retry` only after service connectivity returns and lookup is still unresolved.

### Switch between profiles

```bash
$PY -m client --service standalone request list --limit 10
$PY -m client --service ha request list --limit 10
```

## Troubleshooting

### Connection failures

Verify the selected profile and `base_url`. Mutations retry transient connection failures and may become UNKNOWN. Read-only commands make one attempt and return exit 5.

### HTTP 503 during failover

HTTP 503 is transient for mutations and is retried within the configured limits. In HA mode, keep using the HA Witness profile; HAProxy performs routing. Do not point the client at another server mid-request.

### Invalid configuration

Configuration errors return exit 4. Validate JSON syntax, profile names, HTTP/HTTPS URLs, positive timeout values, retry counts, and that `backoff_seconds` contains one delay for each retry.

### Pending-store lock conflicts

Only one CLI process may own the pending store. Wait for the other client command to finish. The OS releases the lock automatically when that process terminates.

### UNKNOWN outcomes

Do not assume failure and do not generate a replacement UUID. Use `pending check`, `pending recover`, and, after explicit operator review, `pending retry ... --confirm`.

### Common argument errors

- Put `--service`, `--config`, and `--json` before the command.
- Use positive integer account IDs and amounts.
- Use limits from 1 through 100 and non-negative offsets.
- Supply canonical or otherwise valid UUID text to UUID arguments.
- Include `--confirm` for `pending retry`.
- Quote account names containing spaces.
