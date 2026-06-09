# Trusted Python Script Extractors

Expense Manager can load user-owned Python scripts for email formats that are easier to parse with code than with declarative rules.

These scripts are trusted local code. The app does not sandbox them.

## Configuration

Create `script_extractors.json` in the resolved `config` directory:

```json
{
  "enabled": true,
  "paths": ["/absolute/path/to/extractor/scripts"]
}
```

Each configured path can be either a directory containing `*.py` scripts or one direct Python file. Scripts are loaded once when the app starts; restart the app after changing scripts or the config.

Set `EXPENSE_MANAGER_HOME` during development if you want the config file under a known local directory such as `<home>/config/script_extractors.json`.

The repo includes one ready-to-use script for Axis credit-card spend emails:

```json
{
  "enabled": true,
  "paths": ["/absolute/path/to/ExpenseManager/scripts/email_extractors"]
}
```

That loads `axis_credit_card_spend.py`, which parses Axis Bank emails whose subject says `spent on credit card`.

## Script Contract

Each enabled script must define `parse_email(email)` and either `MATCH` or `matches(email)`.

```python
EXTRACTOR_ID = "axis_card_spend"
ORDER = 100
RUN_ON_PARSED = False

MATCH = {
    "from_contains": ["alerts@axis.bank.in"],
    "subject_contains": ["spent on credit card"]
}

def parse_email(email):
    text = email["text_body"]
    # Parse however you want here.
    return [{
        "bank_name": "Axis Bank",
        "direction": "debit",
        "amount": 4431.0,
        "currency": "INR",
        "account_suffix": "1234",
        "counterparty": "EXAMPLESTORE",
        "timestamp": "2026-06-08T10:42:39+05:30",
        "transaction_id": ""
    }]
```

Supported `MATCH` keys:

- `from_contains`
- `sender_contains`
- `subject_contains`
- `title_contains`
- `body_contains`
- `text_contains`
- `html_contains`
- `any_contains`

If multiple keys are present, each key must match at least one of its tokens.

## Email Input

`parse_email(email)` receives a plain dictionary:

- `provider_id`
- `record_type`
- `external_id`
- `title`
- `sender`
- `received_at`
- `subject`
- `text_body`
- `html_body`
- `payload`
- `existing_facts`

## Output

Return a list of transaction dictionaries. Required fields are:

- `direction`: `debit` or `credit`
- `amount`: numeric
- `account_suffix`: masked account/card suffix or digits

Optional fields:

- `bank_name`
- `currency`
- `counterparty`
- `timestamp`
- `transaction_id`
- `category`
- `confidence`

Missing `timestamp` falls back to the email received time. Debit transactions also emit an expense fact.

By default, scripts do not run when a built-in parser already produced facts. Set `RUN_ON_PARSED = True` only when the script should augment already parsed emails.
